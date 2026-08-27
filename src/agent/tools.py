"""Инструменты агента.

Агент не выдумывает товары, цены и нормативные основания — он получает их только
через эти вызовы. Всё, что вернулось из инструмента, взято из выгрузки 1С, поэтому
любую цифру в ответе можно проследить до источника.

Действия, меняющие состояние (корзина, оформление), тоже идут через инструменты,
но оформление заказа агент только начинает: подтверждает его пользователь кнопкой.
"""

from __future__ import annotations

import json
from typing import Any

from catalog.search import SearchQuery
from core.ui import price_text, stock_text

MAX_RESULTS = 8

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
                "Можно указать подраздел целиком — «2.4», тогда вернутся все его позиции."
            ),
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
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

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, f"_{name}", None)
        if handler is None:
            return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)
        try:
            return json.dumps(handler(**arguments), ensure_ascii=False)
        except TypeError as exc:
            return json.dumps({"error": f"неверные аргументы: {exc}"}, ensure_ascii=False)

    # --- Реализация инструментов ------------------------------------------

    def _search_products(
        self,
        query: str,
        in_stock_only: bool = False,
        price_max: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        hits = self.engine.index.search(
            SearchQuery(
                text=query,
                in_stock_only=in_stock_only,
                price_max=price_max,
                limit=min(limit, MAX_RESULTS),
            )
        )
        self.session.last_hits = hits
        return {"found": len(hits), "products": [self._brief(hit) for hit in hits]}

    def _find_by_norm_code(self, code: str) -> dict[str, Any]:
        hits = self.engine.index.search(SearchQuery(text=code, norm_code=code, limit=MAX_RESULTS))
        self.session.last_hits = hits
        if not hits:
            return {
                "found": 0,
                "note": f"В каталоге нет товаров, привязанных к пункту {code}.",
            }
        return {"found": len(hits), "products": [self._brief(hit) for hit in hits]}

    def _get_product(self, sku_1c: str) -> dict[str, Any]:
        product = self.engine.index.get(sku_1c)
        if product is None:
            return {"error": f"товара с кодом {sku_1c} нет в каталоге"}
        self.shown_skus.append(sku_1c)
        norm = product.best_norm()
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
        return {
            "sku_1c": product.sku_1c,
            "name": product.name,
            "price": price_text(product.price),
            "stock": stock_text(product),
            "norm": hit.citation(),
            "url": product.url,
        }
