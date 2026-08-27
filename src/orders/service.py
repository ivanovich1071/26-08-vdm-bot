"""Оформление заказа: сохранить, отправить, при сбое — повторить."""

from __future__ import annotations

import logging
from pathlib import Path

from core.config import Settings
from core.models import Cart, Customer, Order
from core.storage import Storage
from orders.sinks import (
    Bitrix24Sink,
    CompositeSink,
    GoogleSheetsSink,
    JsonlSink,
    OrderSink,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


def build_sink(settings: Settings) -> OrderSink:
    """Приёмник по конфигурации. Локальный файл всегда включён как дубль.

    Пока CRM недоступна, единственный внешний приёмник — Google Sheets. Если он
    отвалится, заказ всё равно окажется в файле и в базе, и его не придётся искать
    по логам.
    """
    fallback = JsonlSink(path=Path(settings.orders_jsonl_path))
    if settings.order_sink == "google_sheets":
        if not settings.google_sheets_id:
            log.warning("ORDER_SINK=google_sheets, но GOOGLE_SHEETS_ID пуст — пишем в файл")
            return fallback
        return CompositeSink(
            [
                GoogleSheetsSink(
                    spreadsheet_id=settings.google_sheets_id,
                    credentials_file=settings.google_credentials_file,
                ),
                fallback,
            ]
        )
    if settings.order_sink == "bitrix24":
        return CompositeSink([Bitrix24Sink(webhook_url=""), fallback])
    return fallback


class OrderService:
    def __init__(self, storage: Storage, sink: OrderSink) -> None:
        self.storage = storage
        self.sink = sink

    def submit(self, cart: Cart, customer: Customer, channel: str) -> Order:
        """Создаёт заказ и пытается отправить.

        Согласие проверяется здесь, а не в адаптере: канал не должен уметь обходить
        это правило.
        """
        consent_id = self.storage.active_consent(cart.user_id)
        if consent_id is None:
            raise PermissionError(
                "Нет действующего согласия на обработку персональных данных: "
                "заказ не оформляется."
            )
        if cart.is_empty:
            raise ValueError("Корзина пуста")

        order = Order.create(cart, customer, channel, consent_id)
        self.storage.save_order(order)
        self._deliver(order)
        cart.clear()
        self.storage.save_cart(cart)
        return order

    def retry_pending(self) -> int:
        """Повторная отправка залежавшихся заказов. Вызывается планировщиком."""
        sent = 0
        for order in self.storage.pending_orders():
            if order.delivery_attempts >= MAX_ATTEMPTS:
                continue
            if self._deliver(order):
                sent += 1
        return sent

    def _deliver(self, order: Order) -> bool:
        order.delivery_attempts += 1
        try:
            self.sink.push(order)
        except Exception as exc:
            order.status = "failed"
            order.last_error = str(exc)
            log.error("Заказ %s не доставлен (%s попытка): %s", order.id, order.delivery_attempts, exc)
            self.storage.save_order(order)
            return False
        order.status = "sent"
        order.last_error = None
        self.storage.save_order(order)
        log.info("Заказ %s отправлен в %s", order.id, getattr(self.sink, "name", "sink"))
        return True
