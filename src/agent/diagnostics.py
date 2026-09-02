"""Проверка провайдеров модели по шагам.

Появилась после дня, потраченного на выяснение, почему «модель отвалилась».
В логах лежали три разные причины подряд — нет денег на счету, наш баг в формате
сообщений и просто отсутствие сети, — а снаружи все три выглядели одинаково:
бот молча отвечает поиском.

Поэтому проверка идёт по шагам и на каждом говорит, что именно не так: имя не
резолвится, порт не отвечает, ключ не принят, деньги кончились, ответ пришёл.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass

from agent.agent import _assistant_message
from agent.client import ChatClient, LLMAuthError, LLMError

# Инструмент для проверки полного круга: запрос → вызов инструмента → результат →
# ответ. Настоящий каталог здесь не нужен, важен сам формат обмена.
PROBE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Поиск товаров в каталоге по словам.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]

PROBE_RESULT = json.dumps(
    {
        "found": 1,
        "products": [{"sku_1c": "TEST-1", "name": "Мяч резиновый 200 мм", "price": "290 ₽"}],
    },
    ensure_ascii=False,
)


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        timing = f" ({self.seconds:.1f} с)" if self.seconds >= 0.1 else ""
        return f"  {mark} {self.name}{timing}: {self.detail}"


def check(client: ChatClient) -> list[Step]:
    """Проверяет одного провайдера. Останавливается на первом провале."""
    steps: list[Step] = []

    started = time.monotonic()
    try:
        address = socket.gethostbyname(client.host)
    except OSError as exc:
        steps.append(Step("DNS", False, f"{client.host} не резолвится: {exc}", _since(started)))
        return steps
    steps.append(Step("DNS", True, f"{client.host} → {address}", _since(started)))

    started = time.monotonic()
    try:
        socket.create_connection((client.host, 443), timeout=10).close()
    except OSError as exc:
        steps.append(Step("TCP 443", False, f"соединение не установлено: {exc}", _since(started)))
        return steps
    steps.append(Step("TCP 443", True, "соединение установлено", _since(started)))

    started = time.monotonic()
    try:
        message = client.complete(
            [{"role": "user", "content": "Ответь одним коротким предложением, что ты на связи."}],
            temperature=0.1,
        )
    except LLMAuthError as exc:
        steps.append(Step("Ключ", False, _short(exc), _since(started)))
        return steps
    except LLMError as exc:
        steps.append(Step("Ответ модели", False, _short(exc), _since(started)))
        return steps
    steps.append(
        Step("Ответ модели", True, (message.get("content") or "").strip()[:120], _since(started))
    )

    steps.append(_tool_round(client))
    return steps


def _tool_round(client: ChatClient) -> Step:
    """Круг с вызовом инструмента — здесь и вскрылась потеря reasoning_content."""
    started = time.monotonic()
    messages: list[dict] = [
        {
            "role": "system",
            "content": "Ты консультант магазина. Товары ищи только через инструмент.",
        },
        {"role": "user", "content": "Найди мячи для спортивного зала."},
    ]
    try:
        message = client.complete(messages, tools=PROBE_TOOL)
        calls = message.get("tool_calls") or []
        if not calls:
            return Step(
                "Вызов инструмента",
                True,
                "модель ответила текстом, инструмент не вызвала",
                _since(started),
            )
        messages.append(_assistant_message(message))
        for call in calls:
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id", ""), "content": PROBE_RESULT}
            )
        final = client.complete(messages, tools=PROBE_TOOL)
    except LLMError as exc:
        return Step("Вызов инструмента", False, _short(exc), _since(started))

    text = (final.get("content") or "").strip()
    return Step("Вызов инструмента", bool(text), text[:120] or "пустой ответ", _since(started))


def report(clients: list[ChatClient], router=None) -> str:  # noqa: ANN001 — providers.LLMRouter
    """Человекочитаемый отчёт по всем настроенным провайдерам.

    `router` показывает, кто из них сейчас в паузе после сбоя. Паузу держит тот
    процесс, который на неё нарвался, поэтому пауза работающего бота здесь не
    видна — об этом сказано в самом отчёте, чтобы «у меня проверка зелёная, а
    бот отвечает другой моделью» не выглядело загадкой.
    """
    if not clients:
        return (
            "Ни один провайдер не настроен.\n"
            "Задайте CLOUDRU_API_KEY или OPENROUTER_API_KEY в .env — без ключа бот\n"
            "работает, но отвечает поиском по каталогу, без диалога."
        )

    blocked = {
        row["name"]: float(row["blocked_for"])
        for row in (router.status() if router is not None else [])
        if float(row["blocked_for"]) > 0
    }

    lines: list[str] = []
    working: list[str] = []
    for client in clients:
        lines.append(f"\n{client.name} · {client.model} · {client.base_url}")
        pause = blocked.get(client.name)
        if pause:
            lines.append(f"  · в паузе после сбоя ещё {pause:.0f} с")
        steps = check(client)
        lines += [str(step) for step in steps]
        if steps and all(step.ok for step in steps):
            working.append(client.name)

    lines.append("")
    if working:
        lines.append(f"Готов к работе: {', '.join(working)}. Диалоговый режим включится сам.")
        lines.append(
            "Проверка идёт отдельным процессом: паузу после сбоя каждый держит свою, "
            "и запущенный бот мог остаться на запасном провайдере — тогда его надо "
            "перезапустить."
        )
    else:
        lines.append(
            "Ни один провайдер не отвечает — бот будет работать поиском по каталогу.\n"
            "Смотрите, на каком шаге отказ: DNS и TCP — это сеть, «Ключ» — деньги\n"
            "или доступ, «Вызов инструмента» — формат обмена."
        )
    return "\n".join(lines)


def _since(started: float) -> float:
    return time.monotonic() - started


def _short(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200]
