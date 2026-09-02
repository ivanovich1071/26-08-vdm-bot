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
from core import intent
from core.config import Settings
from core.models import CartItem, Customer
from core.profile import DialogProfile
from core.storage import Storage
from core.ui import (
    Button,
    Keyboard,
    Message,
    OrderLine,
    OrderSummary,
    ProductCard,
    ProductList,
    Response,
    plural,
    price_text,
    stock_text,
)
from norms import documents as norm_docs
from norms import items as norm_items
from norms import reference as norm_reference
from orders.service import OrderService
from privacy.consent import CONSENT_TEXT, CONSENT_VERSION
from privacy.masking import Masker

log = logging.getLogger(__name__)

# Приветствие открытое: кнопки — подсказка, а не единственный путь. Четыре
# жёстких сценария на старте отсекали тех, кто пришёл с вопросом, а не с заявкой.
GREETING = (
    "Здравствуйте! Я консультант ЭЛТИ-КУДИЦ — помогу с оборудованием для детского "
    "сада или школы.\n\n"
    "Напишите своими словами, что нужно. Например:\n"
    "• «чем оснастить спортзал в саду, дети 3–6 лет»;\n"
    "• «что значит приказ 838» — объясню документ;\n"
    # Пример намеренно из приказа 1057: пункт 2.1.14, стоявший здесь раньше,
    # есть только в школьном приказе 838 — бот сам приглашал детский сад
    # ввести чужой номер, а потом объяснял, почему по нему ничего нет.
    "• «1.5.1» — покажу позиции по пункту перечня.\n\n"
    "Можно просто описать задачу — разберёмся вместе.\n"
    "Можете воспользоваться кнопками, но это не обязательно."
)

# Что бот умеет — по команде /help. Приветствие держим коротким, а перечень
# возможностей выносим сюда: он нужен тому, кто специально его спросил.
HELP = (
    "Что я умею:\n\n"
    "• подобрать оборудование по описанию задачи — «кабинет логопеда в детском саду»;\n"
    "• найти позиции по пункту перечня — «1.5.1» или «1.13.3»;\n"
    "• объяснить документ — «что значит приказ 838»;\n"
    "• собрать корзину и передать заявку менеджеру.\n\n"
    "Команды:\n"
    "/start — начать заново\n"
    "/cart — корзина\n"
    "/order — оформить заказ\n"
    "/manager — связаться с менеджером\n"
    "/my_data — что о вас хранится\n"
    "/delete_data — удалить данные и отозвать согласие"
)

# Сколько реплик разговора храним. Дальше модель всё равно не смотрит, а профиль
# помнит суть без переписки.
HISTORY_KEPT = 24

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

# Сколько позиций показываем за раз. Пять оказалось много: заказчик отдельно
# отметил, что выдача из пяти карточек «излишня». Три помещаются на экран
# целиком, и каждую можно показать со снимком, не превращая чат в ленту.
PAGE_SIZE = 3
# Верхний предел выборки: больше пользователю всё равно не показать,
# а отдавать в агент сотни позиций дорого и бессмысленно.
SEARCH_CAP = 50
# Сколько наименований перечисляем текстом, когда отвечаем без модели. Список
# читается одним взглядом и даёт понять, что вообще есть, а подробности —
# в трёх карточках под ним.
OFFER_NAMES = 10

# Постоянная клавиатура Telegram присылает нажатие обычным текстом — сопоставляем
# его с действием. Ключи в нижнем регистре, сравнение тоже.
KEYBOARD_ACTIONS: dict[str, str] = {
    "каталог": "catalog",
    "моя корзина": "cart",
    "корзина": "cart",
    "менеджер": "manager",
    "связаться с менеджером": "manager",
}


def _not_listed(audience: str | None) -> str | None:
    """Честная строка вместо пустого основания.

    Почти половина каталога к перечням не привязана — реестра «пункт 838 → код
    1С» у заказчика нет вовсе. Молчать об этом хуже, чем сказать: закупщик по
    пустому месту решит, что основание есть и просто не показано.
    """
    if audience == "preschool":
        return "в перечнях для дошкольных организаций эта позиция не числится — уточнит менеджер"
    if audience == "school":
        return "в перечнях для школ эта позиция не числится — уточнит менеджер"
    return "в перечнях приказов эта позиция не числится — уточнит менеджер"


