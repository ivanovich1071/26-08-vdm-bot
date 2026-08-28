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
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
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
    """Карточка так же полно, как на странице сайта.

    Описание не режем: раньше стояла обрезка в 400 символов, и текст обрывался на
    середине слова у половины каталога. В лимит сообщения его укладывает `fit`,
    и делает это по границе разметки, а не вслепую.
    """
    product = card.product
    lines = [
        f"<b>{_escape(product.name)}</b>",
        f"{price_text(product.price)} · {stock_text(product)}",
        f"Код 1С: {_escape(product.sku_1c)}",
    ]
    if card.citation:
        lines.append(f"Основание: {_escape(card.citation)}")

    # Код в характеристиках дублирует код 1С, он уже выведен строкой выше.
    extra = [(k, v) for k, v in product.attributes.items() if k.lower() != "код"]
    if extra:
        lines.append("")
        lines += [f"{_escape(name)}: {_escape(value)}" for name, value in extra]

    if product.description:
        lines.append("")
        lines.append(_escape(product.description))
    if product.kit_contents:
        lines.append("")
        lines.append("<b>Состав комплекта:</b>")
        lines += [f"• {_escape(item)}" for item in product.kit_contents]
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


async def send(
    bot: Bot, chat_id: int, responses: list[Response], storage=None  # noqa: ANN001
) -> None:
    for response in responses:
        markup = to_markup(getattr(response, "keyboard", None))
        if isinstance(response, Message):
            await bot.send_message(chat_id, fit(_escape(response.text)), reply_markup=markup)
        elif isinstance(response, ProductCard):
            await _send_card(bot, chat_id, response, markup, storage)
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


async def _send_card(  # noqa: ANN001
    bot: Bot, chat_id: int, card: ProductCard, markup, storage=None
) -> None:
    """Карточка с фото — одним сообщением, если подпись помещается.

    Длинное описание в подпись не влезает, поэтому такой товар показываем как
    фото и текст раздельно: обрезать состав комплекта хуже, чем разбить на два
    сообщения. Если картинка недоступна, шлём обычный текст — сорванная загрузка
    не должна лишать пользователя карточки.
    """
    text = render_card(card)
    photo = _photo(card, storage)
    if photo is None:
        await bot.send_message(chat_id, fit(text), reply_markup=markup)
        return

    try:
        if len(text) <= CAPTION_LIMIT:
            sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=markup)
        else:
            sent = await bot.send_photo(chat_id, photo)
            await bot.send_message(chat_id, fit(text), reply_markup=markup)
    except TelegramBadRequest as exc:
        log.warning("Фото товара %s не отправилось: %s", card.product.sku_1c, exc)
        await bot.send_message(chat_id, fit(text), reply_markup=markup)
        return

    _remember_photo(storage, card, sent)


def _photo(card: ProductCard, storage):  # noqa: ANN001, ANN202 — тип зависит от aiogram
    """Чем отправлять снимок, от самого дешёвого способа к самому ненадёжному.

    Адрес на vdm.ru стоит последним не случайно: Telegram ходит за картинкой сам
    и с его серверов сайт заказчика не открывается — «failed to get HTTP URL
    content». Поэтому сначала идентификатор уже загруженного файла, потом наш
    собственный файл на диске, и только затем адрес.
    """
    from pathlib import Path

    from aiogram.types import FSInputFile

    if card.image_path and storage is not None:
        file_id = storage.telegram_photo(card.image_path)
        if file_id:
            return file_id
    if card.image_path and Path(card.image_path).is_file():
        return FSInputFile(card.image_path)
    return card.image


def _remember_photo(storage, card: ProductCard, sent) -> None:  # noqa: ANN001
    """Запоминаем файл на серверах Telegram: второй показ уйдёт без загрузки."""
    if storage is None or not card.image_path:
        return
    sizes = getattr(sent, "photo", None)
    if not sizes:
        return
    storage.save_telegram_photo(card.image_path, card.product.sku_1c, sizes[-1].file_id)


def build_dispatcher(engine: DialogEngine) -> Dispatcher:
    dispatcher = Dispatcher()

    @dispatcher.message(Command("start"))
    async def on_start(message: TgMessage, bot: Bot) -> None:
        user, chat = str(message.from_user.id), message.chat.id
        await _reply(bot, chat, engine, lambda: engine.start(user, CHANNEL))

    @dispatcher.message(F.text)
    async def on_text(message: TgMessage, bot: Bot) -> None:
        user, chat, text = str(message.from_user.id), message.chat.id, message.text
        await _reply(bot, chat, engine, lambda: engine.handle_text(user, CHANNEL, text))

    @dispatcher.callback_query(F.data)
    async def on_callback(query: CallbackQuery, bot: Bot) -> None:
        user, chat, data = str(query.from_user.id), query.message.chat.id, query.data
        await _quietly(query.answer())
        await _reply(bot, chat, engine, lambda: engine.handle_action(user, CHANNEL, data))

    return dispatcher


