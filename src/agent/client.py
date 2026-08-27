"""Клиент Cloud.ru Foundation Models.

API совместим с OpenAI, но зависимость от пакета `openai` здесь не нужна: используются
только `/v1/chat/completions` с вызовом инструментов. Обращение через стандартную
библиотеку избавляет прототип от лишних зависимостей и делает поведение при таймаутах
и ошибках предсказуемым.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Модель не ответила. Диалог должен продолжиться без неё, а не оборваться."""


@dataclass
class ChatClient:
    api_key: str
    base_url: str = "https://foundation-models.api.cloud.ru/v1"
    model: str = "deepseek-ai/DeepSeek-V4-Flash"
    timeout: float = 60.0
    max_tokens: int = 1200

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise LLMError(f"Cloud.ru вернул {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"Cloud.ru недоступен: {exc}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError("Пустой ответ модели")
        return choices[0]["message"]
