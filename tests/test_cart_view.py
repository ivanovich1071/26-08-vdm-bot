"""Корзина: что видит человек и что происходит при нажатии.

Две жалобы заказчика сходятся здесь. Первая: «товары непонятные, не видны
названия» — название жило внутри кнопки и обрезалось. Вторая: «выскочили подряд
5 штук одной позиции» — каждое нажатие присылало новое сообщение, изменений
видно не было, и человек жал снова.
"""

import zipfile

import pytest

from adapters.telegram.bot import render_order, to_markup
from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.models import Customer, Order
from core.storage import Storage
from core.ui import Message, OrderSummary
from orders.service import OrderService
from orders.sinks import JsonlSink, XlsxSink

CHANNEL = "telegram"
USER = "u1"

LONG_NAME = "Сенсомоторный набор «Вижу — делаю» для инклюзивной группы"


def product(sku, name, price):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "url": None,
            "short_url": None,
            "price": price,
            "currency": "RUB",
            "in_stock": 2,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]],
            "description": "",
            "kit_contents": [],
            "norms": [],
            "bitrix_id": None,
        }
    )


@pytest.fixture
def engine(tmp_path):
    index = CatalogIndex(
        [product("0Э-00002613", LONG_NAME, 126999), product("KF0015", "Мультиформы", 4816)]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(
        orders_jsonl_path=str(tmp_path / "orders.jsonl"),
        orders_xlsx_dir=str(tmp_path / "orders"),
    )
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    return DialogEngine(index, storage, orders, settings)


def test_full_name_and_code_are_in_the_text(engine):
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    summary = engine.handle_action(USER, CHANNEL, "cart")[0]
    text = render_order(summary)

    assert LONG_NAME in text
    assert "код 1С 0Э-00002613" in text


def test_buttons_carry_numbers_not_names(engine):
    """В кнопку помещается два десятка символов — названию там не место."""
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    summary = engine.handle_action(USER, CHANNEL, "cart")[0]

    titles = [button.title for row in summary.keyboard.rows for button in row]
    assert "1 −" in titles and "1 +" in titles and "1 ✕" in titles
    assert not any(LONG_NAME[:10] in title for title in titles)


def test_quantity_label_is_not_a_working_button(engine):
    """Раньше нажатие на количество присылало карточку заново — отсюда дубли."""
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    assert engine.handle_action(USER, CHANNEL, "noop") == []


def test_quantity_change_replaces_the_message(engine):
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    summary = engine.handle_action(USER, CHANNEL, "inc:0Э-00002613")[0]
    assert isinstance(summary, OrderSummary)
    assert summary.replace is True


def test_quantity_change_on_card_replaces_the_card(engine):
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    card = engine.handle_action(USER, CHANNEL, "card_inc:0Э-00002613")[0]
    assert card.replace is True
    assert card.quantity == 2


def test_emptied_cart_also_replaces(engine):
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    message = engine.handle_action(USER, CHANNEL, "del:0Э-00002613")[0]
    assert isinstance(message, Message)
    assert message.replace is True


def test_noop_button_survives_telegram_markup(engine):
    """Надпись остаётся кнопкой в разметке — иначе ряд развалится."""
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    summary = engine.handle_action(USER, CHANNEL, "cart")[0]
    markup = to_markup(summary.keyboard)
    actions = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "noop" in actions


# --- «Начать заново» ----------------------------------------------------------


def test_restart_clears_cart_and_memory(engine):
    engine.handle_action(USER, CHANNEL, "add:0Э-00002613")
    session = engine.session(USER, CHANNEL)
    session.profile.update_from_text("оснащаю спортзал в детском саду")

    engine.handle_text(USER, CHANNEL, "/start")

    assert engine.storage.load_cart(USER).is_empty
    assert session.profile.is_empty
    assert session.history == []


def test_restart_button_asks_first(engine):
    responses = engine.handle_action(USER, CHANNEL, "restart")
    titles = [b.title for row in responses[0].keyboard.rows for b in row]
    assert "Да, начать заново" in titles


# --- Спецификация в Excel -----------------------------------------------------


def test_xlsx_specification_is_written(tmp_path):
    """Пока нет интеграции с 1С, заказ уходит менеджеру таблицей."""
    from core.models import Cart, CartItem

    cart = Cart(user_id=USER, items=[CartItem("S1", "Мяч", 900, 2)])
    order = Order.create(cart, Customer(name="Иванов", phone="+7 000"), CHANNEL, "c1")

    XlsxSink(directory=tmp_path).push(order)
    target = tmp_path / f"{order.id}.xlsx"

    assert target.exists()
    with zipfile.ZipFile(target) as book:
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Код 1С" in sheet
    assert "Мяч" in sheet
