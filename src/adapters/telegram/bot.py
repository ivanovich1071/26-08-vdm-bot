"""Адаптер Telegram.

Ничего не решает сам: переводит сообщения и нажатия в вызовы ядра, а примитивы
ответа — в сообщения Telegram. Любое правило продажи, согласия или корзины живёт
в `core/dialog.py`, иначе каналы разойдутся.

    python -m adapters.telegram.bot
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.types import (
    Message as TgMessage,
)

from core.app import build_engine
from core.config import Settings
from core.dialog import DialogEngine
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

log = logging.getLogger(__name__)
CHANNEL = "telegram"

# Telegram обрезает callback_data на 64 байтах — коды 1С длиннее не бывают,
# но проверку оставляем, чтобы молча не терять кнопки.
CALLBACK_LIMIT = 64

# Подпись к фото Telegram ограничивает жёстче, чем обычное сообщение.
CAPTION_LIMIT = 1024

# Сообщение длиннее 4096 символов Telegram отклоняет целиком. На текущей выгрузке
# самая длинная карточка — 3490, но описания в следующей могут оказаться длиннее,
# и тогда бот молча перестанет отвечать по части товаров.
MESSAGE_LIMIT = 4096
TRUNCATED_TAIL = "\n\n… полное описание на сайте"


def fit(text: str) -> str:
    """Укладывает сообщение в лимит, не ломая разметку.

    Обрезка «в лоб» может разрубить тег пополам или оставить `<b>` незакрытым —
    тогда Telegram отклонит всё сообщение, а не просто покажет его без выделения.
    """
    if len(text) <= MESSAGE_LIMIT:
        return text

    budget = MESSAGE_LIMIT - len(TRUNCATED_TAIL)
    # Закрывающие теги тоже занимают место, а сколько их — известно только после
    # обрезки. Поэтому подбираем: обрезали, посчитали, при нехватке ужались.
    for _ in range(3):
        cut = _trim_partial_tag(text[:budget])
        closers = _closing_tags(cut)
        if len(cut) + len(closers) <= MESSAGE_LIMIT - len(TRUNCATED_TAIL):
            return cut + closers + TRUNCATED_TAIL
        budget -= len(closers)
    return _trim_partial_tag(text[:budget]) + TRUNCATED_TAIL


def _trim_partial_tag(text: str) -> str:
    """Убирает обрывок тега в конце: `…<b` или `…</`."""
    last_open = text.rfind("<")
    if last_open > text.rfind(">"):
        text = text[:last_open]
    return text.rstrip()


def _closing_tags(text: str) -> str:
    closers = ""
    for tag in ("b", "i", "u", "s", "code", "pre"):
        unclosed = text.count(f"<{tag}>") - text.count(f"</{tag}>")
        closers += f"</{tag}>" * max(0, unclosed)
    return closers


def to_markup(keyboard: Keyboard | None) -> InlineKeyboardMarkup | None:
    if keyboard is None or not keyboard.rows:
        return None
    rows = []
    for row in keyboard.rows:
        buttons = []
        for button in row:
            data = button.action.encode("utf-8")
            if button.url:
                buttons.append(InlineKeyboardButton(text=button.title, url=button.url))
            elif len(data) <= CALLBACK_LIMIT:
                buttons.append(
                    InlineKeyboardButton(text=button.title, callback_data=button.action)
                )
            else:
                log.warning("Кнопка «%s» пропущена: слишком длинное действие", button.title)
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def render_card(card: ProductCard) -> str:
    product = card.product
    lines = [f"<b>{_escape(product.name)}</b>", f"{price_text(product.price)} · {stock_text(product)}"]
    if card.citation:
        lines.append(f"Основание: {_escape(card.citation)}")
    if product.description:
        lines.append("")
        lines.append(_escape(product.description[:400]))
    if product.kit_contents:
        lines.append("")
        lines.append("<b>Состав:</b>")
        lines += [f"• {_escape(item)}" for item in product.kit_contents[:8]]
        if len(product.kit_contents) > 8:
            lines.append(f"…ещё {len(product.kit_contents) - 8}")
    return "\n".join(lines)


def render_list_header(items: ProductList) -> str:
    """Только заголовок выдачи.

    Сами позиции уходят отдельными сообщениями: в Telegram у каждой нужны свои
    кнопки «В корзину» и «Подробнее», а перечислять их ещё и списком — значит
    показать пользователю одно и то же дважды.
    """
    return f"<b>{_escape(items.title)}</b>"


def render_list_item(card: ProductCard) -> str:
    product = card.product
    lines = [
        f"<b>{_escape(product.name)}</b>",
        f"{price_text(product.price)} · {stock_text(product)}",
    ]
    if card.citation:
        lines.append(_escape(card.citation))
    return "\n".join(lines)


def render_order(summary: OrderSummary) -> str:
    lines = ["<b>Ваш заказ</b>", ""]
    for name, quantity, price in summary.lines:
        lines.append(f"• {_escape(name)} — {quantity} × {price_text(price)}")
    lines.append("")
    lines.append(f"<b>Итого: {price_text(summary.total)}</b>")
    if summary.note:
        lines.append("")
        lines.append(_escape(summary.note))
    return "\n".join(lines)


async def send(bot: Bot, chat_id: int, responses: list[Response]) -> None:
    for response in responses:
        markup = to_markup(getattr(response, "keyboard", None))
        if isinstance(response, Message):
            await bot.send_message(chat_id, fit(_escape(response.text)), reply_markup=markup)
        elif isinstance(response, ProductCard):
            await _send_card(bot, chat_id, response, markup)
        elif isinstance(response, ProductList):
            await bot.send_message(chat_id, fit(render_list_header(response)))
            for card in response.cards:
                await bot.send_message(
                    chat_id, fit(render_list_item(card)), reply_markup=to_markup(card.keyboard)
                )
            # Навигация по выдаче — последним сообщением, чтобы кнопки были под рукой.
            if markup is not None:
                await bot.send_message(chat_id, "Что дальше?", reply_markup=markup)
        elif isinstance(response, OrderSummary):
            await bot.send_message(chat_id, fit(render_order(response)), reply_markup=markup)


async def _send_card(bot: Bot, chat_id: int, card: ProductCard, markup) -> None:  # noqa: ANN001
    """Карточка с фото — одним сообщением, если подпись помещается.

    Длинное описание в подпись не влезает, поэтому такой товар показываем как
    фото и текст раздельно: обрезать состав комплекта хуже, чем разбить на два
    сообщения. Если картинка недоступна, шлём обычный текст — сорванная загрузка
    не должна лишать пользователя карточки.
    """
    text = render_card(card)
    if not card.image:
        await bot.send_message(chat_id, fit(text), reply_markup=markup)
        return

    try:
        if len(text) <= CAPTION_LIMIT:
            await bot.send_photo(chat_id, card.image, caption=text, reply_markup=markup)
            return
        await bot.send_photo(chat_id, card.image)
        await bot.send_message(chat_id, fit(text), reply_markup=markup)
    except TelegramBadRequest as exc:
        log.warning("Фото %s не отправилось: %s", card.image, exc)
        await bot.send_message(chat_id, fit(text), reply_markup=markup)


def build_dispatcher(engine: DialogEngine) -> Dispatcher:
    dispatcher = Dispatcher()

    @dispatcher.message(Command("start"))
    async def on_start(message: TgMessage, bot: Bot) -> None:
        await send(bot, message.chat.id, engine.start(str(message.from_user.id), CHANNEL))

    @dispatcher.message(F.text)
    async def on_text(message: TgMessage, bot: Bot) -> None:
        await bot.send_chat_action(message.chat.id, "typing")
        responses = engine.handle_text(str(message.from_user.id), CHANNEL, message.text)
        await send(bot, message.chat.id, responses)

    @dispatcher.callback_query(F.data)
    async def on_callback(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        responses = engine.handle_action(str(query.from_user.id), CHANNEL, query.data)
        await send(bot, query.message.chat.id, responses)

    return dispatcher


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def use_compatible_event_loop() -> None:
    """На Windows aiohttp не работает с циклом событий по умолчанию.

    Стандартный ProactorEventLoop роняет соединение с api.telegram.org ошибкой
    «превышен таймаут семафора» (WinError 121) ещё до отправки запроса, причём
    обычный синхронный запрос к тому же адресу проходит. Лечится переключением
    на SelectorEventLoop. На других системах ничего не меняем.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    if not settings.telegram_token:
        raise SystemExit("Не задан TELEGRAM_TOKEN — бот не запускается.")

    bot = Bot(settings.telegram_token, default=_default_properties(), session=_session())
    dispatcher = build_dispatcher(build_engine(settings))
    log.info("Telegram-бот запущен")
    await dispatcher.start_polling(bot)


def _default_properties():  # noqa: ANN202 — тип зависит от версии aiogram
    from aiogram.client.default import DefaultBotProperties

    return DefaultBotProperties(parse_mode="HTML")


def _session():  # noqa: ANN202 — тип зависит от версии aiogram
    """Сессия, принудительно работающая по IPv4.

    api.telegram.org резолвится и в IPv6, но на сетях без реальной IPv6-связности
    соединение просто виснет до таймаута, и бот падает при старте. Оставляем IPv4:
    он доступен везде, где доступен Telegram.
    """
    import socket

    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession()
    # aiogram собирает коннектор из этого словаря — добавляем семейство адресов,
    # не трогая остальную его настройку (TLS, лимиты, кэш DNS).
    session._connector_init["family"] = socket.AF_INET
    return session


if __name__ == "__main__":
    use_compatible_event_loop()
    asyncio.run(main())
