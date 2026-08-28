"""Хранилище прототипа.

Целевое хранилище — PostgreSQL: там же будет шифрование контактов через pgcrypto
и разделение схем. Пока прототип должен запускаться без инфраструктуры, поэтому
здесь SQLite из стандартной библиотеки. Все обращения идут через этот класс, так что
замена движка не трогает ни ядро, ни адаптеры.

Важно для ФЗ-152: журнал согласий только пополняется и никогда не переписывается,
а удаление данных субъекта чистит корзину, заказы и контакты, оставляя в журнале
запись о самом факте согласия и его отзыве.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from core.models import Cart, CartItem, Customer, Order

SCHEMA = """
CREATE TABLE IF NOT EXISTS carts (
    user_id    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    channel    TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS consents (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    channel      TEXT NOT NULL,
    text_version TEXT NOT NULL,
    action       TEXT NOT NULL,
    at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS consents_user ON consents(user_id);
CREATE TABLE IF NOT EXISTS product_media (
    sku_1c        TEXT PRIMARY KEY,
    images        TEXT NOT NULL,
    source        TEXT NOT NULL,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL,
    failures      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS product_attributes (
    sku_1c     TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_photos (
    path       TEXT PRIMARY KEY,
    sku_1c     TEXT NOT NULL,
    file_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path = "data/vdm.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- Корзина ------------------------------------------------------------

    def load_cart(self, user_id: str) -> Cart:
        row = self._db.execute(
            "SELECT payload FROM carts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return Cart(user_id=user_id)
        items = [CartItem(**raw) for raw in json.loads(row["payload"])]
        return Cart(user_id=user_id, items=items)

    def save_cart(self, cart: Cart) -> None:
        if cart.is_empty:
            self._db.execute("DELETE FROM carts WHERE user_id = ?", (cart.user_id,))
        else:
            self._db.execute(
                "INSERT INTO carts(user_id, payload, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (cart.user_id, json.dumps([asdict(i) for i in cart.items], ensure_ascii=False), _now()),
            )
        self._db.commit()

    # --- Заказы -------------------------------------------------------------

    def save_order(self, order: Order) -> None:
        """Заказ фиксируется до отправки: если Google Sheets недоступен, он не теряется."""
        self._db.execute(
            "INSERT INTO orders(id, user_id, channel, payload, status, attempts, last_error, "
            "created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, status = excluded.status, "
            "attempts = excluded.attempts, last_error = excluded.last_error",
            (
                order.id,
                order.user_id,
                order.channel,
                json.dumps(asdict(order), ensure_ascii=False),
                order.status,
                order.delivery_attempts,
                order.last_error,
                order.created_at,
            ),
        )
        self._db.commit()

    def pending_orders(self, limit: int = 50) -> list[Order]:
        rows = self._db.execute(
            "SELECT payload FROM orders WHERE status != 'sent' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [_order_from_payload(row["payload"]) for row in rows]

    def orders_of(self, user_id: str) -> list[Order]:
        rows = self._db.execute(
            "SELECT payload FROM orders WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [_order_from_payload(row["payload"]) for row in rows]

    # --- Согласия (ФЗ-152) ---------------------------------------------------

    def record_consent(self, user_id: str, channel: str, text_version: str, action: str) -> str:
        consent_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO consents(id, user_id, channel, text_version, action, at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (consent_id, user_id, channel, text_version, action, _now()),
        )
        self._db.commit()
        return consent_id

    def active_consent(self, user_id: str) -> str | None:
        """Идентификатор действующего согласия либо None, если его нет или оно отозвано."""
        row = self._db.execute(
            "SELECT id, action FROM consents WHERE user_id = ? ORDER BY at DESC, rowid DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None or row["action"] != "granted":
            return None
        return row["id"]

    def consent_history(self, user_id: str) -> list[dict[str, str]]:
        rows = self._db.execute(
            "SELECT text_version, action, at FROM consents WHERE user_id = ? ORDER BY at",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Кэш изображений товаров ---------------------------------------------

    def load_media(self, sku_1c: str) -> dict | None:
        row = self._db.execute(
            "SELECT images, source, etag, last_modified, fetched_at, failures "
            "FROM product_media WHERE sku_1c = ?",
            (sku_1c,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["images"] = json.loads(data["images"])
        return data

    def save_media(
        self,
        sku_1c: str,
        images: list[str],
        source: str,
        etag: str | None = None,
        last_modified: str | None = None,
        failures: int = 0,
    ) -> None:
        """Запоминаем и удачи, и неудачи.

        Пустой список с ненулевым счётчиком неудач — это «у товара фото нет либо
        не отдалось»: без такой записи бот ходил бы на сайт при каждом показе
        одной и той же карточки.
        """
        self._db.execute(
            "INSERT INTO product_media(sku_1c, images, source, etag, last_modified, "
            "fetched_at, failures) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sku_1c) DO UPDATE SET images = excluded.images, "
            "source = excluded.source, etag = excluded.etag, "
            "last_modified = excluded.last_modified, fetched_at = excluded.fetched_at, "
            "failures = excluded.failures",
            (
                sku_1c,
                json.dumps(images, ensure_ascii=False),
                source,
                etag,
                last_modified,
                _now(),
                failures,
            ),
        )
        self._db.commit()

    def all_media(self) -> dict[str, list[str]]:
        """Все накопленные фотографии разом — для переливки в базу знаний.

        Пять с половиной тысяч отдельных запросов ради одной перезаписи файла
        не нужны, а весь кэш занимает единицы мегабайт.
        """
        rows = self._db.execute(
            "SELECT sku_1c, images FROM product_media WHERE images != '[]'"
        ).fetchall()
        return {row["sku_1c"]: json.loads(row["images"]) for row in rows}

    # --- Характеристики со страницы товара -----------------------------------

    def save_attributes(self, sku_1c: str, attributes: dict[str, str]) -> None:
        if not attributes:
            return
        self._db.execute(
            "INSERT INTO product_attributes(sku_1c, payload, fetched_at) VALUES(?, ?, ?) "
            "ON CONFLICT(sku_1c) DO UPDATE SET payload = excluded.payload, "
            "fetched_at = excluded.fetched_at",
            (sku_1c, json.dumps(attributes, ensure_ascii=False), _now()),
        )
        self._db.commit()

    def has_attributes(self, sku_1c: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM product_attributes WHERE sku_1c = ?", (sku_1c,)
        ).fetchone()
        return row is not None

    def all_attributes(self) -> dict[str, dict[str, str]]:
        rows = self._db.execute("SELECT sku_1c, payload FROM product_attributes").fetchall()
        return {row["sku_1c"]: json.loads(row["payload"]) for row in rows}

    # --- Снимки, уже загруженные в Telegram ----------------------------------

    def telegram_photo(self, path: str) -> str | None:
        """Идентификатор файла на серверах Telegram.

        Один раз отправленный снимок больше не нужно ни качать с диска, ни
        загружать заново: повторный показ уходит одним коротким запросом.
        """
        row = self._db.execute(
            "SELECT file_id FROM telegram_photos WHERE path = ?", (path,)
        ).fetchone()
        return row["file_id"] if row else None

    def save_telegram_photo(self, path: str, sku_1c: str, file_id: str) -> None:
        self._db.execute(
            "INSERT INTO telegram_photos(path, sku_1c, file_id, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET file_id = excluded.file_id",
            (path, sku_1c, file_id, _now()),
        )
        self._db.commit()

    def media_stats(self) -> dict[str, int]:
        row = self._db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN images != '[]' THEN 1 ELSE 0 END) AS with_images, "
            "SUM(CASE WHEN failures > 0 THEN 1 ELSE 0 END) AS failed "
            "FROM product_media"
        ).fetchone()
        return {key: row[key] or 0 for key in ("total", "with_images", "failed")}

    # --- Права субъекта ПДн --------------------------------------------------

    def export_user_data(self, user_id: str) -> dict[str, object]:
        return {
            "user_id": user_id,
            "cart": [asdict(item) for item in self.load_cart(user_id).items],
            "orders": [asdict(order) for order in self.orders_of(user_id)],
            "consents": self.consent_history(user_id),
            "exported_at": _now(),
        }

    def delete_user_data(self, user_id: str, channel: str) -> None:
        """Удаление по требованию субъекта.

        Заказы обезличиваются, а не стираются: строки уже переданы менеджеру и нужны
        для учёта. Контакты при этом удаляются полностью. Сам факт согласия и его
        отзыва остаётся в журнале — иначе нечем подтвердить законность обработки.
        """
        for order in self.orders_of(user_id):
            order.customer = Customer()
            order.user_id = "deleted"
            self._db.execute(
                "UPDATE orders SET user_id = 'deleted', payload = ? WHERE id = ?",
                (json.dumps(asdict(order), ensure_ascii=False), order.id),
            )
        self._db.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
        self._db.commit()
        self.record_consent(user_id, channel, "n/a", "revoked")


def _order_from_payload(payload: str) -> Order:
    raw = json.loads(payload)
    return Order(
        id=raw["id"],
        user_id=raw["user_id"],
        channel=raw["channel"],
        items=[CartItem(**item) for item in raw["items"]],
        customer=Customer(**raw["customer"]),
        created_at=raw["created_at"],
        consent_id=raw.get("consent_id"),
        status=raw.get("status", "new"),
        delivery_attempts=raw.get("delivery_attempts", 0),
        last_error=raw.get("last_error"),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
