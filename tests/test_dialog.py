import json

import pytest

from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import CHECKOUT_FIELDS, DialogEngine
from core.storage import Storage
from core.ui import Message, OrderSummary, ProductList
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"


def product(sku, name, price, norms=()):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": price,
            "currency": "RUB",
            "in_stock": 3,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
            "description": "",
            "kit_contents": [],
            "norms": [
                {
                    "doc_id": "order_838",
                    "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
                    "item_code": code,
                    "item_title": None,
                    "source": "heading",
                    "confidence": 0.9,
                }
                for code in norms
            ],
            "bitrix_id": None,
            "url": f"https://vdm.ru/{sku}",
            "short_url": None,
        }
    )


@pytest.fixture
def engine(tmp_path):
    index = CatalogIndex(
        [
            product("S1", "Фрезерный станок с ЧПУ", 253000, norms=["2.20.63"]),
            product("S2", "Мяч баскетбольный", 908, norms=["1.7.11"]),
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    return DialogEngine(index, storage, orders, settings)


def fill_contacts(engine, values=("Школа 1", "Иванов", "+7 916 330-02-79", "-", "Москва", "-")):
    for value in values:
        engine.handle_text(USER, CHANNEL, value)


def test_start_greets_with_menu(engine):
    responses = engine.start(USER, CHANNEL)
    assert isinstance(responses[0], Message)
    assert responses[0].keyboard is not None


def test_search_returns_list_with_citation(engine):
    responses = engine.handle_text(USER, CHANNEL, "2.20.63")
    assert isinstance(responses[0], ProductList)
    assert responses[0].cards[0].citation.startswith("позиция 2.20.63")


def test_add_and_quantity_changes_persist(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "inc:S1")
    cart = engine.storage.load_cart(USER)
    assert cart.count == 2 and cart.total == 506000


def test_remove_empties_cart(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    responses = engine.handle_action(USER, CHANNEL, "del:S1")
    assert isinstance(responses[0], Message)
    assert engine.storage.load_cart(USER).is_empty


def test_checkout_requires_consent_first(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    responses = engine.handle_action(USER, CHANNEL, "checkout")
    assert "персональных данных" in responses[0].text
    assert engine.storage.active_consent(USER) is None


def test_order_is_not_created_without_consent(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    # Пользователь пытается подтвердить заказ, минуя согласие.
    responses = engine.handle_action(USER, CHANNEL, "confirm_order")
    assert "персональных данных" in responses[0].text
    assert engine.storage.orders_of(USER) == []


def test_full_order_reaches_sink(engine, tmp_path):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine)
    responses = engine.handle_action(USER, CHANNEL, "confirm_order")

    assert "принят" in responses[0].text
    rows = [json.loads(line) for line in (tmp_path / "orders.jsonl").read_text("utf-8").splitlines()]
    assert rows[0]["Наименование"] == "Фрезерный станок с ЧПУ"
    assert rows[0]["Нормативное основание"].startswith("позиция 2.20.63")
    assert engine.storage.load_cart(USER).is_empty


def test_checkout_asks_every_field(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    asked = []
    for value in ("Школа 1", "Иванов", "+7 916 330-02-79", "-", "Москва"):
        asked.append(engine.handle_text(USER, CHANNEL, value)[0].text)
    assert len(asked) == len(CHECKOUT_FIELDS) - 1


def test_incomplete_contacts_are_rejected(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine, values=("-", "-", "-", "-", "-", "-"))
    assert engine.storage.orders_of(USER) == []


def test_cart_shows_price_note_when_price_missing(engine):
    engine.index.products[1] = product("S3", "Панель интерактивная", None)
    engine.index = CatalogIndex(engine.index.products)
    engine.handle_action(USER, CHANNEL, "add:S3")
    summary = engine.handle_action(USER, CHANNEL, "cart")[0]
    assert isinstance(summary, OrderSummary)
    assert "цена уточняется" in (summary.note or "")


def test_delete_data_anonymizes_orders_but_keeps_lines(engine):
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    fill_contacts(engine)
    engine.handle_action(USER, CHANNEL, "confirm_order")

    engine.handle_text(USER, CHANNEL, "/delete_data")

    assert engine.storage.orders_of(USER) == []
    anonymized = engine.storage.orders_of("deleted")
    assert anonymized and anonymized[0].customer.name == ""
    assert anonymized[0].items[0].sku_1c == "S1"
    assert engine.storage.active_consent(USER) is None


def test_unknown_command_does_not_crash(engine):
    assert "Такой команды нет" in engine.handle_text(USER, CHANNEL, "/nope")[0].text


@pytest.mark.parametrize(
    ("query", "expected"),
    [("2.20.63", "1 позиция"), ("мяч станок", "2 позиции")],
)
def test_result_title_uses_correct_plural(engine, query, expected):
    """Регрессия: заголовок выдачи писал «1 позиций»."""
    responses = engine.handle_text(USER, CHANNEL, query)
    assert expected in responses[0].title


def test_catalog_sections_fit_the_telegram_button_limit(engine):
    """Регрессия: раздел «ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838» терял кнопку.

    Telegram отводит под callback_data 64 байта, а кириллица занимает по два
    на символ — длинные названия разделов в них не помещались, и кнопка молча
    выбрасывалась при отрисовке.
    """
    responses = engine.handle_action(USER, CHANNEL, "catalog")
    buttons = [b for row in responses[0].keyboard.rows for b in row]

    assert buttons, "разделы каталога не показаны"
    for button in buttons:
        assert len(button.action.encode("utf-8")) <= 64, button.action


def test_section_opens_by_number(engine):
    engine.handle_action(USER, CHANNEL, "catalog")

    responses = engine.handle_action(USER, CHANNEL, "root:0")

    assert responses[0].cards, "раздел открылся пустым"


def test_old_buttons_with_section_names_still_work(engine):
    """Кнопки в уже отправленных сообщениях должны пережить обновление бота."""
    root = engine.roots[0]

    responses = engine.handle_action(USER, CHANNEL, f"root:{root}")

    assert responses[0].cards


def test_unknown_section_says_so(engine):
    responses = engine.handle_action(USER, CHANNEL, "root:999")

    assert "нет" in responses[0].text.lower()
