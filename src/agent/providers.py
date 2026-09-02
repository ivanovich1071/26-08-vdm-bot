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

from agent.client import ChatClient, LLMAuthError, LLMError, LLMPaymentError

log = logging.getLogger(__name__)

# Сколько не трогаем провайдера после сбоя. Без паузы каждое сообщение снова ждало бы
# полный таймаут, и бот выглядел бы зависшим у всех пользователей сразу.
COOLDOWN_SECONDS = 300.0
# Отказ по ключу сам не пройдёт — тут нужен человек, а не повтор. Отказ по
# деньгам (402) сюда не попадает: счёт пополняют, и провайдер оживает без нас.
# Пока эти два случая жили под одним сроком, бот после пополнения Cloud.ru
# ещё полчаса разговаривал запасной моделью — поймано на прогоне 02.09.
AUTH_COOLDOWN_SECONDS = 1800.0


def _pause_for(exc: Exception) -> float:
    if isinstance(exc, LLMPaymentError):
        return COOLDOWN_SECONDS
    return AUTH_COOLDOWN_SECONDS if isinstance(exc, LLMAuthError) else COOLDOWN_SECONDS


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
        pause = _pause_for(exc)
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
            price_in=settings.cloudru_price_in,
            price_out=settings.cloudru_price_out,
        )
    if settings.openrouter_api_key:
        clients["openrouter"] = ChatClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            model=settings.openrouter_model,
            timeout=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            name="openrouter",
            price_in=settings.openrouter_price_in,
            price_out=settings.openrouter_price_out,
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


def warm_up(router: LLMRouter) -> None:
    """Короткий вызов каждому провайдеру при запуске.

    Без него первый живой человек платит за проверку связи собственным
    ожиданием: если Cloud.ru не открывается, его таймаут в шестьдесят секунд
    достаётся первому же сообщению, и только потом ход уходит запасному.
    Здесь тот же отказ стоит времени запуска, а не времени пользователя.

    Ответ нам не нужен — важно, поднимется ошибка или нет.
    """
    for client in list(router.clients):
        try:
            client.complete(
                [{"role": "user", "content": "пинг"}], temperature=0.0, max_tokens=1
            )
        except LLMError as exc:
            router.mark_down(client, exc)
            continue
        router.mark_up(client)
        log.info("Провайдер %s отвечает (%s).", client.name, client.model)


__all__ = [
    "LLMRouter",
    "build_router",
    "warm_up",
    "LLMError",
    "LLMAuthError",
    "LLMPaymentError",
    "COOLDOWN_SECONDS",
    "AUTH_COOLDOWN_SECONDS",
]
