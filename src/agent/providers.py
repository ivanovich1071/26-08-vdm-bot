"""Выбор провайдера модели.

У прототипа их два, и это не роскошь. Cloud.ru — то, где всё будет работать
у заказчика: российское облако, российская площадка, оплата в рублях. OpenRouter —
то, где диалог можно проверить прямо сейчас: с машины разработки Cloud.ru не всегда
резолвится, и без запасного пути любая проверка упирается в сеть, а не в код.

Порядок приоритетов задаётся настройкой. Отказ провайдера — не конец разговора:
пробуем следующего, и только когда легли все, ядро отвечает поиском по каталогу.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from agent.client import ChatClient, LLMAuthError, LLMError

log = logging.getLogger(__name__)

# Сколько не трогаем провайдера после сбоя. Без паузы каждое сообщение снова ждало бы
# полный таймаут, и бот выглядел бы зависшим у всех пользователей сразу.
COOLDOWN_SECONDS = 300.0
# Отказ по ключу или деньгам сам не пройдёт — тут нужен человек, а не повтор.
AUTH_COOLDOWN_SECONDS = 1800.0


@dataclass
class LLMRouter:
    clients: list[ChatClient] = field(default_factory=list)
    _blocked_until: dict[str, float] = field(default_factory=dict, init=False)
    _last_error: dict[str, str] = field(default_factory=dict, init=False)

    @property
    def configured(self) -> bool:
        return bool(self.clients)

    @property
    def available(self) -> bool:
        """Есть ли хоть один провайдер, к которому сейчас можно обратиться."""
        return bool(self.ready())

    def ready(self) -> list[ChatClient]:
        now = time.monotonic()
        return [c for c in self.clients if self._blocked_until.get(c.name, 0.0) <= now]

    def mark_down(self, client: ChatClient, exc: Exception) -> None:
        pause = AUTH_COOLDOWN_SECONDS if isinstance(exc, LLMAuthError) else COOLDOWN_SECONDS
        self._blocked_until[client.name] = time.monotonic() + pause
        self._last_error[client.name] = str(exc)[:300]
        log.warning(
            "Провайдер %s отключён на %.0f с: %s", client.name, pause, str(exc)[:200]
        )

    def mark_up(self, client: ChatClient) -> None:
        self._blocked_until.pop(client.name, None)
        self._last_error.pop(client.name, None)

    def status(self) -> list[dict[str, object]]:
        """Состояние провайдеров для логов и диагностики."""
        now = time.monotonic()
        return [
            {
                "name": c.name,
                "model": c.model,
                "host": c.host,
                "blocked_for": max(0.0, self._blocked_until.get(c.name, 0.0) - now),
                "last_error": self._last_error.get(c.name),
            }
            for c in self.clients
        ]


def build_router(settings) -> LLMRouter:  # noqa: ANN001 — core.config.Settings
    """Собирает список провайдеров в порядке, заданном настройкой LLM_PROVIDER."""
    clients: dict[str, ChatClient] = {}

    if settings.cloudru_api_key:
        clients["cloudru"] = ChatClient(
            api_key=settings.cloudru_api_key,
            base_url=settings.cloudru_base_url,
            model=settings.cloudru_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            name="cloudru",
        )
    if settings.openrouter_api_key:
        clients["openrouter"] = ChatClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            name="openrouter",
            extra_headers={
                "HTTP-Referer": settings.site_url,
                "X-Title": "ELTI-KUDITS catalog bot",
            },
        )

    order = ["cloudru", "openrouter"] if settings.llm_provider == "auto" else [settings.llm_provider]
    chosen = [clients[name] for name in order if name in clients]
    if not chosen:
        log.warning(
            "Ни один провайдер модели не настроен (LLM_PROVIDER=%s): "
            "бот отвечает поиском по каталогу.",
            settings.llm_provider,
        )
    else:
        log.info("Провайдеры модели: %s", ", ".join(f"{c.name}/{c.model}" for c in chosen))
    return LLMRouter(clients=chosen)


__all__ = ["LLMRouter", "build_router", "LLMError", "LLMAuthError", "COOLDOWN_SECONDS"]
