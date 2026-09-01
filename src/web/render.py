"""Рендер примитивов ядра для виджета на сайте.

В виджете карточек товара нет — по договорённости с заказчиком это консультант
с корзиной заказа, а не витрина. Поэтому список позиций отдаётся строками, а корзина
выглядит как заявка. Данные при этом те же, что в Telegram: расходиться каналы
не должны.
"""

from __future__ import annotations

from typing import Any

from core.ui import (
    Keyboard,
    Message,
    OrderSummary,
    ProductCard,
    ProductList,
    Response,
    price_text,
    stock_text,
)


def to_json(responses: list[Response]) -> list[dict[str, Any]]:
    return [_one(response) for response in responses]


def _one(response: Response) -> dict[str, Any]:
    keyboard = _keyboard(getattr(response, "keyboard", None))

    if isinstance(response, Message):
        return {"type": "text", "text": response.text, "actions": keyboard}

    if isinstance(response, ProductCard):
        product = response.product
        return {
            "type": "item",
            "text": product.name,
            "meta": f"{price_text(product.price)} · {stock_text(product)}",
            "norm": response.citation,
            "url": product.url,
            "sku": product.sku_1c,
            # Свой файл идёт первым: до vdm.ru браузер посетителя может и не
            # достучаться, а до нас он уже достучался — виджет с нашей страницы.
            "image": f"/media/{product.sku_1c}" if response.image_path else response.image,
            # Все основания с формулировками пунктов приказа — то же, что в Telegram.
            "norms": response.norms,
            "attributes": product.attributes,
            "description": product.description,
            "kit": product.kit_contents,
            "actions": keyboard,
        }

    if isinstance(response, ProductList):
        return {
            "type": "list",
            "title": response.title,
            "total": response.total_found,
            "items": [
                {
                    "sku": card.product.sku_1c,
                    "text": card.product.name,
                    "meta": f"{price_text(card.product.price)} · {stock_text(card.product)}",
                    "norm": card.citation,
                    "url": card.product.url,
                    "actions": _keyboard(card.keyboard),
                }
                for card in response.cards
            ],
            "actions": keyboard,
        }

    if isinstance(response, OrderSummary):
        return {
            "type": "order",
            "lines": [
                {
                    "name": line.name,
                    "quantity": line.quantity,
                    "price": price_text(line.price),
                    "sku": line.sku_1c,
                    "norm": line.norm_citation,
                }
                for line in response.lines
            ],
            "total": price_text(response.total),
            "note": response.note,
            "actions": keyboard,
        }

    return {"type": "text", "text": str(response), "actions": keyboard}


def _keyboard(keyboard: Keyboard | None) -> list[list[dict[str, str | None]]]:
    if keyboard is None:
        return []
    return [
        [{"title": b.title, "action": b.action, "url": b.url} for b in row]
        for row in keyboard.rows
    ]
