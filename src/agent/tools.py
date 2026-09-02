"""Инструменты агента.

Агент не выдумывает товары, цены и нормативные основания — он получает их только
через эти вызовы. Всё, что вернулось из инструмента, взято из выгрузки 1С, поэтому
любую цифру в ответе можно проследить до источника.

Действия, меняющие состояние (корзина, оформление), тоже идут через инструменты,
но оформление заказа агент только начинает: подтверждает его пользователь кнопкой.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

from catalog.search import SearchQuery
from core.ui import price_text, stock_text
from norms import documents as norm_docs
from norms import extract as norm_extract
from norms import reference
from norms.extract import document_ids_in_text

MAX_RESULTS = 8

# Инструменты, чьи вызовы попадают в журнал хода: по ним разбирают сбои
# «нашёл, но не то».
_NORM_TOOLS = frozenset({"find_by_norm_code", "find_norm_item", "explain_norm"})


class _PlainItem(NamedTuple):
    """Пункт, о котором известно только из привязки каталога, без текста приказа."""

    doc_id: str
    code: str
    title: str
    section: str | None = None


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Поиск товаров в каталоге по словам. Возвращает название, цену, наличие "
                "и нормативное основание. Используй для любого предметного запроса."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Что ищем, своими словами"},
                    "in_stock_only": {"type": "boolean"},
                    "price_max": {"type": "integer", "description": "Верхняя граница цены, ₽"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_by_norm_code",
            "description": (
                "Товары по номеру пункта нормативного перечня: «2.1.14», «2.20.63». "
                "Можно указать подраздел целиком — «2.4», тогда вернутся все его позиции. "
                "Документ указывай всегда, когда он известен: один и тот же номер есть "
                "в разных приказах и означает в них разное."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "document": {
                        "type": "string",
                        "description": (
                            "order_838, order_1057 — либо просто «838», «1057». "
                            "Не указан — берётся из разговора."
                        ),
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_norm_item",
            "description": (
                "Пункт перечня по смыслу или по номеру — из текста самого приказа. "
                "Вызывай, прежде чем называть номер пункта: «какой пункт про спортивное "
                "оборудование в приказе 1057» вернёт 1.5.1, а «2.1.14 в 1057» честно "
                "ответит, что такого пункта в этом приказе нет. Формулировку пункта "
                "бери отсюда, а не по памяти."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Слова или номер пункта: «спортивный инвентарь», «1.5.1»",
                    },
                    "document": {
                        "type": "string",
                        "description": "order_838, order_1057 — либо просто «838», «1057»",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_norm",
            "description": (
                "Справка по нормативному документу: что это, кого касается, как устроен "
                "перечень и сколько позиций каталога к нему привязано. Вызывай, когда "
                "спрашивают про сам документ — «что значит приказ 838», «на основании "
                "чего обязаны укомплектовать». Отвечай текстом справки, не пересказывая "
                "документ по памяти."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document": {
                        "type": "string",
                        "description": (
                            "order_838, order_1057, fgos_do, fop_do, func_kits — "
                            "либо просто «838», «1057», «ФГОС ДО»"
                        ),
                    }
                },
                "required": ["document"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Полная карточка товара по коду 1С, включая состав комплекта.",
            "parameters": {
                "type": "object",
                "properties": {"sku_1c": {"type": "string"}},
                "required": ["sku_1c"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Добавить товар в корзину. Только после согласия пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sku_1c": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                },
                "required": ["sku_1c"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cart",
            "description": "Что сейчас в корзине пользователя и на какую сумму.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_manager",
            "description": (
                "Позвать живого менеджера: вопрос вне каталога, нестандартные условия, "
                "нужен расчёт или документы."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


class ToolBox:
    """Исполнение инструментов поверх каталога и корзины пользователя."""

    def __init__(self, engine, session) -> None:  # noqa: ANN001 — циклический импорт
        self.engine = engine
        self.session = session
        self.shown_skus: list[str] = []
        self.handoff_reason: str | None = None
        # Все суммы, которые инструменты вернули за этот ход. По ним проверяется
        # ответ модели: цены, которой здесь нет, в ответе быть не должно.
        self.prices: set[int] = set()
        # То же для нормативных оснований: пары «приказ, пункт». Цены проверялись
        # с самого начала, а основания доходили до человека непроверенными — так
        # и прошло «Спортивный комплекс соответствует пункту 2.20.63 приказа
        # 1057», где 2.20.63 это фрезерный станок из приказа 838.
        self.norm_refs: set[tuple[str, str]] = set()
        # Нормативные вызовы этого хода — для журнала. «Нашёл, но не то» иначе не
        # отличить от нормального ответа: в записи видно только текст, а по нему
        # не понять, какой приказ спрашивали и что вернул поиск.
        self.norm_lookups: list[dict[str, Any]] = []

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)
        try:
            result = handler(**arguments)
        except TypeError as exc:
            return json.dumps({"error": f"неверные аргументы: {exc}"}, ensure_ascii=False)
        self._remember_lookup(name, arguments, result)
        return json.dumps(result, ensure_ascii=False)

    def _remember_lookup(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        """Нормативный вызов — в журнал: что спросили и что вернулось."""
        if name not in _NORM_TOOLS:
            return
        self.norm_lookups.append(
            {
                "tool": name,
                "code": arguments.get("code") or arguments.get("query"),
                "document": arguments.get("document"),
                "found": result.get("found") if isinstance(result, dict) else None,
            }
        )

    # --- Реализация инструментов ------------------------------------------

    def _search_products(
        self,
        query: str,
        in_stock_only: bool = False,
        price_max: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        # Аудиторию берём из профиля разговора: без неё поиск для детского сада
        # поднимал школьные позиции и обосновывал их школьным приказом.
        hits = self.engine.index.search(
            SearchQuery(
                text=query,
                in_stock_only=in_stock_only,
                price_max=price_max,
                limit=min(limit, MAX_RESULTS),
                audience=self.session.profile.audience,
            )
        )
        self.session.last_hits = hits
        return {"found": len(hits), "products": [self._brief(hit) for hit in hits]}

    def _find_by_norm_code(self, code: str, document: str | None = None) -> dict[str, Any]:
        code = norm_extract.normalize_code(code)
        doc_id = self._document_for(document)
        # Пункта нет в запрошенном приказе — так и говорим. Раньше поиск шёл по
        # голому номеру, и «2.1.14 по приказу 1057» возвращал школьную речевую
        # игру: номер совпал, приказ — нет. Человек видел садовскую рекомендацию
        # со школьным основанием и справедливо считал, что бот всё перепутал.
        elsewhere = self._where_code_lives(code)
        if doc_id and elsewhere and doc_id not in elsewhere:
            return self._code_not_in_document(code, doc_id, elsewhere)

        hits = self.engine.index.search(
            SearchQuery(
                text=code,
                norm_code=code,
                norm_doc_id=doc_id,
                limit=MAX_RESULTS,
                audience=self.session.profile.audience,
            )
        )
        self.session.last_hits = hits
        if not hits:
            note = f"В каталоге нет товаров, привязанных к пункту {code}"
            note += f" — {norm_docs.get(doc_id).short_name}." if doc_id else "."
            return {"found": 0, "note": note}

        result: dict[str, Any] = {
            "found": len(hits),
            "products": [self._brief(hit) for hit in hits],
        }
        # Как пункт называется в самом перечне — и в каком именно перечне.
        # Голая формулировка без имени документа однажды уже привела к тому,
        # что текст из приказа 838 был выдан пользователю за пункт 1057.
        item = self._norm_item(hits, code, doc_id)
        if item is not None:
            result["norm_item_title"] = item.title
            result["norm_item_document"] = norm_docs.get(item.doc_id).short_name
        return result

    def _find_norm_item(self, query: str, document: str | None = None) -> dict[str, Any]:
        index = self.engine.norm_texts
        if not index.loaded:
            return {"error": "тексты приказов не загружены, формулировку пункта уточнит менеджер"}

        doc_id = self._document_for(document)
        code = norm_extract.codes_in_query(query)
        if code:
            return self._norm_item_by_code(code[0], doc_id)

        found = index.search(query, doc_id, limit=5)
        if not found:
            return {"found": 0, "note": "В текстах приказов ничего похожего не нашлось."}
        return {"found": len(found), "items": [self._item_brief(item) for item in found]}

    def _norm_item_by_code(self, code: str, doc_id: str | None) -> dict[str, Any]:
        index = self.engine.norm_texts
        homes = index.documents_with(code)
        if doc_id and doc_id not in homes:
            answer: dict[str, Any] = {
                "found": 0,
                "note": (
                    f"Пункта {code} нет: {norm_docs.get(doc_id).short_name} такого номера не содержит."
                ),
            }
            if homes:
                other = index.get(homes[0], code)
                answer["also_in"] = self._item_brief(other) if other else None
            return answer
        for home in [doc_id] if doc_id else homes:
            item = index.get(home, code) if home else None
            if item is not None:
                return {"found": 1, "items": [self._item_brief(item)]}
        return {"found": 0, "note": f"Пункта {code} нет ни в одном из разобранных приказов."}

    def _item_brief(self, item) -> dict[str, Any]:  # noqa: ANN001 — norms.items.NormItem
        self.norm_refs.add((item.doc_id, item.code))
        brief = {
            "code": item.code,
            "title": item.title,
            "document": norm_docs.get(item.doc_id).short_name,
            "document_id": item.doc_id,
        }
        if item.section:
            brief["section"] = item.section
        return brief

    def _norm_item(self, hits, code: str, doc_id: str | None):  # noqa: ANN001, ANN201
        """Пункт, по которому нашлись товары, — из текста приказа или из привязки.

        Документ определяется по самой находке, а не по догадке: иначе
        формулировка одного приказа снова уедет в ответ про другой.
        """
        home = doc_id
        if home is None:
            home = next(
                (hit.matched_doc_id for hit in hits if hit.matched_doc_id and hit.matched_code == code),
                None,
            )
        if home is None:
            return None
        item = self.engine.norm_texts.get(home, code)
        if item is not None:
            self.norm_refs.add((home, code))
            return item
        # Текста приказа нет — берём формулировку из привязки каталога.
        for hit in hits:
            for ref in hit.product.norms:
                if ref.doc_id == home and ref.item_code == code and ref.item_title:
                    self.norm_refs.add((home, code))
                    return _PlainItem(home, code, ref.item_title)
        return None

    def _where_code_lives(self, code: str) -> list[str]:
        """Приказы, в которых такой пункт есть, — по текстам и по каталогу."""
        from_texts = self.engine.norm_texts.documents_with(code)
        return from_texts or self.engine.index.documents_with_code(code)

    def _code_not_in_document(
        self, code: str, doc_id: str, elsewhere: list[str]
    ) -> dict[str, Any]:
        names = ", ".join(norm_docs.get(other).short_name for other in elsewhere)
        answer: dict[str, Any] = {
            "found": 0,
            "note": (
                f"Пункта {code} нет: {norm_docs.get(doc_id).short_name} такого номера не содержит. "
                f"Такой номер есть в другом документе — {names}."
            ),
        }
        item = self.engine.norm_texts.get(elsewhere[0], code)
        if item is not None:
            answer["also_in"] = self._item_brief(item)
        return answer

    def _document_for(self, document: str | None) -> str | None:
        """Приказ, по которому подбираем: названный моделью или взятый из разговора."""
        if document:
            return _document_id(document)
        named = self.session.profile.norm_doc_ids
        return named[0] if len(named) == 1 else None

    def _explain_norm(self, document: str) -> dict[str, Any]:
        doc_id = _document_id(document)
        if doc_id is None:
            return {
                "error": f"документ «{document}» не распознан",
                "known": reference.known_documents(),
            }
        return {
            "document": doc_id,
            "reference": reference.explain(
                doc_id,
                reference.coverage(
                    self.engine.index, doc_id, self.engine.norm_texts.count(doc_id)
                ),
            ),
        }

    def _get_product(self, sku_1c: str) -> dict[str, Any]:
        product = self.engine.index.get(sku_1c)
        if product is None:
            return {"error": f"товара с кодом {sku_1c} нет в каталоге"}
        self.shown_skus.append(sku_1c)
        self._remember_price(product.price)
        self._remember_norms(product)
        norm = product.norm_for(self.session.profile.audience, self.session.profile.room or "")
        return {
            "sku_1c": product.sku_1c,
            "name": product.name,
            "price": price_text(product.price),
            "stock": stock_text(product),
            "url": product.url,
            "category": product.category_paths[0] if product.category_paths else [],
            "kit_contents": product.kit_contents[:20],
            "description": product.description[:1200],
            "norm": norm.citation if norm else None,
        }

    def _add_to_cart(self, sku_1c: str, quantity: int = 1) -> dict[str, Any]:
        product = self.engine.index.get(sku_1c)
        if product is None:
            return {"error": f"товара с кодом {sku_1c} нет в каталоге"}
        self.engine._add(self.session, sku_1c, max(1, quantity))
        cart = self.engine.storage.load_cart(self.session.user_id)
        return {"added": product.name, "cart_count": cart.count, "cart_total": cart.total}

    def _get_cart(self) -> dict[str, Any]:
        cart = self.engine.storage.load_cart(self.session.user_id)
        self._remember_price(cart.total)
        for item in cart.items:
            self._remember_price(item.price)
            self._remember_price(item.total)
        return {
            "items": [
                {"sku_1c": i.sku_1c, "name": i.name, "quantity": i.quantity, "sum": i.total}
                for i in cart.items
            ],
            "total": cart.total,
        }

    def _handoff_to_manager(self, reason: str) -> dict[str, Any]:
        self.handoff_reason = reason
        return {"ok": True, "contact": self.engine.settings.manager_contact}

    def _brief(self, hit) -> dict[str, Any]:  # noqa: ANN001
        product = hit.product
        self.shown_skus.append(product.sku_1c)
        self._remember_price(product.price)
        self._remember_norms(product)
        return {
            "sku_1c": product.sku_1c,
            "name": product.name,
            "price": price_text(product.price),
            "stock": stock_text(product),
            "norm": hit.citation(),
            "url": product.url,
        }

    def _remember_norms(self, product) -> None:  # noqa: ANN001 — catalog.models.Product
        """Основания показанного товара — то, на что модель вправе сослаться."""
        for ref in product.norms:
            if ref.item_code:
                self.norm_refs.add((ref.doc_id, ref.item_code))

    def _remember_price(self, price: int | None) -> None:
        if price:
            self.prices.add(int(price))
        # Цена в ответе почти всегда стоит с разделителем разрядов, а бывает —
        # округлённой до тысяч. Оба написания читаются как одна и та же сумма,
        # и придирка к формату превратила бы проверку в источник ложных тревог.




def _document_id(name: str) -> str | None:
    """Идентификатор документа по тому, как его назвала модель.

    Модель зовёт документ и кодом, и номером, и словами — принимаем всё,
    иначе инструмент отвечает ошибкой на осмысленный вызов.
    """
    raw = (name or "").strip().lower()
    if raw in norm_docs.DOCUMENTS:
        return raw
    found = document_ids_in_text(raw) or document_ids_in_text(f"приказ {raw}")
    return found[0] if found else None