@dataclass
class Session:
    """Состояние диалога одного пользователя.

    Разделено намеренно. Переписка и профиль переживают перезапуск — иначе бот
    забывает разговор посреди подбора. А незаконченный ввод контактов не хранится
    нигде: это чистые персональные данные, и спросить их заново дешевле, чем
    защищать на диске.

    **История хранится маскированной с момента записи.** Не «замаскируем перед
    отправкой модели», а именно так: телефон, попавший в переписку, не должен
    оказаться в базе даже на время.
    """

    user_id: str
    channel: str
    last_hits: list[SearchHit] = field(default_factory=list)
    checkout_step: int | None = None
    customer: Customer = field(default_factory=Customer)
    pending_checkout: bool = False
    history: list[dict[str, str]] = field(default_factory=list)
    profile: DialogProfile = field(default_factory=DialogProfile)
    # Один маскер на весь разговор: метки должны совпадать между сообщениями,
    # иначе модель видит [ИМЯ_1] в истории и [ИМЯ_2] в текущей реплике для
    # одного и того же человека.
    masker: Masker = field(default_factory=Masker)
    # Расход модели за текущий ход: токены, обращения, рубли. Живёт в сессии, а не
    # в агенте, потому что ходы разных людей считаются одновременно, в разных
    # потоках. В журнал уходит по завершении хода и обнуляется перед следующим.
    usage: dict[str, object] = field(default_factory=dict)
    # Кто отвечал и почему: роль, этап, возражение, показаны ли карточки и по
    # какой причине. Заполняет агент, читает журнал. На ручных прогонах без этого
    # не понять, почему бот промолчал карточками, — а прогоны заказчик делает сам.
    route: dict[str, object] = field(default_factory=dict)
    # Все цены, которые инструменты вернули за этот разговор. Проверка на
    # выдуманные суммы смотрела только текущий ход — и отвергала ответ, где
    # модель ссылается на позицию, показанную ходом раньше. Именно так теряется
    # ответ на возражение «дорого»: там инструменты не вызываются вовсе.
    prices: set[int] = field(default_factory=set)
    # То же для нормативных оснований: пары «приказ, пункт», которые инструменты
    # подтвердили за разговор. Модель ссылается на пункт из прошлого хода так же
    # свободно, как на цену, и проверка по одному ходу отвергала бы честный ответ.
    norm_refs: set[tuple[str, str]] = field(default_factory=set)

    def remember(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": self.masker.mask(content)})
        del self.history[:-HISTORY_KEPT]

    def forget(self) -> None:
        self.history.clear()
        self.profile = DialogProfile()
        self.masker = Masker()
        self.prices.clear()
        self.norm_refs.clear()


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
        # Пункты приказов с формулировками и поиском по словам. Файла может не
        # быть — тогда бот называет номер пункта без текста, как и раньше.
        self.norm_texts = norm_items.ItemIndex(norm_items.load())

    def session(self, user_id: str, channel: str) -> Session:
        key = f"{channel}:{user_id}"
        if key not in self._sessions:
            self._sessions[key] = self._restore(user_id, channel)
        return self._sessions[key]

    def _restore(self, user_id: str, channel: str) -> Session:
        """Разговор, начатый до перезапуска бота.

        Метки маскирования после перезапуска раскрыть нечем — соответствие жило
        в памяти процесса. Это осознанный размен: ПДн не хранятся, а нераскрытая
        метка заменяется нейтральным словом при показе (см. `Masker.unmask`).
        """
        session = Session(user_id=user_id, channel=channel)
        try:
            saved = self.storage.load_dialog_state(user_id, channel)
        except Exception as exc:  # состояние не должно мешать начать разговор
            log.warning("Состояние диалога %s не прочитано: %s", user_id, exc)
            return session
        if saved:
            session.history = saved["history"]
            session.profile = DialogProfile.from_dict(saved["profile"])
        return session

    def _remember(self, session: Session) -> None:
        try:
            self.storage.save_dialog_state(
                session.user_id, session.channel, session.history, session.profile.to_dict()
            )
        except Exception as exc:  # запись состояния не стоит ответа пользователю
            log.warning("Состояние диалога %s не сохранено: %s", session.user_id, exc)

    # --- Точки входа ---------------------------------------------------------

    def start(self, user_id: str, channel: str) -> list[Response]:
        self.session(user_id, channel)
        return [Message(GREETING, keyboard=self._main_menu())]

    def handle_text(self, user_id: str, channel: str, text: str) -> list[Response]:
        started = time.monotonic()
        # Шаги сбора контактов — это чистые персональные данные и ничего не дают
        # для настройки промптов. В журнал вместо них идёт отметка о шаге.
        session = self.session(user_id, channel)
        collecting = session.checkout_step is not None
        session.usage = {}
        session.route = {}
        responses = self._handle_text(user_id, channel, text)
        logged = "<контактные данные при оформлении>" if collecting else text
        self._log(user_id, channel, "text", logged, responses, started)
        return responses

    def handle_action(self, user_id: str, channel: str, action: str) -> list[Response]:
        started = time.monotonic()
        # Расход и роль сбрасываются так же, как в текстовом ходе. Без этого на
        # нажатие «Корзина» в журнал уходили токены и рубли предыдущего ответа
        # модели — 02.09 один и тот же ход оказался посчитан трижды.
        session = self.session(user_id, channel)
        session.usage = {}
        session.route = {}
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
        session = self.session(user_id, channel)
        self.dialog_log.turn(
            channel=channel,
            user_id=user_id,
            kind=kind,
            incoming=incoming,
            responses=responses,
            mode=mode,
            latency_ms=int((time.monotonic() - started) * 1000),
            cart_count=self.storage.load_cart(user_id).count,
            usage=session.usage or None,
            route=session.route or None,
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

        # Нажатие постоянной клавиатуры приходит обычным текстом. Разбираем его
        # до обновления профиля: иначе слово «Каталог» уйдёт в разбор задачи.
        action = KEYBOARD_ACTIONS.get(text.strip().lower())
        if action is not None:
            return self._handle_action(user_id, channel, action)

        # Профиль обновляем до ответа: то, что человек сказал сейчас, должно
        # попасть в промпт этого же хода, а не следующего.
        session.profile.update_from_text(text)
        session.remember("user", text)

        doc_id = norm_reference.question_about_document(text)
        if doc_id is not None:
            responses = self._explain_norm(session, doc_id)
        elif self.agent is not None:
            responses = self.agent.reply(session, text)
        else:
            responses = self.offer(session, text)

        self._remember(session)
        return responses

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
            case "norm_doc":
                return self._explain_norm(session, arg)
            case "norm_items":
                return self._norm_items(session, arg)
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
            case "card_inc":
                return self._change_on_card(session, arg, +1)
            case "card_dec":
                return self._change_on_card(session, arg, -1)
            case "cart":
                return self._show_cart(session)
            case "clear":
                return self._clear_cart(session)
            case "restart":
                return self._confirm_restart()
            case "restart_yes":
                return self._restart(session)
            case "manager":
                return self._manager()
            case "noop":
                # Надпись с количеством — не кнопка. Telegram всё равно требует
                # у неё действие, поэтому действие есть, а ответа на него нет.
                return []
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
            case "/start":
                # Начать заново: команда и есть та самая «Перезагрузка». Люди не
                # догадывались, что для нового подбора надо звать /start, поэтому
                # теперь она и в меню команд, и по кнопке.
                return self._restart(session)
            case "/menu":
                return [Message("Чем помочь?", keyboard=self._main_menu())]
            case "/cart":
                return self._show_cart(session)
            case "/order":
                return self._start_checkout(session)
            case "/manager":
                return self._manager()
            case "/my_data":
                return self._export_data(session)
            case "/delete_data":
                return self._delete_data(session)
            case "/help":
                return [Message(HELP, keyboard=self._main_menu())]
        return [Message("Такой команды нет. /help — что умеет бот.", keyboard=self._main_menu())]

    # --- Поиск и карточки -----------------------------------------------------

    def search(
        self, session: Session, text: str, limit: int = PAGE_SIZE, title: str | None = None
    ) -> list[Response]:
        hits = self.index.search(
            SearchQuery(text=text, limit=SEARCH_CAP, audience=session.profile.audience)
        )
        session.last_hits = hits
        if not hits:
            return [
                Message(
                    "Ничего не нашёл по этому запросу. Попробуйте назвать товар иначе "
                    "или указать пункт приказа — например, «1.5.1».\n"
                    f"Если нужно, подключим менеджера: {self.settings.manager_contact}.",
                    keyboard=self._main_menu(),
                )
            ]

        return [
            self._list(hits[:limit], title or self._result_title(hits, text), len(hits), offset=0)
        ]

    def offer(self, session: Session, text: str) -> list[Response]:
        """Ответ, когда модели нет: сеть упала, кончились деньги, ключ не вписан.

        Раньше здесь стоял прямой вызов `search`, и любой текст уходил в каталог.
        1 сентября бот ответил на «привет» списком из пятидесяти товаров, а на
        «а почему на мой привет ты мне товарами отвечаешь?» — ещё пятьюдесятью.
        Поиск перестал быть ответом по умолчанию.

        Что осталось без модели, то и предлагаем честно: список наименований и
        три карточки под ним — по согласованию с заказчиком.
        """
        kind = intent.classify(text)

        # Вопрос о документе отвечается из наших данных и без модели тоже.
        # 01.09 на «по какому приказу оснащается детский сад» бот выдал полсотни
        # случайных товаров: вопрос попал в товарную ветку, и справка, которая
        # лежала рядом, не пригодилась.
        if kind is intent.NORM_QUESTION:
            doc_id = norm_reference.question_about_document(text)
            if doc_id is None:
                return self._norm_help()
            return self._explain_norm(session, doc_id)

        if kind in (intent.GREETING, intent.SMALL_TALK):
            return [
                Message(
                    "Здравствуйте! Я консультант ЭЛТИ-КУДИЦ. Подбираете для "
                    "детского сада или для школы?",
                    keyboard=self._main_menu(),
                )
            ]

        if kind not in (intent.PRODUCT, intent.NORM_CODE):
            return [
                Message(
                    "Сейчас я отвечаю проще обычного — консультант временно "
                    "недоступен. Могу показать каталог или найти позиции по "
                    "пункту перечня, например «1.5.1».\n"
                    f"По остальным вопросам — менеджер: {self.settings.manager_contact}.",
                    keyboard=self._offer_menu(),
                )
            ]

        hits = self.index.search(
            SearchQuery(text=text, limit=SEARCH_CAP, audience=session.profile.audience)
        )
        session.last_hits = hits
        if not hits:
            return [
                Message(
                    "Ничего не нашёл по этому запросу. Попробуйте назвать товар иначе "
                    "или указать пункт приказа — например, «1.5.1».\n"
                    f"Если нужно, подключим менеджера: {self.settings.manager_contact}.",
                    keyboard=self._offer_menu(),
                )
            ]

        shown = hits[:OFFER_NAMES]
        names = "\n".join(f"• {hit.product.name}" for hit in shown)
        header = "Могу предложить товары из каталога"
        if len(hits) > len(shown):
            header += f" — вот {len(shown)} из {self._found(hits)}"
        return [
            Message(f"{header}:\n\n{names}", keyboard=self._offer_menu()),
            self._list(hits[:PAGE_SIZE], "Первые три — подробнее", len(hits), offset=0),
        ]

    def _offer_menu(self) -> Keyboard:
        return Keyboard().row(
            Button("Каталог", "catalog"),
            Button("Связаться с менеджером", "manager"),
        )

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
                # Пустая строка основания читается как «мы не проверяли». В
                # подробной карточке об этом сказано давно, а в выдаче позиция
                # без привязки молчала — и стояла вперемешку с обоснованными.
                citation=hit.citation() or _not_listed(hit.audience),
                keyboard=Keyboard().row(
                    Button("В корзину", f"add:{hit.product.sku_1c}"),
                    Button("Подробнее", f"card:{hit.product.sku_1c}"),
                ),
                # Снимок теперь есть и в выдаче. Раньше его не показывали, чтобы
                # не ходить на сайт заказчика пять раз за один ответ; сейчас файлы
                # лежат у нас, а позиций в выдаче три, а не пять.
                image=self._image(hit.product),
                image_path=self.photo_path(hit.product),
            )
            for hit in hits
        ]
        keyboard = Keyboard()
        if offset + len(hits) < total:
            keyboard.row(Button("Показать ещё", f"more:{offset + len(hits)}"))
        keyboard.row(Button("Моя корзина", "cart"), Button("Меню", "menu"))
        return ProductList(title=title, cards=cards, total_found=total, keyboard=keyboard)

    def _card(self, session: Session, sku: str, replace: bool = False) -> list[Response]:
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]

        cart_item = self.storage.load_cart(session.user_id).find(sku)
        keyboard = Keyboard()
        if cart_item:
            keyboard.row(
                Button("−", f"card_dec:{sku}"),
                # Количество — надпись, а не кнопка: раньше нажатие на неё
                # присылало ту же карточку заново, и в чате копились дубли.
                Button(f"{cart_item.quantity} шт.", "noop"),
                Button("+", f"card_inc:{sku}"),
            )
        else:
            keyboard.row(Button("В корзину", f"add:{sku}"))
        if product.url:
            keyboard.row(Button("Открыть на сайте", f"card:{sku}", url=product.url))
        keyboard.row(Button("Моя корзина", "cart"), Button("Меню", "menu"))

        audience = session.profile.audience
        norm = product.norm_for(audience, session.profile.room or "")
        return [
            ProductCard(
                product=product,
                quantity=cart_item.quantity if cart_item else 0,
                citation=norm.citation if norm else None,
                keyboard=keyboard,
                image=self._image(product),
                image_path=self.photo_path(product),
                norms=self.norm_lines(product, audience),
                replace=replace,
            )
        ]

    def norm_lines(self, product: Product, audience: str | None) -> list[str]:
        """Все основания товара с формулировками пунктов приказа.

        Раньше в карточке стоял голый номер — «позиция 2.4.35». Что за ним, было
        не узнать, не открыв приказ на полутора сотнях страниц. Теперь рядом стоит
        строка из самого документа.

        Чужой перечень сюда не попадает: школьный пункт не обосновывает закупку
        для детского сада, и показывать его человеку из сада — это ровно та
        путаница, на которую жаловался заказчик.
        """
        lines: list[str] = []
        for ref in product.norms_for(audience):
            item = self.norm_texts.get(ref.doc_id, ref.item_code or "")
            title = item.title if item else ref.item_title
            line = ref.citation
            if title:
                line += f" — {title}"
            if item and item.section:
                line += f" ({item.section})"
            lines.append(line)

        # Молчание об основании читается как «мы не проверяли». Почти половина
        # каталога к перечням не привязана, и человеку, который собирает закупку,
        # честный ответ нужнее пустого места.
        if not lines:
            note = _not_listed(audience)
            if note:
                lines.append(note)
        return lines

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

    def _found(self, hits: list[SearchHit]) -> str:
        """«24 позиции» или «более 50 позиций».

        Выдача ограничена сверху, поэтому ровно на пределе честнее сказать «более»:
        иначе бот сообщает как точное число размер собственной выборки.
        """
        if len(hits) >= SEARCH_CAP:
            return f"более {SEARCH_CAP} позиций"
        return f"{len(hits)} {plural(len(hits), 'позиция', 'позиции', 'позиций')}"

    def _result_title(self, hits: list[SearchHit], text: str) -> str:
        count = self._found(hits)
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
        audience = session.profile.audience
        hits = self.index.search(
            SearchQuery(text="", root=root, limit=SEARCH_CAP, audience=audience)
        )
        if not hits:
            # Пустой текст не даёт ранжирования — берём раздел напрямую.
            products = [p for p in self.index.products if root in p.roots][:SEARCH_CAP]
            hits = [SearchHit(p, 0.0, "text", None, audience) for p in products]
        session.last_hits = hits
        return [self._list(hits[:PAGE_SIZE], root.title(), len(hits), offset=0)]

    def _norm_help(self) -> list[Response]:
        keyboard = Keyboard()
        for doc_id in norm_reference.known_documents():
            keyboard.row(Button(norm_docs.get(doc_id).short_name, f"norm_doc:{doc_id}"))
        keyboard.row(Button("Меню", "menu"))
        return [
            Message(
                "По какому документу подбираем? Нажмите — объясню, что это за документ "
                "и что по нему есть в каталоге.\n\n"
                "Можно и сразу номером пункта: «2.20.63», «п. 1.5.1» или подраздел «2.4».",
                keyboard=keyboard,
            )
        ]

    def _explain_norm(self, session: Session, doc_id: str) -> list[Response]:
        """Справка по документу — без обращения к модели.

        Нормативный вопрос обязан работать всегда, в том числе когда провайдер
        недоступен: именно на нём отказ модели заметнее всего, а ответ полностью
        собирается из наших данных.
        """
        text = norm_reference.explain(
            doc_id,
            norm_reference.coverage(self.index, doc_id, self.norm_texts.count(doc_id)),
        )
        if not text:
            return [Message("По этому документу справки пока нет.", keyboard=self._main_menu())]
        # Справка не назначает документ, по которому идёт закупка: человек
        # спросил, что это такое, а не сказал «оснащаю по нему». Иначе один
        # вопрос про 838 переводил в школьный режим весь остаток разговора.
        if doc_id not in session.profile.asked_about_docs:
            session.profile.asked_about_docs.append(doc_id)
        session.remember("assistant", text)

        keyboard = Keyboard().row(Button("Показать позиции", f"norm_items:{doc_id}"))
        keyboard.row(Button("Другой документ", "norms"), Button("Меню", "menu"))
        return [Message(text, keyboard=keyboard)]

    def _norm_items(self, session: Session, doc_id: str) -> list[Response]:
        if doc_id not in norm_docs.DOCUMENTS:
            return [Message("Такого документа нет.", keyboard=self._main_menu())]
        # Аудиторию берём у самого документа: если человек смотрит перечень для
        # школ, обосновывать позиции садовским приказом бессмысленно.
        subject = norm_docs.get(doc_id).subject
        audience = subject if subject in {"school", "preschool"} else session.profile.audience
        hits = self.index.search(
            SearchQuery(text="", norm_doc_id=doc_id, limit=SEARCH_CAP, audience=audience)
        )
        if not hits:
            products = [
                p for p in self.index.products if any(r.doc_id == doc_id for r in p.norms)
            ][:SEARCH_CAP]
            hits = [SearchHit(p, 0.0, "text", None, audience) for p in products]
        if not hits:
            return [
                Message(
                    "К этому документу в каталоге позиции не привязаны.",
                    keyboard=self._main_menu(),
                )
            ]
        session.last_hits = hits
        title = f"{norm_docs.get(doc_id).short_name}: {len(hits)} позиций"
        return [self._list(hits[:PAGE_SIZE], title, len(hits), offset=0)]

    # --- Корзина ---------------------------------------------------------------

    def _add(self, session: Session, sku: str, quantity: int) -> list[Response]:
        product = self.index.get(sku)
        if product is None:
            return [Message("Не нашёл такой товар.", keyboard=self._main_menu())]

        cart = self.storage.load_cart(session.user_id)
        norm = product.norm_for(session.profile.audience, session.profile.room or "")
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
                    Button("Моя корзина", "cart"),
                    Button("Оформить", "checkout"),
                ),
            )
        ]

    def _change(self, session: Session, sku: str, delta: int, remove: bool = False) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        item = cart.find(sku)
        if item is None:
            return self._show_cart(session, replace=True)
        cart.set_quantity(sku, 0 if remove else item.quantity + delta)
        self.storage.save_cart(cart)
        return self._show_cart(session, replace=True)

    def _change_on_card(self, session: Session, sku: str, delta: int) -> list[Response]:
        """Количество меняют прямо в карточке товара — её же и обновляем.

        Отдельные действия от корзинных не ради красоты: ответ должен заменить то
        сообщение, под которым нажали, а это разные сообщения.
        """
        cart = self.storage.load_cart(session.user_id)
        item = cart.find(sku)
        if item is not None:
            cart.set_quantity(sku, item.quantity + delta)
            self.storage.save_cart(cart)
        return self._card(session, sku, replace=True)

    def _show_cart(self, session: Session, replace: bool = False) -> list[Response]:
        cart = self.storage.load_cart(session.user_id)
        if cart.is_empty:
            return [Message("Корзина пуста.", keyboard=self._main_menu(), replace=replace)]

        # Название товара живёт в тексте, а не в кнопке. В кнопку Telegram влезает
        # десятка два символов, и заказчик видел «1 × Сенсом...» вместо позиции.
        # Кнопки теперь короткие и пронумерованы так же, как строки списка.
        keyboard = Keyboard()
        for number, item in enumerate(cart.items, 1):
            keyboard.row(
                Button(f"{number} −", f"dec:{item.sku_1c}"),
                Button(f"{number}: {item.quantity} шт.", "noop"),
                Button(f"{number} +", f"inc:{item.sku_1c}"),
                Button(f"{number} ✕", f"del:{item.sku_1c}"),
            )
        keyboard.row(Button("Оформить заказ", "checkout"), Button("Очистить", "clear"))

        note = None
        if any(item.price is None for item in cart.items):
            note = "По части позиций цена уточняется — менеджер пришлёт её при подтверждении."
        return [
            OrderSummary(
                lines=[
                    OrderLine(
                        name=item.name,
                        quantity=item.quantity,
                        price=item.price,
                        sku_1c=item.sku_1c,
                        norm_citation=item.norm_citation,
                    )
                    for item in cart.items
                ],
                total=cart.total,
                note=note,
                keyboard=keyboard,
                replace=replace,
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
            f"• сохранённых разговоров: {len(data['dialogs'])} "
            "(переписка хранится обезличенной — имена и телефоны заменены метками, "
            "срок хранения 30 дней)",
            "",
            "Удалить всё и отозвать согласие — /delete_data",
        ]
        return [Message("\n".join(lines), keyboard=self._main_menu())]

    def _delete_data(self, session: Session) -> list[Response]:
        self.storage.delete_user_data(session.user_id, session.channel)
        session.customer = Customer()
        session.last_hits = []
        # Переписка и профиль стираются и в памяти процесса: удалить их только
        # в базе означало бы, что бот всё ещё помнит разговор.
        session.forget()
        return [
            Message(
                "Данные удалены, согласие отозвано. Переписка и то, что я о задаче "
                "запомнил, тоже стёрты. Переданные ранее заказы обезличены: контакты "
                "удалены, позиции остались у менеджера для учёта.",
                keyboard=self._main_menu(),
            )
        ]

    # --- Общее -------------------------------------------------------------------

    def _main_menu(self) -> Keyboard:
        """Кнопки под сообщением — только то, чего нет в меню команд.

        Корзина, оформление, помощь и «начать заново» переехали в командное меню
        Telegram: постоянные четыре кнопки под каждым ответом загромождали окно
        диалога, а нажать их всё равно можно было только у последнего сообщения.
        """
        return Keyboard().row(
            Button("Каталог", "catalog"),
            Button("Подбор по приказу", "norms"),
            Button("Начать заново", "restart"),
        )

    def _confirm_restart(self) -> list[Response]:
        return [
            Message(
                "Начать заново? Я забуду, что мы обсуждали, и очищу корзину.",
                keyboard=Keyboard().row(
                    Button("Да, начать заново", "restart_yes"),
                    Button("Отмена", "menu"),
                ),
            )
        ]

    def _restart(self, session: Session) -> list[Response]:
        """Чистый лист: ни разговора, ни профиля, ни корзины.

        Пользователи не догадывались, что для нового подбора нужно звать /start,
        и продолжали прежний разговор — бот помнил старую задачу и подмешивал её
        в новую. Незаконченное оформление сбрасываем тоже: чужие контакты в чужой
        заявке хуже, чем лишний вопрос.
        """
        cart = self.storage.load_cart(session.user_id)
        cart.clear()
        self.storage.save_cart(cart)
        session.checkout_step = None
        session.pending_checkout = False
        session.customer = Customer()
        session.last_hits = []
        session.forget()
        self._remember(session)
        return [Message(GREETING, keyboard=self._main_menu())]

    def _manager(self) -> list[Response]:
        return [
            Message(
                "Менеджер ЭЛТИ-КУДИЦ ответит на вопросы по срокам, документам и "
                "нестандартной комплектации.\n\n"
                f"{self.settings.manager_contact}\n\n"
                "Если корзина собрана, отправьте заявку — менеджер увидит её со "
                "всеми позициями и основаниями: /order",
                keyboard=self._main_menu(),
            )
        ]


def describe(product: Product) -> str:
    """Короткое описание товара одной строкой — для списков и виджета."""
    parts = [product.name, price_text(product.price), stock_text(product)]
    norm = product.best_norm()
    if norm:
        parts.append(norm.citation)
    return " · ".join(parts)
