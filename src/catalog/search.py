"""Поиск по каталогу.

Порядок неслучаен. Покупатель у заказчика чаще всего приходит не со словами, а с
пунктом перечня: «нужна позиция 2.1.14». Поэтому нормативный матч идёт первым и
обгоняет любой текстовый results — иначе бот выдаёт похожее по названию вместо того,
что закрывает требование приказа.

Дальше — обычный лексический поиск (BM25 по полям с разными весами) и триграммный
запасной путь для опечаток и артикулов.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from catalog.models import Product
from catalog.text import expand, stems, trigrams
from norms.extract import codes_in_query, document_ids_in_text

# Вес поля в BM25. Название и раздел каталога описывают товар, описание — уговаривает
# купить: там встречается всё подряд, от «спортивного духа» до «школьных лет».
# Пока описание весило почти как название, запрос про спортзал вытаскивал наверх
# что угодно, где эти слова просто упомянуты.
FIELD_WEIGHTS = {"name": 4.0, "category": 2.2, "kit": 1.0, "description": 0.7}

# Насколько поднимаем товар из «своей» ветки каталога и опускаем из чужой.
# Мягко, а не отсечением: жёсткий фильтр по аудитории оставлял бы человека
# вообще без выдачи там, где заказчик разложил товары иначе, чем мы ожидаем.
_AUDIENCE_BOOST = 1.6
_AUDIENCE_PENALTY = 0.55

_K1 = 1.4
_B = 0.75
# Длина общего начала слова для запасного прохода при пустой выдаче.
_PREFIX_LEN = 4

# Запросы про наличие и цену обрабатываются фильтрами, а не текстом: иначе «до 2000 руб»
# участвует в ранжировании как обычные слова и вытаскивает наверх «Играем в магазин».
_IN_STOCK_HINT = re.compile(
    r"(?:есть\s+)?в\s+наличи[еи]\b|со\s+склада\b|\bимеются\b", re.IGNORECASE
)
_PRICE_MAX = re.compile(
    r"(?:до|дешевле|не\s+дороже|максимум|в\s+пределах)\s+(\d[\d\s]*)\s*(?:т\.?р|тыс\w*)?\s*"
    r"(?:руб\w*|₽|р\.)?",
    re.IGNORECASE,
)
_PRICE_MIN = re.compile(
    r"(?:от|дороже|не\s+дешевле|минимум)\s+(\d[\d\s]*)\s*(?:т\.?р|тыс\w*)?\s*"
    r"(?:руб\w*|₽|р\.)?",
    re.IGNORECASE,
)
_THOUSANDS = re.compile(r"\s*(?:т\.?р|тыс\w*)", re.IGNORECASE)
_STOPWORDS = {
    "для", "и", "или", "с", "со", "на", "в", "во", "по", "из", "от", "до", "к", "у",
    "что", "как", "нужн", "нужен", "нужна", "надо", "хочу", "подбер", "покажи", "най",
    "мне", "пожалуйст", "это", "весь", "все", "нам",
}


@dataclass
class SearchQuery:
    text: str = ""
    limit: int = 20
    in_stock_only: bool = False
    price_min: int | None = None
    price_max: int | None = None
    root: str | None = None
    norm_doc_id: str | None = None
    norm_code: str | None = None
    # Кому подбираем: preschool | school | None. Не отсекает выдачу, а меняет
    # порядок и выбор нормативного основания.
    audience: str | None = None


@dataclass
class SearchHit:
    product: Product
    score: float
    reason: str  # norm_code | text | trigram
    # Пункт, по которому товар нашёлся. У товара их бывает несколько, и назвать
    # в ответе нужно именно тот, о котором спросили.
    matched_code: str | None = None
    audience: str | None = None
    query: str = ""

    @property
    def by_norm(self) -> bool:
        return self.reason == "norm_code"

    def citation(self) -> str | None:
        if self.matched_code:
            for ref in self.product.norms:
                if ref.item_code == self.matched_code:
                    return ref.citation
        ref = self.product.norm_for(self.audience, self.query)
        return ref.citation if ref else None


@dataclass
class _Posting:
    doc: int
    weight: float


@dataclass
class CatalogIndex:
    """Инвертированный индекс по товарам.

    Держит каталог в памяти: 6 тысяч позиций — это десятки мегабайт, зато поиск
    работает без БД, и прототип запускается на любой машине. Интерфейс совпадает
    с тем, что позже реализует PostgreSQL, поэтому переезд не трогает вызовы.
    """

    products: list[Product]
    _postings: dict[str, list[_Posting]] = field(default_factory=lambda: defaultdict(list))
    _lengths: list[float] = field(default_factory=list)
    _avg_length: float = 0.0
    _by_norm_code: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    _by_sku: dict[str, int] = field(default_factory=dict)
    _name_trigrams: list[set[str]] = field(default_factory=list)
    _by_prefix: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def __post_init__(self) -> None:
        self._build()

    # --- Построение ---------------------------------------------------------

    def _build(self) -> None:
        for doc, product in enumerate(self.products):
            self._by_sku[product.sku_1c] = doc
            for code in product.norm_codes:
                self._by_norm_code[code].append(doc)
            self._name_trigrams.append(trigrams(product.name))

            weights: dict[str, float] = defaultdict(float)
            for field_name, text in _fields(product):
                weight = FIELD_WEIGHTS[field_name]
                for token in stems(text):
                    weights[token] += weight

            self._lengths.append(sum(weights.values()) or 1.0)
            for token, weight in weights.items():
                self._postings[token].append(_Posting(doc, weight))

        self._avg_length = (sum(self._lengths) / len(self._lengths)) if self._lengths else 1.0
        for token in self._postings:
            if len(token) >= _PREFIX_LEN:
                self._by_prefix[token[:_PREFIX_LEN]].add(token)

    # --- Поиск --------------------------------------------------------------

    def search(self, query: SearchQuery) -> list[SearchHit]:
        query = apply_text_filters(query)
        allowed = self._filter(query)
        if not allowed:
            return []

        codes = [query.norm_code] if query.norm_code else codes_in_query(query.text)
        hits = self._by_norm(codes, allowed, query)
        seen = {hit.product.sku_1c for hit in hits}

        # Запрос из одного номера пункта («2.1.14») текстом искать нельзя: цифры
        # совпадают с обрывками чужих артикулов и заваливают точную выдачу мусором.
        if not _is_code_only(query.text, codes):
            for hit in self._by_text(query, allowed):
                if hit.product.sku_1c not in seen:
                    hits.append(hit)
                    seen.add(hit.product.sku_1c)
        elif hits:
            return _diversified(hits)[: query.limit]

        if len(hits) < query.limit and query.text.strip():
            for hit in self._by_trigram(query, allowed, seen):
                hits.append(hit)
                seen.add(hit.product.sku_1c)

        return _diversified(hits)[: query.limit]

    def get(self, sku_1c: str) -> Product | None:
        doc = self._by_sku.get(sku_1c)
        return self.products[doc] if doc is not None else None

    def by_norm_code(self, code: str) -> list[Product]:
        return [self.products[doc] for doc in self._by_norm_code.get(code, ())]

    # --- Составляющие -------------------------------------------------------

    def _filter(self, query: SearchQuery) -> set[int]:
        allowed: set[int] = set()
        for doc, product in enumerate(self.products):
            if query.in_stock_only and not product.available:
                continue
            if query.price_min is not None and (product.price or 0) < query.price_min:
                continue
            if query.price_max is not None and (product.price or 0) > query.price_max:
                continue
            if query.root and query.root not in product.roots:
                continue
            if query.norm_doc_id and not any(
                ref.doc_id == query.norm_doc_id for ref in product.norms
            ):
                continue
            allowed.add(doc)
        return allowed

    def _by_norm(
        self, codes: list[str], allowed: set[int], query: SearchQuery | None = None
    ) -> list[SearchHit]:
        hits: dict[str, SearchHit] = {}
        audience = query.audience if query else None
        text = query.text if query else ""

        def keep(doc: int, score: float, code: str) -> None:
            # Товар закрывает несколько пунктов подраздела — в выдаче он один раз.
            sku = self.products[doc].sku_1c
            if sku not in hits:
                hits[sku] = SearchHit(
                    self.products[doc], score, "norm_code", code, audience, text
                )

        for code in codes:
            exact = [doc for doc in self._by_norm_code.get(code, ()) if doc in allowed]
            for doc in exact:
                keep(doc, 1_000.0, code)
            if exact:
                continue
            # «2.4» должно находить всё содержимое подраздела.
            prefix = f"{code}."
            for known, docs in self._by_norm_code.items():
                if not known.startswith(prefix):
                    continue
                for doc in docs:
                    if doc in allowed:
                        keep(doc, 900.0, known)
        return sorted(hits.values(), key=lambda hit: (-hit.score, hit.product.name))

    def _by_text(self, query: SearchQuery, allowed: set[int]) -> list[SearchHit]:
        tokens = [token for token in stems(query.text) if token not in _STOPWORDS]
        if not tokens:
            return []
        # «Спортзал» в каталоге называется «спортивный зал», «мастерская» —
        # «кабинет труда». Раскрываем запрос, а не индекс.
        tokens = expand(tokens)

        scores = self._score(set(tokens), allowed, penalty=1.0)
        if not scores:
            # Отсечение окончаний не справляется с беглой гласной: «станки» не сводится
            # к «станок». Добираем по общему началу слова, но только когда точный
            # поиск не дал вообще ничего — иначе префиксы размывают выдачу.
            expanded: set[str] = set()
            for token in tokens:
                if len(token) >= _PREFIX_LEN:
                    expanded |= self._by_prefix.get(token[:_PREFIX_LEN], set())
            scores = self._score(expanded, allowed, penalty=0.5)

        # Названный в запросе документ («по приказу 838») поднимает свои товары.
        for doc_id in document_ids_in_text(query.text):
            for doc in list(scores):
                if any(ref.doc_id == doc_id for ref in self.products[doc].norms):
                    scores[doc] *= 1.5

        # Подбираем для сада — школьные позиции опускаем, и наоборот.
        if query.audience:
            for doc in list(scores):
                audiences = self.products[doc].audiences
                if not audiences:
                    continue
                scores[doc] *= (
                    _AUDIENCE_BOOST if query.audience in audiences else _AUDIENCE_PENALTY
                )

        ranked = sorted(scores.items(), key=lambda item: (-item[1], self.products[item[0]].name))
        return [
            SearchHit(self.products[doc], score, "text", None, query.audience, query.text)
            for doc, score in ranked
        ]

    def _score(self, tokens: set[str], allowed: set[int], penalty: float) -> dict[int, float]:
        total = len(self.products)
        scores: dict[int, float] = defaultdict(float)
        for token in tokens:
            postings = self._postings.get(token)
            if not postings:
                continue
            idf = math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            for posting in postings:
                if posting.doc not in allowed:
                    continue
                norm = 1 - _B + _B * self._lengths[posting.doc] / self._avg_length
                scores[posting.doc] += (
                    penalty * idf * (posting.weight * (_K1 + 1)) / (posting.weight + _K1 * norm)
                )
        return scores

    def _by_trigram(
        self, query: SearchQuery, allowed: set[int], seen: set[str]
    ) -> list[SearchHit]:
        query_grams = trigrams(query.text)
        if not query_grams:
            return []

        scored: list[tuple[float, int]] = []
        for doc in allowed:
            if self.products[doc].sku_1c in seen:
                continue
            grams = self._name_trigrams[doc]
            overlap = len(query_grams & grams)
            if not overlap:
                continue
            similarity = overlap / len(query_grams | grams)
            if similarity >= 0.25:
                scored.append((similarity, doc))

        scored.sort(key=lambda item: (-item[0], self.products[item[1]].name))
        return [
            SearchHit(self.products[doc], score, "trigram", None, query.audience, query.text)
            for score, doc in scored
        ]


def apply_text_filters(query: SearchQuery) -> SearchQuery:
    """Вынимает из запроса условия по цене и наличию и убирает их из текста.

    «мячи в наличии до 2000 руб» -> текст «мячи», фильтры in_stock + price_max.
    """
    text = query.text
    in_stock = query.in_stock_only
    price_min, price_max = query.price_min, query.price_max

    if _IN_STOCK_HINT.search(text):
        in_stock = True
        text = _IN_STOCK_HINT.sub(" ", text)

    for pattern, setter in ((_PRICE_MAX, "max"), (_PRICE_MIN, "min")):
        match = pattern.search(text)
        if not match:
            continue
        value = _to_rubles(match.group(0), match.group(1))
        if value is None:
            continue
        if setter == "max" and price_max is None:
            price_max = value
        elif setter == "min" and price_min is None:
            price_min = value
        text = text[: match.start()] + " " + text[match.end() :]

    return SearchQuery(
        text=" ".join(text.split()),
        limit=query.limit,
        in_stock_only=in_stock,
        price_min=price_min,
        price_max=price_max,
        root=query.root,
        norm_doc_id=query.norm_doc_id,
        norm_code=query.norm_code,
        audience=query.audience,
    )


def _diversified(hits: list[SearchHit]) -> list[SearchHit]:
    """Перемешивает выдачу так, чтобы подряд не шло одно и то же.

    На «чем оснастить спортзал в саду» бот показывал пять обручей: обруч 60,
    обруч 70, обруч 60 салатовый… Формально это лучшие совпадения, но человеку
    нужен зал, а не витрина обручей. Порядок по релевантности сохраняется внутри
    групп, наверх выносится по одному представителю от каждой.
    """
    groups: dict[str, list[SearchHit]] = defaultdict(list)
    order: list[str] = []
    for hit in hits:
        key = _family(hit.product.name)
        if key not in groups:
            order.append(key)
        groups[key].append(hit)

    result: list[SearchHit] = []
    while len(result) < len(hits):
        for key in order:
            if groups[key]:
                result.append(groups[key].pop(0))
    return result


def _family(name: str) -> str:
    """Товарная «семья» — по первым значимым словам названия.

    Названия у заказчика начинаются с кода поставщика: «БОС Обруч 60 см».
    Поэтому берём первые два осмысленных слова, а не одно.
    """
    words = [word for word in stems(name) if len(word) > 2 and not word.isdigit()]
    return " ".join(words[:2]) if words else name.lower()


def _to_rubles(phrase: str, digits: str) -> int | None:
    try:
        value = int(digits.replace(" ", ""))
    except ValueError:
        return None
    return value * 1000 if _THOUSANDS.search(phrase) else value


def _is_code_only(text: str, codes: list[str]) -> bool:
    """В запросе нет ничего, кроме номеров пунктов и служебных слов."""
    if not codes:
        return False
    rest = text
    for code in codes:
        rest = rest.replace(code, " ")
    words = [w for w in stems(rest) if not w.isdigit() and w not in _STOPWORDS]
    return not words or words == ["пункт"] or words == ["п"]


def _fields(product: Product) -> list[tuple[str, str]]:
    return [
        ("name", product.name),
        ("category", " ".join(" ".join(path) for path in product.category_paths)),
        ("kit", " ".join(product.kit_contents)),
        ("description", product.description),
    ]
