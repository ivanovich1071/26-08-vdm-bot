"""Агент: консультант и продажник в одном диалоге.

Ключевое свойство — деградация без обрыва. Если провайдер не ответил, пробуем
следующего; если легли все — диалог продолжается обычным поиском по каталогу.
Бот, который молчит из-за недоступности внешнего сервиса, хуже бота без модели.

Персональные данные до модели не доходят: текст маскируется перед отправкой и
восстанавливается в ответе.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.client import ChatClient, LLMError
from agent.providers import LLMRouter
from agent.tools import TOOL_SCHEMAS, ToolBox
from core.ui import Button, Keyboard, Message, ProductCard, Response
from privacy.masking import Masker

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

        masker = Masker()
        tools = ToolBox(self.engine, session)
        messages = [
            {"role": "system", "content": self.system_prompt},
            *self._history(session, masker),
            {"role": "user", "content": masker.mask(text)},
        ]

        try:
            answer = self._ask(messages, tools)
        except LLMError:
            # Провайдеры уже помечены нерабочими и записаны в лог — здесь остаётся
            # только доиграть ход поиском по каталогу.
            return self.engine.search(session, text)

        answer = masker.unmask(answer)
        session.history.append({"role": "assistant", "content": answer})
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
            # Брать последнюю запись истории нельзя: там уже лежит пустой ответ.
            return self.engine.search(session, question)
        return responses

    def _keyboard(self, tools: ToolBox) -> Keyboard:
        keyboard = Keyboard()
        if tools.handoff_reason:
            keyboard.row(Button("Связаться с менеджером", "menu"))
        keyboard.row(Button("Корзина", "cart"), Button("Оформить", "checkout"))
        return keyboard

    def _history(self, session, masker: Masker) -> list[dict]:  # noqa: ANN001
        history = []
        for item in session.history[-HISTORY_LIMIT:-1]:
            message = {"role": item["role"], "content": masker.mask(item["content"])}
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