async def _reply(bot: Bot, chat_id: int, engine: DialogEngine, work) -> None:  # noqa: ANN001
    """Ответ на сообщение: считаем в отдельном потоке, показываем «печатает».

    Ядро диалога синхронное, а ход с обращением к модели занимает от минуты до
    трёх. Вызванное прямо в обработчике, оно вставало поперёк цикла событий: бот
    переставал опрашивать Telegram, не отвечал другим людям и вообще не подавал
    признаков жизни. Отсюда — отдельный поток.

    Индикатор «печатает» Telegram гасит через пять секунд, поэтому его приходится
    повторять всё время ожидания. Иначе три минуты тишины выглядят как зависание,
    чем они, собственно, и выглядели.
    """
    typing = asyncio.create_task(_keep_typing(bot, chat_id))
    try:
        responses = await asyncio.to_thread(work)
    except Exception:
        log.exception("Ход диалога не отработал")
        responses = [
            Message("Что-то пошло не так на моей стороне. Повторите, пожалуйста, вопрос.")
        ]
    finally:
        typing.cancel()

    try:
        await send(bot, chat_id, responses, engine.storage)
    except TelegramNetworkError as exc:
        # Ответ уже посчитан, но связь оборвалась. Молчим в чат и остаёмся живыми:
        # опрос продолжится, а человек повторит вопрос.
        log.warning("Ответ не доставлен (%s)", exc)


class RetryOnNetworkError(BaseRequestMiddleware):
    """Повтор запроса к Telegram при обрыве связи.

    Канал до api.telegram.org с машины разработки рвётся регулярно, и до сих пор
    это било по самому больному месту: бот получал сообщение, считал ответ — и не
    мог его отправить. С точки зрения человека бот молчал, хотя работал.

    Повторяем всё, что уходит наружу, включая отправку сообщений и фотографий.
    Ошибки самого Telegram (неверный запрос, лимиты) сюда не попадают — их
    повторять бессмысленно, они не про связь.
    """

    def __init__(self, attempts: int = 3, pause: float = 2.0) -> None:
        self.attempts = attempts
        self.pause = pause

    async def __call__(self, make_request, bot, method):  # noqa: ANN001 — тип из aiogram
        pause = self.pause
        for attempt in range(1, self.attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError:
                if attempt == self.attempts:
                    raise
                log.info(
                    "%s не прошёл (попытка %s из %s), повтор через %.0f с",
                    type(method).__name__,
                    attempt,
                    self.attempts,
                    pause,
                )
                await asyncio.sleep(pause)
                pause *= 2
        raise AssertionError("недостижимо")  # pragma: no cover


async def _keep_typing(bot: Bot, chat_id: int) -> None:
    while True:
        await _quietly(bot.send_chat_action(chat_id, "typing"))
        await asyncio.sleep(4)


async def _quietly(coro) -> None:  # noqa: ANN001
    """Необязательное действие: индикатор набора, подтверждение нажатия.

    Раньше сорванный `send_chat_action` уносил с собой весь обработчик, и человек
    не получал ответа вовсе — при том что ответ уже был готов.
    """
    try:
        await coro
    except (TelegramNetworkError, TelegramBadRequest) as exc:
        log.debug("Служебный вызов не прошёл: %s", exc)


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
    bot.session.middleware(RetryOnNetworkError())
    dispatcher = build_dispatcher(build_engine(settings))
    log.info("Telegram-бот запущен")
    await _poll_forever(dispatcher, bot)


async def _poll_forever(dispatcher: Dispatcher, bot: Bot) -> None:
    """Опрос, переживающий обрывы связи.

    Внутри опроса aiogram сам повторяет неудачные запросы, но первый же обрыв
    на старте валил процесс целиком. На нестабильном канале это означало, что
    бот не поднимается вовсе. Неверный токен сюда не попадает: это отдельная
    ошибка, и её мы пропускаем наверх, чтобы не крутиться впустую.
    """
    pause = 5
    while True:
        try:
            await dispatcher.start_polling(bot)
            return
        except TelegramNetworkError as exc:
            log.warning("Связь с Telegram потеряна (%s). Повтор через %s с.", exc, pause)
            await asyncio.sleep(pause)
            pause = min(pause * 2, 60)


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
