"""Переливка собранных фотографий в базу знаний.

Кэш изображений живёт в SQLite, а карточку товара боту отдаёт `products.jsonl`.
Пока фотографии не переехали в базу знаний, карточка неполная: цена и артикул
есть, фото нет. Здесь этот разрыв и закрывается.

Формат базы знаний — JSON по строке на товар. Он же уходит в Cloud.ru: карточка
целиком, без обращений к SQLite.

Запись идёт во временный файл рядом и заканчивается подменой: если процесс
прервать посередине, база знаний останется прежней, а не обрежется на середине.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.storage import Storage

DEFAULT_KB = Path("data/kb/products.jsonl")


@dataclass
class SyncReport:
    products: int = 0
    with_images: int = 0
    updated: int = 0
    photos: int = 0

    def __str__(self) -> str:
        return (
            f"в базе знаний {self.products} товаров, с фото {self.with_images} "
            f"(добавлено {self.updated}, всего снимков {self.photos})"
        )


def sync_to_kb(storage: Storage, kb_path: str | Path = DEFAULT_KB) -> SyncReport:
    """Проставляет товарам в `products.jsonl` собранные фотографии."""
    kb_path = Path(kb_path)
    if not kb_path.exists():
        raise FileNotFoundError(f"База знаний не собрана: {kb_path}. Сначала `run.py ingest`.")

    media = storage.all_media()
    report = SyncReport()
    tmp = kb_path.with_suffix(kb_path.suffix + ".tmp")

    with kb_path.open(encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            product = json.loads(line)
            report.products += 1

            images = media.get(product["sku_1c"], [])
            if images and product.get("images") != images:
                product["images"] = images
                report.updated += 1
            if product.get("images"):
                report.with_images += 1
                report.photos += len(product["images"])

            dst.write(json.dumps(product, ensure_ascii=False) + "\n")

    tmp.replace(kb_path)
    return report


def images_in_kb(kb_path: str | Path = DEFAULT_KB) -> dict[str, list[str]]:
    """Фотографии из готовой базы знаний — чтобы не потерять их при пересборке.

    Выгрузка 1С приходит два-три раза в месяц, и каждая пересборка иначе обнуляла
    бы всё, что собрано с сайта.
    """
    kb_path = Path(kb_path)
    if not kb_path.exists():
        return {}

    found: dict[str, list[str]] = {}
    with kb_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            product = json.loads(line)
            if product.get("images"):
                found[product["sku_1c"]] = product["images"]
    return found
