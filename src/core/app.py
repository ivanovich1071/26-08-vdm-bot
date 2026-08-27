"""Сборка приложения: одна точка, где всё соединяется.

Адаптеры получают готовый движок диалога и ничего не знают о том, как он собран,
поэтому замена хранилища, приёмника заказов или модели не расходится по каналам.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.agent import SalesAgent
from agent.client import ChatClient
from catalog.repository import load_index
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from media.fetcher import DEFAULT_USER_AGENT, PageFetcher
from media.service import MediaService
from observability.dialog_log import DialogLogger
from orders.service import OrderService, build_sink

log = logging.getLogger(__name__)


def build_engine(settings: Settings | None = None) -> DialogEngine:
    settings = settings or Settings.from_env()
    index = load_index(settings.kb_path)
    storage = Storage(settings.storage_path)
    orders = OrderService(storage, build_sink(settings))

    agent = None
    if settings.llm_enabled:
        agent = SalesAgent(
            engine=None,  # проставим после создания движка: агенту нужен сам движок
            client=ChatClient(
                api_key=settings.cloudru_api_key,
                base_url=settings.cloudru_base_url,
                model=settings.cloudru_model,
                timeout=settings.llm_timeout_seconds,
                max_tokens=settings.llm_max_tokens,
            ),
        )
    else:
        log.warning(
            "CLOUDRU_API_KEY не задан: бот работает без модели, "
            "отвечает поиском по каталогу."
        )

    dialog_log = DialogLogger(
        path=Path(settings.dialog_log_path),
        enabled=settings.dialog_log_enabled,
        mask_personal_data=settings.dialog_log_mask_pdn,
    )
    media = MediaService(
        storage=storage,
        fetcher=PageFetcher(
            user_agent=settings.media_user_agent or DEFAULT_USER_AGENT,
            min_interval=settings.media_min_interval,
            respect_robots=settings.media_respect_robots,
        ),
        enabled=settings.media_enabled,
    )
    engine = DialogEngine(
        index, storage, orders, settings, agent=agent, dialog_log=dialog_log, media=media
    )
    if agent is not None:
        agent.engine = engine
    log.info(
        "Каталог загружен: %s позиций, приёмник заказов — %s, журнал диалогов — %s",
        len(index.products),
        getattr(orders.sink, "name", "?"),
        settings.dialog_log_path if settings.dialog_log_enabled else "выключен",
    )
    if not settings.media_enabled:
        log.info(
            "Догрузка фотографий с сайта выключена (MEDIA_ENABLED=0): "
            "показываем только то, что уже собрано в базе знаний."
        )
    return engine
