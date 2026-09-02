"""Сборка приложения: одна точка, где всё соединяется.

Адаптеры получают готовый движок диалога и ничего не знают о том, как он собран,
поэтому замена хранилища, приёмника заказов или модели не расходится по каналам.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.agent import SalesAgent
from agent.providers import build_router, warm_up
from catalog.repository import load_index
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from media.fetcher import DEFAULT_USER_AGENT, PageFetcher
from media.files import PhotoStore
from media.service import MediaService
from observability.dialog_log import DialogLogger
from orders.service import OrderService, build_sink

log = logging.getLogger(__name__)


def build_engine(settings: Settings | None = None, warm_llm: bool = False) -> DialogEngine:
    """Готовый движок диалога.

    `warm_llm` включают долгоживущие каналы — Telegram и виджет. Разовым
    командам (`run.py search`) прогрев ни к чему: они и так живут секунду.
    """
    settings = settings or Settings.from_env()
    index = load_index(settings.kb_path)
    storage = Storage(settings.storage_path)
    # Срок хранения переписки соблюдается при каждом старте, а не только при
    # чтении: тот, кто перестал писать, сам за собой не почистит.
    expired = storage.purge_expired_dialogs()
    if expired:
        log.info("Удалено разговоров по сроку хранения: %s", expired)
    orders = OrderService(storage, build_sink(settings))

    router = build_router(settings)
    if warm_llm and router.configured:
        warm_up(router)
    agent = None
    if router.configured:
        # Движок агенту нужен, но сам он создаётся ниже — проставим после.
        agent = SalesAgent(engine=None, router=router)

    dialog_log = DialogLogger(
        path=Path(settings.dialog_log_path),
        enabled=settings.dialog_log_enabled,
        mask_personal_data=settings.dialog_log_mask_pdn,
    )
    fetcher = PageFetcher(
        user_agent=settings.media_user_agent or DEFAULT_USER_AGENT,
        min_interval=settings.media_min_interval,
        respect_robots=settings.media_respect_robots,
    )
    media = MediaService(
        storage=storage,
        fetcher=fetcher,
        enabled=settings.media_enabled,
        photos=PhotoStore(fetcher=fetcher, root=Path(settings.media_dir)),
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
