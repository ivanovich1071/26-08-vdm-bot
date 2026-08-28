import pytest

pytest.importorskip("aiogram")

import asyncio  # noqa: E402

from aiogram.exceptions import TelegramNetworkError  # noqa: E402

from adapters.telegram.bot import (  # noqa: E402
    CALLBACK_LIMIT,
    MESSAGE_LIMIT,
    RetryOnNetworkError,
    _reply,
    fit,
    render_card,
    render_list_item,
    to_markup,
)
from catalog.models import Product  # noqa: E402
from core.ui import Button, Keyboard, Message, ProductCard  # noqa: E402


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


# --- Полная карточка «как на сайте» -------------------------------------------


def test_description_is_not_cut_in_the_middle():
    """Регрессия: описание резалось на 400 символах, у половины каталога — по слову."""
    text = "Комплект дидактических пособий. " * 40
    card = render_card(ProductCard(product=product(description=text)))

    assert text.strip() in card
    assert "…" not in card


def test_card_shows_country_and_certificate():
    """Страну и сертификат в закупке для сада и школы спрашивают всерьёз."""
    card = render_card(
        ProductCard(
            product=product(
                attributes={"Код": "0Э-00005662", "Страна": "Китай", "Сертификат": "ЕАС"}
            )
        )
    )

    assert "Страна: Китай" in card and "Сертификат: ЕАС" in card
    # Код 1С уже выведен отдельной строкой — второй раз он не нужен.
    assert card.count("0Э-00005662") == 0
    assert "Код 1С: S1" in card


def test_whole_kit_is_listed():
    card = render_card(ProductCard(product=product(kit_contents=[f"поз. {i}" for i in range(12)])))

    assert "поз. 11" in card


def test_long_card_still_fits_the_message_limit():
    card = render_card(ProductCard(product=product(description="Очень длинно. " * 900)))

    assert len(fit(card)) <= MESSAGE_LIMIT


# --- Чем отправлять снимок ------------------------------------------------------


class FakeStorage:
    def __init__(self, known=None):
        self.known = known or {}
        self.saved = []

    def telegram_photo(self, path):
        return self.known.get(path)

    def save_telegram_photo(self, path, sku_1c, file_id):
        self.saved.append((path, sku_1c, file_id))


def test_known_file_id_is_reused(tmp_path):
    from adapters.telegram.bot import _photo

    path = tmp_path / "1.jpg"
    path.write_bytes(b"jpeg")
    card = ProductCard(product=product(), image="https://vdm.ru/1.jpg", image_path=str(path))

    assert _photo(card, FakeStorage({str(path): "AgACAgIAAx"})) == "AgACAgIAAx"


def test_local_file_beats_the_address(tmp_path):
    """Telegram не может забрать картинку с vdm.ru сам — файл ему нужнее адреса."""
    from aiogram.types import FSInputFile

    from adapters.telegram.bot import _photo

    path = tmp_path / "1.jpg"
    path.write_bytes(b"jpeg")
    card = ProductCard(product=product(), image="https://vdm.ru/1.jpg", image_path=str(path))

    assert isinstance(_photo(card, FakeStorage()), FSInputFile)


def test_address_is_the_last_resort(tmp_path):
    from adapters.telegram.bot import _photo

    card = ProductCard(
        product=product(), image="https://vdm.ru/1.jpg", image_path=str(tmp_path / "нет.jpg")
    )

    assert _photo(card, FakeStorage()) == "https://vdm.ru/1.jpg"


def test_card_without_any_photo_gives_none():
    from adapters.telegram.bot import _photo

    assert _photo(ProductCard(product=product()), FakeStorage()) is None


# --- Обработчик сообщения ------------------------------------------------------


class FakeBot:
    """Бот, у которого индикатор набора всегда обрывается по сети."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_chat_action(self, chat_id, action):  # noqa: ANN001
        raise TelegramNetworkError(method=None, message="ClientConnectorError")

    async def send_message(self, chat_id, text, reply_markup=None):  # noqa: ANN001
        self.sent.append(text)


class FakeEngine:
    storage = None


async def test_broken_typing_indicator_does_not_swallow_the_answer():
    """Регрессия 28.08: бот молчал на сообщения, хотя ответ был готов.

    Сорванный `send_chat_action` уносил с собой весь обработчик — со стороны это
    выглядело как зависший бот.
    """
    bot = FakeBot()
    await _reply(bot, 1, FakeEngine(), lambda: [Message("Готовый ответ")])

    assert bot.sent == ["Готовый ответ"]


async def test_failure_inside_the_core_still_gets_a_human_answer():
    bot = FakeBot()

    def broken():
        raise RuntimeError("что-то сломалось в ядре")

    await _reply(bot, 1, FakeEngine(), broken)

    assert bot.sent and "Повторите" in bot.sent[0]


async def test_long_answer_does_not_block_the_event_loop():
    """Ход с обращением к модели идёт минуты — всё это время бот обязан жить."""
    import time

    bot = FakeBot()
    ticks = 0

    async def other_work():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1

    await asyncio.gather(
        _reply(bot, 1, FakeEngine(), lambda: (time.sleep(0.2), [Message("Готово")])[1]),
        other_work(),
    )

    assert ticks == 5, "цикл событий стоял, пока считался ответ"
    assert bot.sent == ["Готово"]


async def test_send_is_retried_when_the_link_breaks():
    """Регрессия 28.08: ответ был готов, но не доходил — канал до Telegram рвётся."""
    calls = 0

    async def make_request(bot, method):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TelegramNetworkError(method=None, message="ClientConnectorError")
        return "доставлено"

    middleware = RetryOnNetworkError(attempts=3, pause=0.01)

    assert await middleware(make_request, None, object()) == "доставлено"
    assert calls == 3


async def test_hopeless_link_gives_up_instead_of_retrying_forever():
    async def always_broken(bot, method):  # noqa: ANN001
        raise TelegramNetworkError(method=None, message="ClientConnectorError")

    middleware = RetryOnNetworkError(attempts=2, pause=0.01)

    with pytest.raises(TelegramNetworkError):
        await middleware(always_broken, None, object())
