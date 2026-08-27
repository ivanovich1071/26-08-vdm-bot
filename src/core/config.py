"""Настройки приложения.

Читаются из окружения и файла .env. Ничего не зашито в код: перенос прототипа
с нашего аккаунта Cloud.ru на аккаунт заказчика — это смена переменных окружения.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE = Path(".env")


def load_env(path: Path = ENV_FILE) -> None:
    """Простое чтение .env: без внешних зависимостей и без перезаписи окружения."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    # Каталог
    kb_path: str = "data/kb/products.jsonl"
    storage_path: str = "data/vdm.sqlite3"

    # Cloud.ru Foundation Models
    cloudru_api_key: str = ""
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    cloudru_model: str = "deepseek-ai/DeepSeek-V4-Flash"
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 1200

    # Каналы
    telegram_token: str = ""
    max_token: str = ""
    site_url: str = "https://vdm.ru"
    manager_contact: str = "+7 (495) 646-01-40, elti@vdm.ru"

    # Заказы
    order_sink: str = "jsonl"  # jsonl | google_sheets | bitrix24
    google_sheets_id: str = ""
    google_credentials_file: str = "secrets/google-service-account.json"
    orders_jsonl_path: str = "data/orders.jsonl"

    # Фотографии товаров с сайта заказчика.
    # Сайт принадлежит заказчику, бот делается для него же, поэтому сбор включён.
    # Осталось единственное ограничение — частота запросов: это рабочий сайт
    # с живыми покупателями, и класть его своей же выгрузкой незачем.
    media_enabled: bool = True
    media_min_interval: float = 1.0
    media_user_agent: str = ""
    media_respect_robots: bool = False

    # Журнал диалогов (сырьё для доработки промптов)
    dialog_log_enabled: bool = True
    dialog_log_path: str = "data/dialogs"
    # По умолчанию персональные данные в журнал не пишутся. Отключать только
    # с письменного согласия заказчика и под конкретную задачу.
    dialog_log_mask_pdn: bool = True

    # Виджет
    widget_allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:8000"])
    widget_host: str = "0.0.0.0"
    widget_port: int = 8000

    @classmethod
    def from_env(cls) -> Settings:
        load_env()
        env = os.environ
        origins = env.get("WIDGET_ALLOWED_ORIGINS", "http://localhost:8000")
        return cls(
            kb_path=env.get("KB_PATH", cls.kb_path),
            storage_path=env.get("STORAGE_PATH", cls.storage_path),
            cloudru_api_key=env.get("CLOUDRU_API_KEY", ""),
            cloudru_base_url=env.get("CLOUDRU_BASE_URL", cls.cloudru_base_url),
            cloudru_model=env.get("CLOUDRU_MODEL", cls.cloudru_model),
            llm_timeout_seconds=float(env.get("LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds)),
            llm_max_tokens=int(env.get("LLM_MAX_TOKENS", cls.llm_max_tokens)),
            telegram_token=env.get("TELEGRAM_TOKEN", ""),
            max_token=env.get("MAX_TOKEN", ""),
            site_url=env.get("SITE_URL", cls.site_url),
            manager_contact=env.get("MANAGER_CONTACT", cls.manager_contact),
            order_sink=env.get("ORDER_SINK", cls.order_sink),
            google_sheets_id=env.get("GOOGLE_SHEETS_ID", ""),
            google_credentials_file=env.get(
                "GOOGLE_CREDENTIALS_FILE", cls.google_credentials_file
            ),
            orders_jsonl_path=env.get("ORDERS_JSONL_PATH", cls.orders_jsonl_path),
            media_enabled=env.get("MEDIA_ENABLED", "1") not in {"0", "false", "no"},
            media_min_interval=float(env.get("MEDIA_MIN_INTERVAL", cls.media_min_interval)),
            media_user_agent=env.get("MEDIA_USER_AGENT", ""),
            media_respect_robots=env.get("MEDIA_RESPECT_ROBOTS", "0")
            not in {"0", "false", "no"},
            dialog_log_enabled=env.get("DIALOG_LOG_ENABLED", "1") not in {"0", "false", "no"},
            dialog_log_path=env.get("DIALOG_LOG_PATH", cls.dialog_log_path),
            dialog_log_mask_pdn=env.get("DIALOG_LOG_MASK_PDN", "1") not in {"0", "false", "no"},
            widget_allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
            widget_host=env.get("WIDGET_HOST", cls.widget_host),
            widget_port=int(env.get("WIDGET_PORT", cls.widget_port)),
        )

    @property
    def llm_enabled(self) -> bool:
        return bool(self.cloudru_api_key)
