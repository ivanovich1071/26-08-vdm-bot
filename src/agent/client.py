"""Клиент модели по протоколу OpenAI.

Один и тот же класс работает и с Cloud.ru Foundation Models, и с OpenRouter: оба
принимают `/v1/chat/completions` с вызовом инструментов. Отличаются адресом, ключом,
названием модели и парой заголовков — всё это поля, а не отдельный код.

Зависимость от пакета `openai` здесь не нужна: обращение через стандартную библиотеку
избавляет прототип от лишних зависимостей и делает поведение при таймаутах
и ошибках предсказуемым.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Модель не ответила. Диалог должен продолжиться без неё, а не оборваться."""


class LLMAuthError(LLMError):
    """Ключ не принят или на счету нет средств. Повторять запрос бессмысленно."""


@dataclass
class ChatClient:
    api_key: str
    base_url: str = "https://foundation-models.api.cloud.ru/v1"
    model: str = "deepseek-ai/DeepSeek-V4-Flash"
    timeout: float = 60.0
    max_tokens: int = 1200
    # Как провайдер называется в логах и в диагностике: «cloudru», «openrouter».
    name: str = "cloudru"
    # OpenRouter просит указать, откуда пришёл запрос; Cloud.ru лишние заголовки
    # игнорирует, поэтому отдельной ветки в коде не нужно.
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Рубли за миллион токенов — из прайса провайдера. Нужны, чтобы в журнале
    # стояла стоимость хода, а не только их количество: выбирать модель по
    # ощущению «эта пободрее» дорого, а по цифрам — нет.
    price_in: float = 0.0
    price_out: float = 0.0

    @property
    def host(self) -> str:
        from urllib.parse import urlparse

        return urlparse(self.base_url).hostname or ""

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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            message = f"{self.name} вернул {exc.code}: {detail}"
            # 401 — не тот ключ, 402 — нет средств, 403 — ключу закрыт доступ.
            # Пробовать ещё раз или ждать паузу смысла нет, нужен человек.
            if exc.code in {401, 402, 403}:
                raise LLMAuthError(message) from exc
            raise LLMError(message) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"{self.name} недоступен: {exc}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name}: пустой ответ модели")

        # Расход провайдер сообщает в каждом ответе, а мы его выбрасывали — и
        # посчитать, во сколько обходится один разговор, было нечем. Кладём его
        # в само сообщение под служебным ключом: сигнатура метода не меняется,
        # а обратно провайдеру такое сообщение не уходит — там пересобирается
        # только то, что он прислал сам.
        message = dict(choices[0]["message"])
        message["_usage"] = _usage(body, self.model)
        return message

    @property
    def usage_prices(self) -> tuple[float, float]:
        """Рубли за миллион токенов: вход, выход."""
        return self.price_in, self.price_out


def _usage(body: dict[str, Any], model: str) -> dict[str, Any]:
    raw = body.get("usage") or {}
    return {
        "model": model,
        "tokens_in": int(raw.get("prompt_tokens") or 0),
        "tokens_out": int(raw.get("completion_tokens") or 0),
    }
