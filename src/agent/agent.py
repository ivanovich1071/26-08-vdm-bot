"""Два агента в одном диалоге: консультант и продавец.

Роль на каждый ход выбирает маршрутизатор (`agent/routing.py`), и промпт
собирается под неё. Раньше все четыре файла промптов склеивались в одну простыню
на четыре с лишним тысячи токенов и уходили одному вызову — агенту приходилось
быть сразу справочной, продавцом и охраной, и он выбирал самое простое: показать
товар. Отсюда жалоба заказчика «пропал режим диалога».

Ключевое свойство — деградация без обрыва. Если провайдер не ответил, пробуем
следующего; если легли все — диалог продолжается предложением из каталога.
Бот, который молчит из-за недоступности внешнего сервиса, хуже бота без модели.

Персональные данные до модели не доходят: история приходит сюда уже маскированной
(`core/dialog.Session.remember`), а ответ восстанавливается перед показом.

Три вещи агент делает поверх обычного tool-calling.

**Показывает модели профиль разговора.** Короткая выжимка «что уже известно»
подставляется в системный промпт. Без неё бот переспрашивал возраст детей, который
ему назвали ходом раньше, — это видно в журнале диалогов.

**Проверяет цены и нормативные основания в ответе.** Всё, что похоже на сумму или
на ссылку «пункт такой-то приказа такого-то», должно встречаться среди результатов
инструментов. Не совпало — просим переписать, а если не помогло, отвечаем выдачей
каталога. Выдуманная цена дороже молчания, а выдуманное основание — дороже цены:
по нему принимают закупку.

**Дописывает каталог, когда модель обошлась без него.** Ответ списком общих слов —
«мячи, обручи, скакалки» — промптом не лечится: проверено на живых прогонах.
Поэтому к такому ответу молча добавляется настоящая выдача поиска, с ценами и
пунктами перечня.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from agent.client import ChatClient, LLMError
from agent.providers import LLMRouter
from agent.routing import CONSULT, GUARD, SELL, Decision, Router
from agent.tools import TOOL_SCHEMAS, ToolBox
from agent.verify import (
    describe_refs,
    invented_norm_refs,
    invented_prices,
    prices_in,
    talks_about_goods,
)
from core.ui import Button, Keyboard, Message, ProductCard, ProductList, Response

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TOOL_ROUNDS = 4
HISTORY_LIMIT = 12
# Сколько карточек прикладываем к ответу модели. Заказчик отдельно попросил
# не больше трёх: пять карточек подряд читаются как выгрузка, а не как подбор.
CARDS_SHOWN = 3

# Чего мы ждём от переписанного ответа. Отдельной строкой, чтобы просьба звучала
# одинаково и для выдуманной цены, и для выдуманного пункта приказа.
_REWRITE_HINT = (
    " Названия, цены и пункты перечней бери только из вызовов инструментов. "
    "Номер пункта всегда называй вместе с приказом, а формулировку — только ту, "
    "что вернул инструмент. Перепиши ответ: оставь лишь подтверждённые позиции, "
    "а чего в каталоге нет — так и скажи."
)

# Код 1С в ответе модели: она обязана его называть, чтобы карточки сошлись с
# текстом, а перед показом человеку код вырезается — он служебный.
_CODE_MENTION = re.compile(
    r"\s*[(\[]?\s*(?:код\s*1\s*[СCc]|артикул)\s*:?\s*[A-Za-z0-9А-ЯЁа-яё\-]+\s*[)\]]?",
    re.IGNORECASE,
)

# Из чего собирается промпт роли. Границы идут первыми, чтобы не тонуть в
# середине длинного текста, дальше общая часть, дальше сама роль.
ROLE_PARTS: dict[str, tuple[str, ...]] = {
    CONSULT: ("guard", "common", "consultant"),
    SELL: ("guard", "common", "salesman"),
    GUARD: ("guard",),
}

# Какие инструменты доступны роли. Консультанту каталог не нужен: он объясняет
# документы, а не подбирает. Охране не нужно ничего.
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    # Справочник пунктов приказа консультанту нужен не меньше, чем справка по
    # самому документу: «что такое пункт 2.1.14» — это его вопрос, и отвечать
    # на него по памяти он не должен.
    CONSULT: ("explain_norm", "find_norm_item", "get_cart", "handoff_to_manager"),
    SELL: (),  # пустой кортеж — значит все
    GUARD: ("__none__",),
}


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def tools_for(branch: str) -> list[dict] | None:
    allowed = ROLE_TOOLS.get(branch, ())
    if not allowed:
        return TOOL_SCHEMAS
    chosen = [
        schema
        for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in allowed
    ]
    return chosen or None


def may_show_cards(
    profile,  # noqa: ANN001
    decision: Decision,
    named_positions: bool = False,
) -> tuple[bool, str]:
    """Можно ли приложить к ответу карточки товаров — и почему.

    Заказчик сформулировал правило так: «только после отработки возражений
    выводить карточку с кнопкой подробнее или в корзину, бот должен быть живым,
    а не просто связывать карточки и корзину». Причина решения возвращается
    наружу и пишется в журнал — иначе на прогоне не понять, почему бот промолчал.

    `named_positions` — модель уже назвала конкретные позиции из каталога.
    Тогда требование «сначала выясни учреждение и зону» снимается: 02.09 на
    реплику «чем оснастить спортзал в саду» бот перечислил три позиции с ценами
    и пунктами приказа, а карточек не дал — и человеку было нечем положить их в
    корзину. Товар с ценой в тексте и без кнопки хуже, чем товар с кнопкой.
    Гейт по возражению при этом остаётся: он и есть суть правила заказчика.
    """
    if decision.branch != SELL:
        return False, "ветка консультирования"
    if profile.objection != "none" and not profile.objection_handled:
        return False, f"возражение не снято ({profile.objection})"
    if not profile.ready_to_see and not named_positions:
        return False, "клиент не просил показывать"
    if not profile.task_known and not decision.precise and not named_positions:
        return False, "не выяснены учреждение и зона"
    if named_positions and not (profile.task_known or profile.ready_to_see):
        return True, "позиции уже названы в ответе"
    return True, "задача ясна, клиент готов смотреть"


class SalesAgent:
    def __init__(self, engine, router: LLMRouter, routing: Router | None = None) -> None:  # noqa: ANN001
        self.engine = engine
        self.router = router
        self.routing = routing if routing is not None else Router(router)
        self.prompts = {
            branch: "\n\n".join(
                part for part in (load_prompt(name) for name in names) if part
            )
            for branch, names in ROLE_PARTS.items()
        }

    @property
    def available(self) -> bool:
        return self.router.available

    def reply(self, session, text: str) -> list[Response]:  # noqa: ANN001
        if not self.available:
            return self.engine.offer(session, text)

        decision = self.routing.decide(session, text)
        show_cards, reason = may_show_cards(session.profile, decision)
        session.route = {
            "role": decision.branch,
            "stage": decision.stage,
            "objection": session.profile.objection,
            "objection_handled": session.profile.objection_handled,
            "routed_by": decision.source,
            "cards": {"allowed": show_cards, "reason": reason},
        }

        tools = ToolBox(self.engine, session)
        messages = [
            {"role": "system", "content": self._system_prompt(session, decision)},
            *self._history(session),
        ]

        try:
            answer = self._ask(messages, tools, tools_for(decision.branch))
            session.prices |= tools.prices
            session.norm_refs |= tools.norm_refs
        except LLMError:
            # Провайдеры уже помечены нерабочими и записаны в лог — здесь остаётся
            # только доиграть ход предложением из каталога.
            return self.engine.offer(session, text)

        # Гейт пересчитываем, когда ход уже сыгран: до вызова модели неизвестно,
        # назовёт ли она конкретные позиции, а от этого зависит, будет ли человеку
        # чем воспользоваться.
        show_cards, reason = may_show_cards(session.profile, decision, bool(tools.shown_skus))
        session.route["cards"] = {"allowed": show_cards, "reason": reason}
        if tools.norm_lookups:
            session.route["norm_lookups"] = tools.norm_lookups

        answer = self._verified(answer, messages, tools, text, session)
        if not answer:
            session.route["discarded_answer"] = True
            return self.engine.offer(session, text)

        answer = session.masker.unmask(answer)
        session.remember("assistant", answer)
        session.profile.remember_offered(_unique(tools.shown_skus))
        return self._render(session, tools, answer, text, decision, show_cards)

    # --- Сведение текста ответа с карточками ---------------------------------

    def _mentioned_skus(self, tools: ToolBox, answer: str) -> list[str]:
        """Товары, которые модель действительно назвала в ответе.

        Раньше искались только коды 1С. Модель их не пишет — она пишет названия, —
        и совпадений не было ни разу, а на их месте молча подставлялись первые
        позиции из поиска. Так и вышло, что текст обещал интерактивное зеркало и
        песочницу, а карточками приходили парта логопеда и карточки «Овощи».

        Теперь совпадение ищется двумя способами: по коду, если модель его всё же
        назвала, и по названию. Если не нашлось ничего — карточек не будет:
        показать наугад хуже, чем не показать.
        """
        by_code = [sku for sku in tools.shown_skus if sku in answer]
        if by_code:
            return _unique([sku for sku in by_code if not _rejected(answer, sku)])

        words = _significant(answer)
        pairs = {(words[i], words[i + 1]) for i in range(len(words) - 1)}
        matched = []
        for sku in _unique(tools.shown_skus):
            product = self.engine.index.get(sku)
            if product is None or not _named_in(product.name, words, pairs):
                continue
            # Модель бывает права, отказывая: 01.09 она сама выяснила, что код
            # 45892 — игрушечный бронемобиль, честно об этом написала, а карточка
            # бронемобиля всё равно пришла — «упомянут» и «рекомендован» тут не
            # различались. Сомневаемся — карточку не показываем.
            if _rejected(answer, product.name):
                continue
            matched.append(sku)
        return matched

    # --- Цикл вызова инструментов -------------------------------------------

    def _ask(self, messages: list[dict], tools: ToolBox, schemas: list[dict] | None) -> str:
        """Ход разговора: пробуем провайдеров по очереди, пока кто-то не ответит.

        Каждому даём свою копию сообщений. Цикл вызова инструментов дописывает
        в них ответы модели, и остатки неудачной попытки не должны утекать
        следующему провайдеру: служебные поля у них разные.
        """
        last: LLMError | None = None
        for client in self.router.ready():
            try:
                answer = self._run(client, list(messages), tools, schemas)
            except LLMError as exc:
                self.router.mark_down(client, exc)
                last = exc
                continue
            self.router.mark_up(client)
            return answer
        raise last or LLMError("нет настроенных провайдеров модели")

    def _run(
        self,
        client: ChatClient,
        messages: list[dict],
        tools: ToolBox,
        schemas: list[dict] | None,
    ) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            message = client.complete(messages, tools=schemas)
            account_usage(tools.session, client, message)
            calls = message.get("tool_calls") or []
            if not calls:
                return (message.get("content") or "").strip()

            # Ответ модели возвращаем в историю как есть. Пересобирать его из
            # content и tool_calls нельзя: рассуждающие модели отдают ещё и
            # reasoning_content, а Cloud.ru требует это поле обратно — без него
            # следующий запрос падает с «Missing reasoning_content field».
            messages.append(_assistant_message(message))
            for call in calls:
                function = call.get("function", {})
                arguments = _parse_arguments(function.get("arguments"))
                result = tools.run(function.get("name", ""), arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}
                )

        # Инструменты вызывались снова и снова без итогового ответа — просим завершить.
        messages.append(
            {
                "role": "user",
                "content": "Ответь пользователю по уже собранным данным, без новых вызовов.",
            }
        )
        final = client.complete(messages)
        account_usage(tools.session, client, final)
        return (final.get("content") or "").strip()

    # --- Проверка ответа -------------------------------------------------------

    def _verified(  # noqa: ANN001
        self, answer: str, messages: list[dict], tools: ToolBox, question: str, session
    ) -> str:
        """Ответ, в котором каждая сумма и каждый пункт приказа подтверждены данными.

        Одна попытка исправиться: модель почти всегда переписывает ответ честно,
        когда ей называют конкретные лишние числа. Если и второй ответ выдуман,
        возвращаем пустую строку — вызывающая сторона ответит выдачей каталога.
        """
        # Цены и основания за весь разговор, а не только за этот ход. Отвечая на
        # «дорого», модель ссылается на уже показанные позиции и в инструменты не
        # ходит — проверка по одному ходу отвергала такой ответ дважды подряд и
        # роняла его в выдачу каталога. Поймано на живом прогоне 01.09.
        prices = (
            tools.prices
            | session.prices
            | prices_in(question)
            | prices_in(session.profile.budget or "")
        )
        refs = tools.norm_refs | session.norm_refs
        complaint = self._complaint(answer, prices, refs)
        if not complaint:
            return answer

        log.warning("%s Просим переписать ответ.", complaint)
        messages.append({"role": "assistant", "content": answer, "reasoning_content": ""})
        messages.append({"role": "user", "content": complaint + _REWRITE_HINT})
        try:
            second = self._ask(messages, tools, TOOL_SCHEMAS)
        except LLMError:
            return ""

        prices |= tools.prices
        refs |= tools.norm_refs
        if self._complaint(second, prices, refs):
            log.warning("Ответ выдуман повторно — отвечаем выдачей каталога.")
            return ""
        return second

    def _complaint(
        self, answer: str, prices: set[int], refs: set[tuple[str, str]]
    ) -> str:
        """Что в ответе не подтверждено данными. Пустая строка — всё в порядке."""
        parts: list[str] = []
        invented = invented_prices(answer, prices)
        if invented:
            parts.append(
                "суммы, которых нет в результатах инструментов: "
                + ", ".join(f"{price} ₽" for price in sorted(invented))
            )
        wrong_refs = invented_norm_refs(answer, refs)
        if wrong_refs:
            parts.append(
                "нормативные основания, которых инструменты не возвращали: "
                + describe_refs(wrong_refs)
            )
        return f"В твоём ответе есть {' и '.join(parts)}." if parts else ""

    # --- Сборка ответа --------------------------------------------------------

    def _render(  # noqa: ANN001
        self,
        session,
        tools: ToolBox,
        answer: str,
        question: str,
        decision: Decision,
        show_cards: bool,
    ) -> list[Response]:
        # Карточки показываем по товарам, которые агент действительно назвал: так
        # текст ответа и карточки не расходятся.
        mentioned = self._mentioned_skus(tools, answer) if show_cards else []

        responses: list[Response] = []
        if answer:
            # Коды 1С нужны нам для сведения текста с карточками, но человеку в
            # ответе они ни к чему — это внутренний артикул, а не характеристика.
            responses.append(Message(_without_codes(answer), keyboard=self._keyboard(tools)))

        for sku in mentioned[:CARDS_SHOWN]:
            product = self.engine.index.get(sku)
            if product is None:
                continue
            responses.append(
                ProductCard(
                    product=product,
                    citation=self._citation(session, product),
                    keyboard=Keyboard().row(
                        Button("В корзину", f"add:{sku}"),
                        Button("Подробнее", f"card:{sku}"),
                    ),
                    # Без этих полей карточки от модели приходили без снимка в
                    # обоих каналах, даже когда файл лежал у нас на диске.
                    image=self.engine._image(product),
                    image_path=self.engine.photo_path(product),
                )
            )

        if not responses:
            # Модель промолчала — отвечаем предложением по исходному вопросу.
            return self.engine.offer(session, question)

        # Модель перечислила оборудование словами, не заглянув в каталог. Спорить
        # с ней дорого — целое обращение, — поэтому просто дописываем настоящие
        # позиции: с ценой, наличием и пунктом перечня.
        #
        # Только у продавца. У консультанта эта страховка срабатывала на любом
        # ответе с двумя пунктами списком: человек спрашивал про приказ, получал
        # объяснение — и под ним выдачу каталога. Именно так «пропадал диалог».
        if show_cards and not tools.shown_skus and talks_about_goods(answer):
            found = self.engine.search(
                session,
                self._catalog_query(session, question),
                # Заголовок «Нашлось более 50 позиций по запросу…» здесь не к
                # месту: человек не искал, он получил ответ, к которому мы сами
                # дописываем настоящие позиции.
                title="Вот эти позиции есть в каталоге",
            )
            # Пустая выдача сюда не идёт: «ничего не нашёл» сразу после связного
            # ответа модели выглядит поломкой, а не помощью.
            if any(isinstance(item, ProductList) for item in found):
                log.warning("Ответ без обращения к каталогу — дописываем выдачу поиска.")
                responses += found
        return responses

    def _citation(self, session, product) -> str | None:  # noqa: ANN001
        """Основание для карточки — то же, что бот назвал в тексте.

        Раньше текст брал основание из результата поиска, а карточка считала его
        заново по аудитории профиля, и в одном сообщении оказывались два разных
        приказа: «привязана к приказу 838» в тексте и «позиция 1.13.4.3.1.6 —
        приказ 1057» на карточке под ним.
        """
        for hit in session.last_hits or ():
            if hit.product.sku_1c == product.sku_1c:
                return hit.citation()
        norm = product.norm_for(session.profile.audience, session.profile.room or "")
        return norm.citation if norm else None

    def _catalog_query(self, session, question: str) -> str:  # noqa: ANN001
        """Чем искать, когда искать приходится за модель.

        Реплика пользователя для поиска годится не всегда: «сейчас зал пустой,
        только ремонт сделали» — это про обстоятельства, а не про товар. Профиль
        разговора описывает задачу точнее, и он уже разобран.
        """
        profile = session.profile
        words = [profile.room or "", profile.institution or ""]
        query = " ".join(word for word in words if word).strip()
        return query or question

    def _keyboard(self, tools: ToolBox) -> Keyboard:
        keyboard = Keyboard()
        if tools.handoff_reason:
            keyboard.row(Button("Связаться с менеджером", "menu"))
        keyboard.row(Button("Корзина", "cart"), Button("Оформить", "checkout"))
        return keyboard

    def _system_prompt(self, session, decision: Decision) -> str:  # noqa: ANN001
        """Промпт выбранной роли плюс то, что уже известно об этом разговоре."""
        base = self.prompts.get(decision.branch) or self.prompts[SELL]
        profile = session.profile.as_prompt()
        return f"{base}\n\n{profile}" if profile else base

    def _history(self, session) -> list[dict]:  # noqa: ANN001
        """Переписка для модели.

        Маскировать здесь нечего: в сессию реплики попадают уже с метками вместо
        персональных данных, и на диске лежат в том же виде.
        """
        history = []
        for item in session.history[-HISTORY_LIMIT:]:
            message = {"role": item["role"], "content": item["content"]}
            if item["role"] == "assistant":
                # Само рассуждение не храним, но поле должно присутствовать:
                # валидатор Cloud.ru требует его у каждого ответа ассистента.
                message["reasoning_content"] = ""
            history.append(message)
        return history


def account_usage(session, client: ChatClient, message: dict) -> None:  # noqa: ANN001
    """Складывает расход модели за ход в сессию.

    Ход почти никогда не равен одному обращению: сначала вызовы инструментов,
    потом ответ, иногда ещё и переписывание из-за выдуманной цены. Стоимость
    сценария — это сумма всех, поэтому считаем накопительно.
    """
    usage = message.get("_usage")
    if not usage:
        return
    tokens_in = int(usage.get("tokens_in") or 0)
    tokens_out = int(usage.get("tokens_out") or 0)
    cost = (tokens_in * client.price_in + tokens_out * client.price_out) / 1_000_000

    box = session.usage
    box["provider"] = client.name
    box["model"] = usage.get("model") or client.model
    box["calls"] = int(box.get("calls", 0)) + 1
    box["tokens_in"] = int(box.get("tokens_in", 0)) + tokens_in
    box["tokens_out"] = int(box.get("tokens_out", 0)) + tokens_out
    box["cost_rub"] = round(float(box.get("cost_rub", 0.0)) + cost, 4)


def _assistant_message(message: dict) -> dict:
    """Ответ модели в том виде, в каком его примут обратно.

    Провайдеры расходятся в служебных полях, поэтому ничего не выбрасываем и
    ничего не придумываем: берём пришедшее и добавляем только то, чего нет.
    Наши собственные пометки (они начинаются с подчёркивания) провайдеру,
    разумеется, не возвращаем — он их не поймёт.
    """
    kept = {
        key: value
        for key, value in message.items()
        if value is not None and not key.startswith("_")
    }
    kept.setdefault("role", "assistant")
    kept.setdefault("content", "")
    kept.setdefault("reasoning_content", "")
    return kept


def _parse_arguments(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _unique(items) -> list[str]:  # noqa: ANN001
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _significant(text: str) -> list[str]:
    """Основы слов длиннее двух букв — по ним сверяются названия товаров."""
    from catalog.text import stems

    return [word for word in stems(text) if len(word) > 2 and not word.isdigit()]


# Модель отговаривает от позиции, а не предлагает её.
_REJECTION = re.compile(
    r"не\s+подойд\w+|не\s+подход\w+|не\s+соответству\w+|не\s+рекоменд\w+|"
    r"не\s+относ\w+|не\s+числ\w+|не\s+для\s+|вряд\s+ли|ошибочн\w+|"
    r"это\s+не\s+то|исключ\w+\s+из|игрушечн\w+",
    re.IGNORECASE,
)
# Предложения делим по точке с большой буквы, переводу строки и точке с запятой.
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")


def _rejected(answer: str, needle: str) -> bool:
    """Названо ли это только затем, чтобы отказать.

    Смотрим предложения, где позиция упомянута: если все они содержат отказ,
    карточке под ответом взяться неоткуда.
    """
    words = _significant(needle)
    if not words:
        return False
    mentions = []
    for sentence in _SENTENCE.split(answer or ""):
        low = sentence.lower()
        if needle.lower() in low or all(word in low for word in words[:2]):
            mentions.append(sentence)
    return bool(mentions) and all(_REJECTION.search(sentence) for sentence in mentions)


def _named_in(name: str, words: list[str], pairs: set[tuple[str, str]]) -> bool:
    """Названо ли это в ответе.

    Названия у заказчика начинаются с кода поставщика — «ВТ ПЛ Парта логопеда», —
    а модель пишет «парта логопеда». Поэтому сверяются не строки, а пары соседних
    значимых слов: одно общее слово («набор», «комплект») есть у половины каталога
    и совпадением не является.
    """
    own = _significant(name)
    if not own:
        return False
    if len(own) == 1:
        return own[0] in words
    return any((own[i], own[i + 1]) in pairs for i in range(len(own) - 1))


def _without_codes(answer: str) -> str:
    return _CODE_MENTION.sub("", answer)
