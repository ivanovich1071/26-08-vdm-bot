"""Ядро диалога: одна логика продажи на все каналы.

Адаптер отдаёт сюда текст или действие пользователя и получает готовые примитивы
ответа. Ни Telegram, ни виджет не знают ни про корзину, ни про согласие, ни про
нормативные основания — иначе правила разъедутся между каналами и починить их
в одном месте станет невозможно.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from catalog.models import Product
from catalog.search import CatalogIndex, SearchHit, SearchQuery
from core.config import Settings
from core.models import CartItem, Customer
from core.storage import Storage
from core.ui import (
    Button,
    Keyboard,
    Message,
    OrderSummary,
    ProductCard,
    ProductList,
    Response,
    plural,
    price_text,
    stock_text,
)
from norms import documents as norm_docs
from orders.service import OrderService
from privacy.consent import CONSENT_TEXT, CONSENT_VERSION

log = logging.getLogger(__name__)

GREETING = (
    "Здравствуйте! Помогу подобрать оборудование для детского сада или школы.\n\n"
    "Можно так:\n"
    "• назвать, что нужно: «мячи для спортивного зала»;\n"
    "• указать пункт приказа: «2.1.14» или «п. 2.20.63»;\n"
    "• спросить, чем оснастить кабинет: «что нужно в кабинет логопеда по приказу 838».\n\n"
    "Цены и наличие — из выгрузки 1С заказчика."
)

# Поля, которые бот спрашивает при оформлении. Больше не собираем: каждое лишнее
# поле — это лишние персональные данные, которые придётся защищать и удалять.
CHECKOUT_FIELDS: tuple[tuple[str, str], ...] = (
    ("organization", "Название организации (или «-», если заказ для себя):"),
    ("name", "Контактное лицо — как к вам обращаться:"),
    ("phone", "Телефон для связи:"),
    ("email", "E-mail (можно «-»):"),
    ("region", "Город или регион доставки:"),
    ("comment", "Комментарий к заказу (можно «-»):"),
)

PAGE_SIZE = 5
# Верхний предел выборки: больше пользователю всё равно не показать,
# а отдавать в агент сотни позиций дорого и бессмысленно.
SEARCH_CAP = 50


@dataclass
class Session:
    """Состояние диалога одного пользователя.

    Живёт в памяти процесса: корзина и заказы лежат в базе, а вот незаконченный
    ввод контактов переживать перезапуск не обязан — его проще спросить заново.
    """

    user_id: str
    channel: str
    last_hits: list[SearchHit] = field(default_factory=list)
    checkout_step: int | None = None
    customer: Customer = field(default_factory=Customer)
    pending_checkout: bool = False
    history: list[dict[str, str]] = field(default_factory=list)


class DialogEngine:
    def __init__(
        self,
        index: CatalogIndex,
        storage: Storage,
        orders: OrderService,
        settings: Settings,
        agent=None,  # noqa: ANN001 — необязательная зависимость, см. agent/
        dialog_log=None,  # noqa: ANN001 — observability/dialog_log.py
        media=None,  # noqa: ANN001 — media/service.py, фото добираются с сайта
    ) -> None:
        self.index = index
        self.storage = storage
        self.orders = orders
        self.settings = settings
        self.agent = agent
        self.dialog_log = dialog_log
        self.media = media
        self._sessions: dict[str, Session] = {}
        self._roots: list[str] | None = None

    def session(self, user_id: str, channel: str) -> Session:
        key = f"{channel}:{user_id}"
        if key not in self._sessions:
            self._sessions[key] = Session(user_id=user_id, channel=channel)
        return self._sessions[key]

    # --- Точки входа ---------------------------------------------------------

    def start(self, user_id: str, channel: str) -> list[Response]:
        self.session(user_id, channel)
        return [Message(GREETING, keyboard=self._main_menu())]

    def handle_text(self, user_id: str, channel: str, text: str) -> list[Response]:
        started = time.monotonic()
        # Шаги сбора контактов — это чистые персональные данные и ничего не дают
        # для настройки промптов. В журнал вместо них идёт отметка о шаге.
        collecting = self.session(user_id, channel).checkout_step is not None
        responses = self._handle_text(user_id, channel, text)
        logged = "<контактные данные при оформлении>" if collecting else text
        self._log(user_id, channel, "text", logged, responses, started)
        return responses

    def handle_action(self, user_id: str, channel: str, action: str) -> list[Response]:
        started = time.monotonic()
        responses = self._handle_action(user_id, channel, action)
        self._log(user_id, channel, "action", action, responses, started)
        return responses

    def _log(
        self,
        user_id: str,
        channel: str,
        kind: str,
        incoming: str,
        responses: list[Response],
        started: float,
    ) -> None:
        if self.dialog_log is None:
            return
        agent = self.agent
        mode = "search" if agent is None or not getattr(agent, "available", True) else "agent"
        self.dialog_log.turn(
            channel=channel,
            user_id=user_id,
            kind=kind,
            incoming=incoming,
            responses=responses,
            mode=mode,
            latency_ms=int((time.monotonic() - started) * 1000),
            cart_count=self.storage.load_cart(user_id).count,
        )

    def _handle_text(self, user_id: str, channel: str, text: str) -> list[Response]:
        session = self.session(user_id, channel)
        text = text.strip()
        if not text:
            return [Message("Напишите, что ищете.", keyboard=self._main_menu())]

        if text.startswith("/"):
            return self._handle_command(session, text)
        if session.checkout_step is not None:
            return self._collect_contact(session, text)

        session.history.append({"role": "user", "content": text})
        if self.agent is not None:
            return self.agent.reply(session, text)
        return self.search(session, text)

    def _handle_action(self, user_id: str, channel: str, action: str) -> list[Response]:
        session = self.session(user_id, channel)
        verb, _, arg = action.partition(":")

        match verb:
            case "menu":
                return [Message("Чем помочь?", keyboard=self._main_menu())]
            case "catalog":
                return self._sections()
            case "root":
                return self._by_root(session, arg)
            case "norms":
                return self._norm_help()
            case "card":
                return self._card(session, arg)
            case "add":
                return self._add(session, arg, 1)
            case "inc":
                return self._change(session, arg, +1)
            case "dec":
                return self._change(session, arg, -1)
            case "del":
                return self._change(session, arg, 0, remove=True)
            case "cart":
                return self._show_cart(session)
            case "clear":
                return self._clear_cart(session)
            case "checkout":
                return self._start_checkout(session)
            case "consent_yes":
                return self._grant_consent(session)
            case "consent_no":
                return [
                    Message(
                        "Без согласия заказ передать менеджеру нельзя. "
                        f"Можно позвонить напрямую: {self.settings.manager_contact}.",
                        keyboard=self._main_menu(),
                    )
                ]
            case "confirm_order":
                return self._submit(session)
            case "cancel":
                session.checkout_step = None
                session.pending_checkout = False
                return [Message("Оформление отменено, корзина сохранена.", keyboard=self._main_menu())]
            case "more":
                return self._more(session, int(arg or 0))
        return [Message("Не понял действие.", keyboard=self._main_menu())]

    # --- Команды -------------------------------------------------------------

    def _handle_command(self, session: Session, text: str) -> list[Response]:
        command = text.split()[0].lower()
        match command:
            case "/start" | "/menu":
                return self.start(session.user_id, session.channel)
            case "/cart":
                return self._show_cart(session)
            case "/my_data":
                return self._export_data(session)
            case "/delete_data":
                return self._delete_data(session)
            case "/help":
                return [Message(GREETING, keyboard=self._main_menu())]
        return [Message("Такой команды нет. /help — что умеет бот.", keyboard=self._main_menu())]

    # --- Поиск и карточки -----------------------------------------------------

    def search(self, session: Session, text: str, limit: int = PAGE_SIZE) -> list[Response]:
        hits = self.index.search(SearchQuery(text=text, limit=SEARCH_CAP))
        session.last_hits = hits
        if not hits:
            return [
                Message(
                    "Ничего не нашёл по этому запросу. Попробуйте назвать товар иначе "
                    "или указать пункт приказа — например, «2.1.14».\n"
                    f"Если нужно, подключим менеджера: {self.settings.manager_contact}.",
                    keyboard=self._main_menu(),
                )
            ]

        title = self._result_title(hits, text)
        return [self._list(hits[:limit], title, len(hits), offset=0)]

    def _more(self, session: Session, offset: int) -> list[Response]:
        hits = session.last_hits
        chunk = hits[offset : offset + PAGE_SIZE]
        if not chunk:
            return [Message("Больше ничего нет.", keyboard=self._main_menu())]
        return [self._list(chunk, "Ещё варианты", len(hits), offset=offset)]

    def _list(self, hits: list[SearchHit], title: str, total: int, offset: int) -> ProductList:
        cards = [
            ProductCard(
                product=hit.product,
                citation=hit.citation(),
                keyboard=Keyboard().row(
                    Button("В корзину", f"add:{hit.product.sku_1c}"),
                    Button("Подробнее", f"card:{hit.product.sku_1c}"),
                ),
            )
            for hit in hits
        ]
        keyboard = Keyboard()
        if offset + len(hits) < total:
            keyboard.row(Button("Показать ещё", f"more:{offset + len(hits)}"))
        keyboard.row(Button("Корзина", "cart"), Button("Меню", "menu"))
        return ProductList(title=title, cards=cards, total_found=total, keyboard=keyboard)

    def _card(self, session: Session, sku: str) -> list[Response]:
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]

        cart_item = self.storage.load_cart(session.user_id).find(sku)
        keyboard = Keyboard()
        if cart_item:
            keyboard.row(
                Button("−", f"dec:{sku}"),
                Button(f"{cart_item.quantity} шт.", f"card:{sku}"),
                Button("+", f"inc:{sku}"),
            )
        else:
            keyboard.row(Button("В корзину", f"add:{sku}"))
        if product.url:
            keyboard.row(Button("Открыть на сайте", f"card:{sku}", url=product.url))
        keyboard.row(Button("Корзина", "cart"), Button("Меню", "menu"))

        norm = product.best_norm()
        return [
            ProductCard(
                product=product,
                quantity=cart_item.quantity if cart_item else 0,
                citation=norm.citation if norm else None,
                keyboard=keyboard,
                image=self._image(product),
                image_path=self.photo_path(product),
            )
        ]

    def photo_path(self, product: Product) -> str | None:
        """Снимок, лежащий у нас на диске.

        Telegram не может забрать картинку с vdm.ru сам — отвечает «failed to get
        HTTP URL content». Поэтому файл для него важнее адреса.
        """
        if self.media is None:
            return None
        try:
            return self.media.local_photo(product)
        except Exception as exc:  # фото не должно ломать ответ
            log.warning("Локальное фото для %s не найдено: %s", product.sku_1c, exc)
            return None

    def _image(self, product: Product) -> str | None:
        """Фото только для подробной карточки.

        В списках выдачи их не запрашиваем: пять позиций — это пять обращений
        к сайту заказчика на каждый запрос, а пользы от превью в списке мало.
        """
        # Собранная база знаний уже содержит снимки — тогда на сайт идти незачем.
        if product.images:
            return product.images[0]
        if self.media is None:
            return None
        try:
            return self.media.main_image(product)
        except Exception as exc:  # фото не должно ломать ответ
            log.warning("Фото для %s не получено: %s", product.sku_1c, exc)
            return None

    def _result_title(self, hits: list[SearchHit], text: str) -> str:
        # Выдача ограничена сверху, поэтому ровно на пределе честнее сказать «более»:
        # иначе бот сообщает как точное число размер собственной выборки.
        capped = len(hits) >= SEARCH_CAP
        word = plural(len(hits), "позиция", "позиции", "позиций")
        count = f"более {SEARCH_CAP} позиций" if capped else f"{len(hits)} {word}"
        if hits and hits[0].by_norm:
            code = hits[0].matched_code
            return f"По пункту {code}: {count}" if code else f"По перечню: {count}"
        return f"Нашлось {count} по запросу «{text}»"

    # --- Разделы --------------------------------------------------------------

    @property
    def roots(self) -> list[str]:
        """Корневые разделы каталога в порядке появления в выгрузке."""
        if self._roots is None:
            found: list[str] = []
            for product in self.index.products:
                for root in product.roots:
                    if root not in found:
                        found.append(root)
            self._roots = found
        return self._roots

    def _sections(self) -> list[Response]:
        keyboard = Keyboard()
        # В кнопку кладём номер раздела, а не название: Telegram ограничивает
        # callback_data 64 байтами, а «ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838»
        # в кириллице занимает вдвое больше — такие кнопки молча пропадали.
        for number, root in enumerate(self.roots):
            keyboard.row(Button(root.title(), f"root:{number}"))
        keyboard.row(Button("Подбор по приказу", "norms"), Button("Меню", "menu"))
        return [Message("Выберите раздел каталога:", keyboard=keyboard)]

    def _by_root(self, session: Session, arg: str) -> list[Response]:
        root = self._root_by(arg)
        if root is None:
            return [Message("Такого раздела нет.", keyboard=self._main_menu())]
        return self._list_root(session, root)

    def _root_by(self, arg: str) -> str | None:
        """Номер раздела или его название.

        Название понимаем ради кнопок, нажатых в старых сообщениях: в чате они
        остаются рабочими и после обновления бота.
        """
        if arg.isdigit():
            number = int(arg)
            return self.roots[number] if number < len(self.roots) else None
        return arg if arg in self.roots else None

    def _list_root(self, session: Session, root: str) -> list[Response]:
        hits = self.index.search(SearchQuery(text="", root=root, limit=SEARCH_CAP))
        if not hits:
            # Пустой текст не даёт ранжирования — берём раздел напрямую.
            products = [p for p in self.index.products if root in p.roots][:SEARCH_CAP]
            hits = [SearchHit(p, 0.0, "text") for p in products]
        session.last_hits = hits
        return [self._list(hits[:PAGE_SIZE], root.title(), len(hits), offset=0)]

    def _norm_help(self) -> list[Response]:
        lines = ["Подбор по нормативным перечням. Что можно спросить:", ""]
        for doc in (norm_docs.ORDER_838, norm_docs.ORDER_1057):
            lines.append(f"• {doc.full_name or doc.short_name}")
        lines += [
            "",
            "Напишите номер пункта — «2.20.63», «п. 2.1.14» — или подраздел целиком: «2.4».",
            "Можно словами: «кабинет физики по приказу 838».",
        ]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    # --- Корзина ---------------------------------------------------------------

    def _add(self, session: Session, sku: str, quantity: int) -> list[Response]:
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]

        cart = self.storage.load_cart(session.user_id)
        norm = product.best_norm()
        cart.add(
            CartItem(
                sku_1c=product.sku_1c,
                name=product.name,
                price=product.price,
                quantity=quantity,
                url=product.url,
                norm_citation=norm.citation if norm else None,
            )
        )
        self.storage.save_cart(cart)
        return [
            Message(
                f"«{product.name}» добавлен. В корзине {cart.count} шт. "
                f"на {price_text(cart.total)}.",
                keyboard=Keyboard().row(
                    Button("Корзина", "cart"),
                    Button("Оформить", "checkout"),
                    Button("Меню", "menu"),
                ),
            )
        ]

    def _change(self, session: Session, sku: str, delta: int, remove: bool = False) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        item = cart.find(sku)
        if item is None:
            return self._show_cart(session)
        cart.set_quantity(sku, 0 if remove else item.quantity + delta)
        self.storage.save_cart(cart)
        return self._show_cart(session)

    def _show_cart(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        if cart.is_empty:
            return [Message("Корзина пуста.", keyboard=self._main_menu())]

        keyboard = Keyboard()
        for item in cart.items:
            keyboard.row(
                Button("−", f"dec:{item.sku_1c}"),
                Button(f"{item.quantity} × {item.name[:24]}", f"card:{item.sku_1c}"),
                Button("+", f"inc:{item.sku_1c}"),
                Button("Удалить", f"del:{item.sku_1c}"),
            )
        keyboard.row(Button("Оформить заказ", "checkout"), Button("Очистить", "clear"))
        keyboard.row(Button("Меню", "menu"))

        note = None
        if any(item.price is None for item in cart.items):
            note = "По части позиций цена уточняется — менеджер пришлёт её при подтверждении."
        return [
            OrderSummary(
                lines=[(item.name, item.quantity, item.price) for item in cart.items],
                total=cart.total,
                note=note,
                keyboard=keyboard,
            )
        ]

    def _clear_cart(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        cart.clear()
        self.storage.save_cart(cart)
        return [Message("Корзина очищена.", keyboard=self._main_menu())]

    # --- Оформление -------------------------------------------------------------

    def _start_checkout(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        if cart.is_empty:
            return [Message("Сначала добавьте товары в корзину.", keyboard=self._main_menu())]

        if self.storage.active_consent(session.user_id) is None:
            session.pending_checkout = True
            return [
                Message(
                    CONSENT_TEXT,
                    keyboard=Keyboard().row(
                        Button("Согласен", "consent_yes"),
                        Button("Отказаться", "consent_no"),
                    ),
                )
            ]
        return self._ask_contact(session, step=0)

    def _grant_consent(self, session: Session) -> list[Response]:
        self.storage.record_consent(
            session.user_id, session.channel, CONSENT_VERSION, "granted"
        )
        if not session.pending_checkout:
            return [Message("Согласие записано.", keyboard=self._main_menu())]
        session.pending_checkout = False
        return self._ask_contact(session, step=0)

    def _ask_contact(self, session: Session, step: int) -> list[Response]:
        session.checkout_step = step
        _, question = CHECKOUT_FIELDS[step]
        return [
            Message(
                f"Шаг {step + 1} из {len(CHECKOUT_FIELDS)}. {question}",
                keyboard=Keyboard().row(Button("Отменить", "cancel")),
            )
        ]

    def _collect_contact(self, session: Session, text: str) -> list[Response]:
        step = session.checkout_step or 0
        field_name, _ = CHECKOUT_FIELDS[step]
        value = "" if text.strip() in {"-", "—", "нет"} else text.strip()
        setattr(session.customer, field_name, value)

        if step + 1 < len(CHECKOUT_FIELDS):
            return self._ask_contact(session, step + 1)

        session.checkout_step = None
        if not session.customer.is_complete:
            session.customer = Customer()
            return [
                Message(
                    "Нужны хотя бы имя и телефон (или e-mail), иначе менеджер не сможет "
                    "с вами связаться. Начнём заново?",
                    keyboard=Keyboard().row(
                        Button("Заполнить заново", "checkout"), Button("Меню", "menu")
                    ),
                )
            ]

        cart = self.storage.load_cart(session.user_id)
        customer = session.customer
        summary = (
            f"Проверьте заказ:\n\n"
            f"Организация: {customer.organization or '—'}\n"
            f"Контакт: {customer.name}, {customer.phone or customer.email}\n"
            f"Регион: {customer.region or '—'}\n"
            f"Позиций: {cart.count} на {price_text(cart.total)}"
        )
        return [
            Message(
                summary,
                keyboard=Keyboard().row(
                    Button("Отправить менеджеру", "confirm_order"),
                    Button("Отменить", "cancel"),
                ),
            )
        ]

    def _submit(self, session: Session) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        try:
            order = self.orders.submit(cart, session.customer, session.channel)
        except PermissionError:
            return self._start_checkout(session)
        except ValueError as exc:
            return [Message(str(exc), keyboard=self._main_menu())]

        session.customer = Customer()
        delivered = order.status == "sent"
        tail = (
            "Менеджер свяжется с вами в рабочее время."
            if delivered
            else "Заказ сохранён, менеджер получит его чуть позже — мы повторим отправку."
        )
        return [
            Message(
                f"Заказ {order.id} принят на {price_text(order.total)}. {tail}\n"
                f"Связаться напрямую: {self.settings.manager_contact}",
                keyboard=self._main_menu(),
            )
        ]

    # --- Права субъекта ПДн ------------------------------------------------------

    def _export_data(self, session: Session) -> list[Response]:
        data = self.storage.export_user_data(session.user_id)
        if not data["orders"] and not data["consents"] and not data["cart"]:
            return [Message("По вам не хранится никаких данных.", keyboard=self._main_menu())]
        lines = [
            "Что о вас хранится:",
            f"• позиций в корзине: {len(data['cart'])}",
            f"• заказов: {len(data['orders'])}",
            f"• записей о согласии: {len(data['consents'])}",
            "",
            "Удалить всё и отозвать согласие — /delete_data",
        ]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    def _delete_data(self, session: Session) -> list[Response]:
        self.storage.delete_user_data(session.user_id, session.channel)
        session.customer = Customer()
        session.last_hits = []
        return [
            Message(
                "Данные удалены, согласие отозвано. Переданные ранее заказы обезличены: "
                "контакты стёрты, позиции остались у менеджера для учёта.",
                keyboard=self._main_menu(),
            )
        ]

    # --- Общее -------------------------------------------------------------------

    def _main_menu(self) -> Keyboard:
        return (
            Keyboard()
            .row(Button("Каталог", "catalog"), Button("Подбор по приказу", "norms"))
            .row(Button("Корзина", "cart"), Button("Оформить", "checkout"))
        )


def describe(product: Product) -> str:
    """Короткое описание товара одной строкой — для списков и виджета."""
    parts = [product.name, price_text(product.price), stock_text(product)]
    norm = product.best_norm()
    if norm:
        parts.append(norm.citation)
    return " · ".join(parts)
