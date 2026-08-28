"""Агент: консультант и продажник в одном диалоге.

Ключевое свойство — деградация без обрыва. Если провайдер не ответил, пробуем
следующего; если легли все — диалог продолжается обычным поиском по каталогу.
Бот, который молчит из-за недоступности внешнего сервиса, хуже бота без модели.

Персональные данные до модели не доходят: история приходит сюда уже маскированной
(`core/dialog.Session.remember`), а ответ восстанавливается перед показом.

Три вещи агент делает поверх обычного tool-calling.

**Показывает модели профиль разговора.** Короткая выжимка «что уже известно»
подставляется в системный промпт. Без неё бот переспрашивал возраст детей, который
ему назвали ходом раньше, — это видно в журнале диалогов.

**Проверяет цены в ответе.** Всё, что похоже на сумму, должно встречаться среди
результатов инструментов. Не совпало — просим переписать, а если не помогло,
отвечаем выдачей каталога. Выдуманная цена дороже молчания.

**Дописывает каталог, когда модель обошлась без него.** Ответ списком общих слов —
«мячи, обручи, скакалки» — промптом не лечится: проверено на живых прогонах.
Поэтому к такому ответу молча добавляется настоящая выдача поиска, с ценами и
пунктами перечня.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.client import ChatClient, LLMError
from agent.providers import LLMRouter
from agent.tools import TOOL_SCHEMAS, ToolBox
from agent.verify import invented_prices, prices_in, talks_about_goods
from core.ui import Button, Keyboard, Message, ProductCard, ProductList, Response

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TOOL_ROUNDS = 4
HISTORY_LIMIT = 12

# Порядок склейки промптов: сначала маршрутизация веток, потом роли, в конце —
# границы. Так правила защиты не тонут в середине длинного текста.
PROMPT_PARTS = ("router", "consultant", "salesman", "guard")


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class SalesAgent:
    def __init__(self, engine, router: LLMRouter) -> None:  # noqa: ANN001
        self.engine = engine
        self.router = router
        self.system_prompt = "\n\n".join(
            part for part in (load_prompt(name) for name in PROMPT_PARTS) if part
        )

    @property
    def available(self) -> bool:
        return self.router.available

    def reply(self, session, text: str) -> list[Response]:  # noqa: ANN001
        if not self.available:
            return self.engine.search(session, text)

        tools = ToolBox(self.engine, session)
        messages = [
            {"role": "system", "content": self._system_prompt(session)},
            *self._history(session),
        ]

        try:
            answer = self._ask(messages, tools)
        except LLMError:
            # Провайдеры уже помечены нерабочими и записаны в лог — здесь остаётся
            # только доиграть ход поиском по каталогу.
            return self.engine.search(session, text)

        answer = self._without_invented_prices(answer, messages, tools, text, session)
        if not answer:
            return self.engine.search(session, text)

        answer = session.masker.unmask(answer)
        session.remember("assistant", answer)
        session.profile.remember_offered(_unique(tools.shown_skus))
        return self._render(session, tools, answer, text)

    # --- Цикл вызова инструментов -------------------------------------------

    def _ask(self, messages: list[dict], tools: ToolBox) -> str:
        """Ход разговора: пробуем провайдеров по очереди, пока кто-то не ответит.

        Каждому даём свою копию сообщений. Цикл вызова инструментов дописывает
        в них ответы модели, и остатки неудачной попытки не должны утекать
        следующему провайдеру: служебные поля у них разные.
        """
        last: LLMError | None = None
        for client in self.router.ready():
            try:
                answer = self._run(client, list(messages), tools)
            except LLMError as exc:
                self.router.mark_down(client, exc)
                last = exc
                continue
            self.router.mark_up(client)
            return answer
        raise last or LLMError("нет настроенных провайдеров модели")

    def _run(self, client: ChatClient, messages: list[dict], tools: ToolBox) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            message = client.complete(messages, tools=TOOL_SCHEMAS)
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
        return (client.complete(messages).get("content") or "").strip()

    # --- Проверка ответа -------------------------------------------------------

    def _without_invented_prices(  # noqa: ANN001
        self, answer: str, messages: list[dict], tools: ToolBox, question: str, session
    ) -> str:
        """Ответ, в котором каждая сумма подтверждена данными.

        Одна попытка исправиться: модель почти всегда переписывает ответ честно,
        когда ей называют конкретные лишние числа. Если и второй ответ выдуман,
        возвращаем пустую строку — вызывающая сторона ответит выдачей каталога.
        """
        allowed = tools.prices | prices_in(question) | prices_in(session.profile.budget or "")
        invented = invented_prices(answer, allowed)
        if not invented:
            return answer

        log.warning(
            "Модель назвала суммы, которых нет в данных: %s. Просим переписать ответ.",
            ", ".join(str(price) for price in sorted(invented)),
        )
        messages.append({"role": "assistant", "content": answer, "reasoning_content": ""})
        messages.append(
            {
                "role": "user",
                "content": (
                    "В твоём ответе есть суммы, которых нет в результатах инструментов: "
                    + ", ".join(f"{price} ₽" for price in sorted(invented))
                    + ". Названия и цены товаров бери только из вызовов инструментов. "
                    "Перепиши ответ: оставь лишь подтверждённые позиции, а чего в "
                    "каталоге нет — так и скажи."
                ),
            }
        )
        try:
            second = self._ask(messages, tools)
        except LLMError:
            return ""

        allowed |= tools.prices
        if invented_prices(second, allowed):
            log.warning("Ответ выдуман повторно — отвечаем выдачей каталога.")
            return ""
        return second

    # --- Сборка ответа --------------------------------------------------------

    def _render(  # noqa: ANN001
        self, session, tools: ToolBox, answer: str, question: str
    ) -> list[Response]:
        responses: list[Response] = []
        if answer:
            responses.append(Message(answer, keyboard=self._keyboard(tools)))

        # Карточки показываем по товарам, которые агент действительно назвал: так
        # текст ответа и карточки не расходятся.
        mentioned = _unique(sku for sku in tools.shown_skus if sku in answer) or _unique(
            tools.shown_skus
        )
        for sku in mentioned[:3]:
            product = self.engine.index.get(sku)
            if product is None:
                continue
            norm = product.best_norm()
            responses.append(
                ProductCard(
                    product=product,
                    citation=norm.citation if norm else None,
                    keyboard=Keyboard().row(
                        Button("В корзину", f"add:{sku}"),
                        Button("Подробнее", f"card:{sku}"),
                    ),
                )
            )

        if not responses:
            # Модель промолчала — отвечаем поиском по исходному вопросу.
            return self.engine.search(session, question)

        # Модель перечислила оборудование словами, не заглянув в каталог. Спорить
        # с ней дорого — целое обращение, — поэтому просто дописываем настоящие
        # позиции: с ценой, наличием и пунктом перечня.
        if not tools.shown_skus and talks_about_goods(answer):
            found = self.engine.search(session, self._catalog_query(session, question))
            # Пустая выдача сюда не идёт: «ничего не нашёл» сразу после связного
            # ответа модели выглядит поломкой, а не помощью.
            if any(isinstance(item, ProductList) for item in found):
                log.warning("Ответ без обращения к каталогу — дописываем выдачу поиска.")
                responses += found
        return responses

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

    def _system_prompt(self, session) -> str:  # noqa: ANN001
        """Промпт роли плюс то, что уже известно об этом разговоре."""
        profile = session.profile.as_prompt()
        return f"{self.system_prompt}\n\n{profile}" if profile else self.system_prompt

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


def _assistant_message(message: dict) -> dict:
    """Ответ модели в том виде, в каком его примут обратно.

    Провайдеры расходятся в служебных полях, поэтому ничего не выбрасываем и
    ничего не придумываем: берём пришедшее и добавляем только то, чего нет.
    """
    kept = {key: value for key, value in message.items() if value is not None}
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
