import pytest

pytest.importorskip("aiogram")

from adapters.telegram.bot import (  # noqa: E402
    CALLBACK_LIMIT,
    MESSAGE_LIMIT,
    fit,
    render_card,
    render_list_item,
    to_markup,
)
from catalog.models import Product  # noqa: E402
from core.ui import Button, Keyboard, ProductCard  # noqa: E402


def product(**kw):
    raw = {
        "sku_1c": "S1",
        "name": "Мяч баскетбольный",
        "price": 908,
        "currency": "RUB",
        "in_stock": 4,
        "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]],
        "description": "",
        "kit_contents": [],
        "norms": [],
        "bitrix_id": None,
        "url": None,
        "short_url": None,
    }
    raw.update(kw)
    return Product.from_dict(raw)


def test_short_text_is_untouched():
    assert fit("<b>Мяч</b>") == "<b>Мяч</b>"


def test_long_text_fits_the_limit():
    assert len(fit("а" * 5000)) <= MESSAGE_LIMIT


def test_truncation_closes_open_tags():
    """Регрессия: незакрытый <b> заставляет Telegram отклонить всё сообщение."""
    result = fit("<b>" + "а" * 5000)
    assert result.count("<b>") == result.count("</b>")
    assert len(result) <= MESSAGE_LIMIT


def test_truncation_never_leaves_half_a_tag():
    # Обрезка приходится ровно на середину тега.
    text = "а" * (MESSAGE_LIMIT - 30) + "<b>хвост</b>" + "б" * 100
    result = fit(text)
    assert "<" not in result[result.rfind(">") + 1 :]
    assert len(result) <= MESSAGE_LIMIT


def test_special_characters_are_escaped_in_card():
    card = ProductCard(product=product(name='Набор "Мама & сын" <малый>'))
    rendered = render_card(card)
    assert "&amp;" in rendered and "&lt;малый&gt;" in rendered
    assert "<малый>" not in rendered


def test_card_shows_price_stock_and_citation():
    card = ProductCard(product=product(), citation="позиция 1.7.11 — приказ № 838")
    rendered = render_card(card)
    assert "908 ₽" in rendered and "в наличии 4 шт." in rendered
    assert "позиция 1.7.11" in rendered


def test_card_without_price_says_on_request():
    assert "цена по запросу" in render_card(ProductCard(product=product(price=None)))


def test_list_item_is_compact():
    rendered = render_list_item(ProductCard(product=product(description="ж" * 3000)))
    assert len(rendered) < 200


def test_link_button_becomes_url_button():
    keyboard = Keyboard().row(Button("Открыть на сайте", "card:S1", url="https://vdm.ru/x"))
    markup = to_markup(keyboard)
    assert markup.inline_keyboard[0][0].url == "https://vdm.ru/x"


def test_overlong_callback_is_dropped_not_sent_broken():
    keyboard = Keyboard().row(Button("Кнопка", "add:" + "я" * CALLBACK_LIMIT))
    assert to_markup(keyboard) is None


def test_empty_keyboard_gives_no_markup():
    assert to_markup(None) is None
    assert to_markup(Keyboard()) is None
