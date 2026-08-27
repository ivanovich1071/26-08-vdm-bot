"""Корзина, заказ и данные клиента."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class CartItem:
    sku_1c: str
    name: str
    price: int | None
    quantity: int
    url: str | None = None
    norm_citation: str | None = None

    @property
    def total(self) -> int:
        return (self.price or 0) * self.quantity


@dataclass
class Cart:
    """Корзина привязана к пользователю, а не к каналу.

    Один человек, начавший подбор в виджете на сайте и продолживший в Telegram,
    должен видеть тот же заказ.
    """

    user_id: str
    items: list[CartItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(item.total for item in self.items)

    @property
    def count(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def is_empty(self) -> bool:
        return not self.items

    def find(self, sku_1c: str) -> CartItem | None:
        return next((item for item in self.items if item.sku_1c == sku_1c), None)

    def add(self, item: CartItem) -> CartItem:
        existing = self.find(item.sku_1c)
        if existing is None:
            self.items.append(item)
            return item
        existing.quantity += item.quantity
        return existing

    def set_quantity(self, sku_1c: str, quantity: int) -> CartItem | None:
        item = self.find(sku_1c)
        if item is None:
            return None
        if quantity <= 0:
            self.items.remove(item)
            return None
        item.quantity = quantity
        return item

    def clear(self) -> None:
        self.items.clear()


@dataclass
class Customer:
    """Контакты для передачи заказа менеджеру.

    Собираются только после согласия на обработку персональных данных и только те,
    без которых заказ не обработать.
    """

    name: str = ""
    phone: str = ""
    email: str = ""
    organization: str = ""
    region: str = ""
    comment: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.name and (self.phone or self.email))


@dataclass
class Order:
    id: str
    user_id: str
    channel: str  # telegram | web | max
    items: list[CartItem]
    customer: Customer
    created_at: str
    consent_id: str | None = None
    status: str = "new"  # new | sent | failed
    delivery_attempts: int = 0
    last_error: str | None = None

    @property
    def total(self) -> int:
        return sum(item.total for item in self.items)

    @classmethod
    def create(cls, cart: Cart, customer: Customer, channel: str, consent_id: str | None) -> Order:
        return cls(
            id=f"VDM-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
            user_id=cart.user_id,
            channel=channel,
            items=[
                CartItem(
                    sku_1c=item.sku_1c,
                    name=item.name,
                    price=item.price,
                    quantity=item.quantity,
                    url=item.url,
                    norm_citation=item.norm_citation,
                )
                for item in cart.items
            ],
            customer=customer,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            consent_id=consent_id,
        )
