"""Извлечение изображений товара из страниц сайта.

Это не парсер каталога: цены, названия и остатки берутся из выгрузки 1С. Здесь
добирается только то, чего в выгрузке нет, — фотографии.

Разбор идёт по устойчивым якорям, а не по вёрстке:

* карточка товара — `<meta property="og:image">` (абсолютный адрес, 1200×1200)
  и галерея `a.zoom[data-large-picture]` внутри `#pictureContainer`;
* плитка листинга — `div.item.product.sku[data-product-id]` с превью 220×200.

Оба якоря — это данные компонента Битрикса, а не оформление, поэтому смена темы
или перестановка блоков их не ломает. Если якоря всё же исчезнут, извлечение
вернёт пустой результат, а не мусор: молча подставить чужую картинку хуже, чем
показать карточку без фото.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urlparse

# Битрикс складывает превью в resize_cache/<хеш>/<ШИРИНА_ВЫСОТА_хеш>/файл.
# Размер нужен, чтобы отличить полноразмерное фото от иконки 50×50.
_SIZE_IN_PATH = re.compile(r"/(\d{2,4})_(\d{2,4})_[^/]*/", re.I)

_OG_IMAGE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I
)
_OG_IMAGE_REVERSED = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I
)
_ITEMPROP_IMAGE = re.compile(
    r'<[^>]+itemprop=["\']image["\'][^>]*(?:href|src|content)=["\']([^"\']+)["\']', re.I
)
_LARGE_PICTURE = re.compile(r'data-large-picture=["\']([^"\']+)["\']', re.I)

# Характеристики в карточке лежат парами имя-значение в блоке propertyList.
# Их нет в выгрузке 1С, а в закупке для сада или школы страну и сертификат
# спрашивают всерьёз — поэтому забираем их тем же проходом, что и фотографии.
_PROPERTY_LIST = re.compile(r'<div[^>]+class=["\'][^"\']*propertyList[^"\']*["\'][^>]*>', re.I)
_PROPERTY_PAIR = re.compile(
    r'propertyName["\'][^>]*>(.*?)</div>.*?propertyValue["\'][^>]*>(.*?)</div>',
    re.I | re.S,
)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
MAX_ATTRIBUTES = 20

# На сайте в названиях характеристик попадаются латинские буквы вместо кириллицы
# («Cтрана» с латинской C). Глазом не отличить, а обращение по ключу ломается.
_LOOKALIKE = str.maketrans("ACEOPTHKMBXY", "АСЕОРТНКМВХУ")

_TILE = re.compile(r'<div class="item product sku"[^>]*>', re.I)
_TILE_PRODUCT_ID = re.compile(r'data-product-id=["\'](\d+)["\']', re.I)
_TILE_LINK = re.compile(r'<a href=["\']([^"\']+)["\'][^>]*class=["\']picture["\']', re.I)
_TILE_IMG = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)

# Иконки интерфейса, флаги и картинки форм фотографиями товара не являются.
_NOT_A_PRODUCT_PHOTO = re.compile(r"/upload/form/|/images/|/icons?/|\.svg$", re.I)
MIN_SIDE = 200


@dataclass
class ProductMedia:
    """Изображения одного товара. `main` — то, что показываем в карточке."""

    images: list[str] = field(default_factory=list)
    source: str = ""  # card | listing

    @property
    def main(self) -> str | None:
        return self.images[0] if self.images else None

    @property
    def is_empty(self) -> bool:
        return not self.images


@dataclass(frozen=True)
class ListingItem:
    """Товар, найденный на странице списка."""

    bitrix_id: int
    url: str | None
    image: str | None


def decode(raw: bytes) -> str:
    """Текст страницы по содержимому, а не по заголовку.

    Заголовок `charset` на сайте встречается неверный, поэтому кодировку
    подбираем: сначала UTF-8, затем cp1251 — других на 1С-Битрикс не бывает.
    """
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_from_card(page: str, base_url: str) -> ProductMedia:
    """Фотографии со страницы товара: сначала главная, затем остальные из галереи."""
    images: list[str] = []

    for pattern in (_OG_IMAGE, _OG_IMAGE_REVERSED):
        match = pattern.search(page)
        if match:
            _append(images, match.group(1), base_url)
            break

    # Галерея даёт полноразмерные снимки, включая тот же первый кадр — дубли
    # отсекаются при добавлении.
    for path in _LARGE_PICTURE.findall(page):
        _append(images, path, base_url)

    if not images:
        for path in _ITEMPROP_IMAGE.findall(page):
            _append(images, path, base_url)

    return ProductMedia(images=images, source="card" if images else "")


def extract_attributes(page: str) -> dict[str, str]:
    """Характеристики со страницы товара: страна, сертификат, код.

    Порядок сохраняем такой же, как на сайте: карточка в боте должна читаться
    так же, как страница, с которой её собрали.
    """
    match = _PROPERTY_LIST.search(page)
    if not match:
        return {}

    attributes: dict[str, str] = {}
    for raw_name, raw_value in _PROPERTY_PAIR.findall(page[match.end() :]):
        name = _plain(raw_name).translate(_LOOKALIKE)
        value = _plain(raw_value)
        if not name or not value or name in attributes:
            continue
        attributes[name] = value
        if len(attributes) >= MAX_ATTRIBUTES:
            break
    return attributes


def extract_from_listing(page: str, base_url: str) -> list[ListingItem]:
    """Товары со страницы списка: идентификатор Битрикса, ссылка и превью.

    Одна такая страница закрывает до трёх десятков товаров сразу — это дешевле,
    чем открывать карточку каждого.
    """
    items: list[ListingItem] = []
    seen: set[int] = set()

    for start, end in _tile_bounds(page):
        chunk = page[start:end]
        product_id = _TILE_PRODUCT_ID.search(chunk)
        if not product_id:
            continue
        bitrix_id = int(product_id.group(1))
        if bitrix_id in seen:
            continue
        seen.add(bitrix_id)

        link = _TILE_LINK.search(chunk)
        image = next(
            (
                _absolute(src, base_url)
                for src in _TILE_IMG.findall(chunk)
                if _looks_like_photo(src, min_side=0)
            ),
            None,
        )
        items.append(
            ListingItem(
                bitrix_id=bitrix_id,
                url=_absolute(link.group(1), base_url) if link else None,
                image=image,
            )
        )
    return items


# --- Внутреннее ---------------------------------------------------------------


def _tile_bounds(page: str) -> list[tuple[int, int]]:
    """Границы плиток: от начала одной до начала следующей."""
    starts = [m.start() for m in _TILE.finditer(page)]
    return [(s, starts[i + 1] if i + 1 < len(starts) else len(page)) for i, s in enumerate(starts)]


def _append(images: list[str], path: str, base_url: str) -> None:
    if not _looks_like_photo(path):
        return
    url = _absolute(path, base_url)
    if not url:
        return
    # Дубли ищем по пути, а не по полному адресу: `og:image` абсолютный, а галерея
    # относительная, и один и тот же снимок иначе попадает в список дважды.
    if any(urlparse(url).path == urlparse(known).path for known in images):
        return
    images.append(url)


def _plain(html: str) -> str:
    return _SPACES.sub(" ", unescape(_TAGS.sub(" ", html))).strip(" : ")


def _absolute(path: str, base_url: str) -> str | None:
    path = unescape(path.strip())
    if not path or path.startswith(("data:", "javascript:", "#")):
        return None
    return urljoin(base_url, path)


def _looks_like_photo(path: str, min_side: int = MIN_SIDE) -> bool:
    if _NOT_A_PRODUCT_PHOTO.search(path):
        return False
    size = _SIZE_IN_PATH.search(path)
    if size and min_side:
        # Отсекаем миниатюры вроде 50×50: в карточке от них нет пользы.
        return max(int(size.group(1)), int(size.group(2))) >= min_side
    return True
