"""Файлы фотографий на диске.

Telegram отказался показывать снимки товаров: на попытку отправить их по адресу
он отвечает «failed to get HTTP URL content» — его серверы до vdm.ru не достают.
Значит, байты должны приносить мы сами и отдавать их файлом.

Побочная выгода важнее исходной причины: карточка перестаёт зависеть от того,
жив ли сайт в момент показа. Один проход по каталогу — и бот работает даже когда
vdm.ru недоступен, а именно так бывает чаще, чем хотелось бы.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from media.fetcher import FetchError, PageFetcher

log = logging.getLogger(__name__)

DEFAULT_DIR = Path("data/media")

# Снимок 1200×1200 весит сотни килобайт. Всё, что заметно больше, — это уже не
# фотография товара, и класть такое в карточку незачем.
MAX_BYTES = 8_000_000
# Telegram не примет файл тяжелее 10 МБ по адресу и 50 МБ файлом; наш потолок ниже.

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Сколько снимков храним на товар. Первый идёт в карточку, остальные — в галерею.
MAX_PER_PRODUCT = 3


@dataclass
class PhotoStore:
    """Скачивание и хранение снимков. Имя файла — код 1С, чтобы найти без базы."""

    fetcher: PageFetcher
    root: Path = DEFAULT_DIR
    downloaded: int = field(default=0, init=False)
    failed: int = field(default=0, init=False)

    def existing(self, sku_1c: str) -> list[Path]:
        """Уже скачанные снимки товара, в порядке номеров."""
        folder = self.root / _safe(sku_1c)
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix != ".part")

    def main(self, sku_1c: str) -> Path | None:
        files = self.existing(sku_1c)
        return files[0] if files else None

    def download(self, sku_1c: str, urls: list[str], limit: int = MAX_PER_PRODUCT) -> list[Path]:
        """Скачивает недостающие снимки товара и возвращает все, что есть на диске.

        Повторный вызов ничего не качает заново: файл на месте — значит, готово.
        Так проход по каталогу продолжается с места остановки, а не с начала.
        """
        saved = self.existing(sku_1c)
        if len(saved) >= min(limit, len(urls)):
            return saved

        folder = self.root / _safe(sku_1c)
        for number, url in enumerate(urls[:limit], 1):
            if any(p.stem == str(number) for p in saved):
                continue
            path = self._fetch_one(folder, number, url)
            if path is not None:
                saved.append(path)
        return sorted(saved)

    # --- Внутреннее -----------------------------------------------------------

    def _fetch_one(self, folder: Path, number: int, url: str) -> Path | None:
        try:
            result = self.fetcher.get(url)
        except FetchError as exc:
            self.failed += 1
            log.info("Снимок %s не скачан: %s", url, exc)
            return None

        if not result.body:
            self.failed += 1
            return None
        if len(result.body) > MAX_BYTES:
            self.failed += 1
            log.info("Снимок %s пропущен: %s байт", url, len(result.body))
            return None

        suffix = _suffix(result.content_type, url)
        if suffix is None:
            # Вместо картинки пришла страница — обычно это заглушка «404» с кодом
            # 200. Сохранять её под видом фотографии хуже, чем не сохранять ничего.
            self.failed += 1
            log.info("Снимок %s пропущен: тип %r", url, result.content_type)
            return None

        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{number}{suffix}"
        # Пишем через временный файл: прерванный на середине проход не должен
        # оставить обрезанный снимок, который потом сочтут скачанным.
        partial = path.with_suffix(path.suffix + ".part")
        partial.write_bytes(result.body)
        os.replace(partial, path)
        self.downloaded += 1
        return path

    def stats(self) -> dict[str, int]:
        return {"скачано": self.downloaded, "не вышло": self.failed}


def local_paths(root: Path | str = DEFAULT_DIR) -> dict[str, list[str]]:
    """Что уже лежит на диске: код 1С → пути к файлам. Для сборки базы знаний."""
    root = Path(root)
    if not root.is_dir():
        return {}
    found: dict[str, list[str]] = {}
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix != ".part")
        if files:
            found[folder.name] = [p.as_posix() for p in files]
    return found


def _suffix(content_type: str, url: str) -> str | None:
    for mime, extension in EXTENSIONS.items():
        if mime in content_type:
            return extension
    if not content_type:
        # Заголовка нет — доверяем расширению в адресе, но только известному.
        guessed = Path(urlparse(url).path).suffix.lower()
        return guessed if guessed in set(EXTENSIONS.values()) else None
    return None


def _safe(sku_1c: str) -> str:
    """Код 1С в имени папки: у заказчика встречаются коды вида «0Э-00005388»."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in sku_1c) or "unknown"
