"""Доменные типы каталога.

Общие для поиска, агента, адаптеров и заказа: ядро и адаптеры обмениваются именно
этими объектами, а не сырыми строками выгрузки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormRef:
    """Нормативное основание: документ и, если известен, пункт перечня."""

    doc_id: str
    doc_citation: str
    item_code: str | None
    item_title: str | None
    source: str
    confidence: float

    @property
    def citation(self) -> str:
        if self.item_code:
            return f"позиция {self.item_code} — {self.doc_citation}"
        return self.doc_citation

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NormRef:
        return cls(
            doc_id=raw["doc_id"],
            doc_citation=raw["doc_citation"],
            item_code=raw.get("item_code"),
            item_title=raw.get("item_title"),
            source=raw.get("source", ""),
            confidence=float(raw.get("confidence", 0.0)),
        )


# Корневые разделы каталога заказчика и то, кому они адресованы. «Оснащение
# новостроек» отнесено к садам не на глаз: все 1 048 позиций этой ветки привязаны
# к приказу № 1057. «Коррекционная среда» и «Инновационные решения» смешанные,
# поэтому их здесь нет — они не поднимаются и не опускаются.
_AUDIENCE_BY_ROOT = {
    "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА": "preschool",
    "ОСНАЩЕНИЕ НОВОСТРОЕК": "preschool",
    "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838": "school",
}


def _relevance(ref: NormRef, query: str) -> tuple[int, int, float]:
    """Насколько пункт отвечает на заданный вопрос.

    Порядок ключей: сначала есть ли вообще номер пункта, потом совпадение слов
    названия пункта со словами запроса, и только затем надёжность привязки.
    """
    overlap = 0
    if query and ref.item_title:
        from catalog.text import stems

        words = set(stems(query))
        overlap = len(words & set(stems(ref.item_title)))
    return (ref.item_code is not None, overlap, ref.confidence)


@dataclass(frozen=True)
class Product:
    sku_1c: str
    name: str
    url: str | None
    short_url: str | None
    price: int | None
    currency: str
    in_stock: int
    category_paths: list[list[str]]
    description: str
    kit_contents: list[str]
    norms: list[NormRef]
    bitrix_id: int | None
    images: list[str] = field(default_factory=list)
    # Характеристики со страницы товара: страна, сертификат. В выгрузке 1С их нет,
    # они добираются тем же проходом, что и фотографии.
    attributes: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""

    @property
    def roots(self) -> list[str]:
        return [path[0] for path in self.category_paths if path]

    @property
    def available(self) -> bool:
        return self.in_stock > 0

    @property
    def norm_codes(self) -> list[str]:
        return [ref.item_code for ref in self.norms if ref.item_code]

    @property
    def audiences(self) -> set[str]:
        """Кому товар предназначен — по веткам каталога, в которых он лежит.

        Товар часто лежит сразу в нескольких: садовский комплект попадает и в
        школьный раздел. Поэтому это множество, а не одно значение.
        """
        found: set[str] = set()
        for root in self.roots:
            audience = _AUDIENCE_BY_ROOT.get(root.strip().upper())
            if audience:
                found.add(audience)
        return found

    def norms_for(self, audience: str | None = None) -> list[NormRef]:
        """Основания, уместные этому собеседнику.

        Сайт заказчика кладёт один товар в несколько веток сразу: садовские
        карточки по лексическим темам стоят ещё и в школьном разделе «по приказу
        № 838». Раньше это приводило к тому, что на вопрос про детский сад бот
        цитировал школьный приказ.

        Чужой перечень не подменяется своим и не добирается «хоть какой-нибудь»:
        школьный пункт не является основанием для детского сада, и честнее не
        назвать основание вовсе, чем назвать чужое.
        """
        from norms import documents as docs

        allowed = docs.for_audience(audience)
        return [ref for ref in self.norms if ref.doc_id in allowed]

    def norm_for(self, audience: str | None = None, query: str = "") -> NormRef | None:
        """Одно основание для строки выдачи.

        У товара их бывает несколько — «КМО 2024 Классификация» закрывает и
        пункт 2.4.35, и 4.4.17. Показываем тот, что ближе к запросу: спросили про
        логопеда — назовём логопедический пункт, а не пункт про аутизм.
        """
        candidates = self.norms_for(audience)
        if not candidates:
            return None
        return max(candidates, key=lambda ref: _relevance(ref, query))

    def best_norm(self) -> NormRef | None:
        """Основание без учёта собеседника. Остаётся для мест, где его негде взять."""
        return self.norm_for(None)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Product:
        return cls(
            sku_1c=raw["sku_1c"],
            name=raw["name"],
            url=raw.get("url"),
            short_url=raw.get("short_url"),
            price=raw.get("price"),
            currency=raw.get("currency", "RUB"),
            in_stock=raw.get("in_stock", 0),
            category_paths=raw.get("category_paths", []),
            description=raw.get("description", ""),
            kit_contents=raw.get("kit_contents", []),
            norms=[NormRef.from_dict(n) for n in raw.get("norms", [])],
            bitrix_id=raw.get("bitrix_id"),
            images=raw.get("images", []),
            attributes=raw.get("attributes", {}),
            updated_at=raw.get("updated_at", ""),
        )
