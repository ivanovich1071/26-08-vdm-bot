"""Куда уходит сформированный заказ.

Целевая точка — CRM заказчика, но доступа к ней пока нет. Чтобы это не блокировало
работу, приёмник вынесен за интерфейс: сейчас включён Google Sheets, при появлении
доступа к CRM меняется одна строка конфигурации, а не код бота.

Заказ всегда сначала сохраняется в своей базе и только потом отправляется наружу,
поэтому недоступность Google не теряет заказ — он уйдёт при следующей попытке.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.models import Order

HEADERS = [
    "Дата",
    "Номер заказа",
    "Канал",
    "Организация",
    "Контактное лицо",
    "Телефон",
    "E-mail",
    "Регион",
    "Код 1С",
    "Наименование",
    "Кол-во",
    "Цена",
    "Сумма",
    "Нормативное основание",
    "Ссылка на товар",
    "Комментарий",
]


def order_rows(order: Order) -> list[list[str]]:
    """Заказ разворачивается в строки — по одной на позицию.

    Так менеджеру удобнее: можно фильтровать и сводить по товарам, не разбирая
    вложенные структуры.
    """
    customer = order.customer
    head = [
        order.created_at,
        order.id,
        order.channel,
        customer.organization,
        customer.name,
        customer.phone,
        customer.email,
        customer.region,
    ]
    return [
        [
            *head,
            item.sku_1c,
            item.name,
            str(item.quantity),
            "" if item.price is None else str(item.price),
            str(item.total),
            item.norm_citation or "",
            item.url or "",
            customer.comment,
        ]
        for item in order.items
    ]


class OrderSink(Protocol):
    name: str

    def push(self, order: Order) -> None:
        """Отправить заказ. Исключение означает «повторить позже»."""
        ...


@dataclass
class JsonlSink:
    """Локальный приёмник: демонстрация и запасной вариант, когда внешних доступов нет."""

    path: Path = Path("data/orders.jsonl")
    name: str = "jsonl"

    def push(self, order: Order) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for row in order_rows(order):
                fh.write(json.dumps(dict(zip(HEADERS, row, strict=True)), ensure_ascii=False) + "\n")


@dataclass
class GoogleSheetsSink:
    """Таблица заказов вместо CRM.

    Работает через сервисный аккаунт Google: файл ключа и идентификатор таблицы
    задаются в окружении. Заголовок создаётся один раз при первой записи.
    """

    spreadsheet_id: str
    credentials_file: str
    worksheet: str = "Заказы"
    name: str = "google_sheets"

    def push(self, order: Order) -> None:
        sheet = self._worksheet()
        if not sheet.acell("A1").value:
            sheet.append_row(HEADERS, value_input_option="RAW")
        sheet.append_rows(order_rows(order), value_input_option="USER_ENTERED")

    def _worksheet(self):  # noqa: ANN202 — тип из gspread, импортируется лениво
        import gspread  # локальный импорт: прототип запускается и без Google

        client = gspread.service_account(filename=self.credentials_file)
        book = client.open_by_key(self.spreadsheet_id)
        try:
            return book.worksheet(self.worksheet)
        except Exception:
            return book.add_worksheet(self.worksheet, rows=1000, cols=len(HEADERS))


@dataclass
class Bitrix24Sink:
    """Заглушка под CRM заказчика: включится, когда дадут вебхук."""

    webhook_url: str
    name: str = "bitrix24"

    def push(self, order: Order) -> None:
        raise NotImplementedError(
            "Интеграция с Битрикс24 не включена: нужен вебхук и согласованный состав "
            "полей сделки. До этого заказы уходят в Google Sheets."
        )


@dataclass
class CompositeSink:
    """Несколько приёмников сразу: основной и дублирующий.

    Если основной не ответил, а дублирующий записал, заказ всё равно считается
    доставленным — но ошибка попадает в лог, чтобы её было видно.
    """

    sinks: list[OrderSink]
    name: str = "composite"

    def push(self, order: Order) -> None:
        errors: list[str] = []
        delivered = False
        for sink in self.sinks:
            try:
                sink.push(order)
                delivered = True
            except Exception as exc:
                errors.append(f"{sink.name}: {exc}")
        if not delivered:
            raise RuntimeError("; ".join(errors) or "нет ни одного приёмника заказов")
