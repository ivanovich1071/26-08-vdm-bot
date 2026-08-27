"""Агент: консультант и продажник в одном диалоге.

Ключевое свойство — деградация без обрыва. Если ключа Cloud.ru нет, модель не ответила
или исчерпала попытки, диалог продолжается обычным поиском по каталогу. Бот, который
молчит из-за недоступности внешнего сервиса, хуже бота без модели.

Персональные данные до модели не доходят: текст маскируется перед отправкой и
восстанавливается в ответе.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent.client import ChatClient, LLMError
from agent.tools import TOOL_SCHEMAS, ToolBox
from core.ui import Button, Keyboard, Message, ProductCard, Response
from privacy.masking import Masker

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_TOOL_ROUNDS = 4
HISTORY_LIMIT = 12

# Если модель недоступна, откат на поиск происходит после таймаута. Без паузы
# каждое следующее сообщение снова ждало бы полный таймаут, и бот выглядел бы
# зависшим у всех пользователей сразу. Поэтому после сбоя не трогаем модель
# несколько минут и отвечаем поиском мгновенно.
COOLDOWN_SECONDS = 300


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class SalesAgent:
    def __init__(self, engine, client: ChatClient) -> None:  # noqa: ANN001
        self.engine = engine
        self.client = client
        self.system_prompt = "\n\n".join(
            part for part in (load_prompt("consultant"), load_prompt("salesman")) if part
        )
        self._unavailable_until = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self._unavailable_until

    def _mark_unavailable(self) -> None:
        self._unavailable_until = time.monotonic() + COOLDOWN_SECONDS

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
            answer = self._run(messages, tools)
        except LLMError as exc:
            self._mark_unavailable()
            log.warning(
                "Модель недоступна, отвечаем поиском и не обращаемся к ней %s с: %s",
                COOLDOWN_SECONDS,
                exc,
            )
            return self.engine.search(session, text)

        answer = masker.unmask(answer)
        session.history.append({"role": "assistant", "content": answer})
        return self._render(session, tools, answer, text)

    # --- Цикл вызова инструментов -------------------------------------------

    def _run(self, messages: list[dict], tools: ToolBox) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            message = self.client.complete(messages, tools=TOOL_SCHEMAS)
            calls = message.get("tool_calls") or []
            if not calls:
                return (message.get("content") or "").strip()

            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
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
        return (self.client.complete(messages).get("content") or "").strip()

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
        return [
            {"role": item["role"], "content": masker.mask(item["content"])}
            for item in session.history[-HISTORY_LIMIT:-1]
        ]


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
