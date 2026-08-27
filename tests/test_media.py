"""Проверка сбора фотографий на сохранённых страницах сайта — без сети.

Фикстуры лежат в `tests/fixtures/` и в репозиторий не попадают: это страницы
заказчика с названиями и ценами. Если их нет, тесты разбора пропускаются,
а логика кэша и сдержанности проверяется на подставных страницах.
"""

from __future__ import annotations

import pathlib

import pytest

from catalog.models import Product
from core.storage import Storage
from media.extract import ProductMedia, decode, extract_from_card, extract_from_listing
from media.fetcher import FetchError, FetchResult, PageFetcher, _ascii
from media.service import MAX_FAILURES, MediaService

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CARD_FIXTURE = FIXTURES / "product_card.html"
BASE = "https://vdm.ru/catalog/x/tovar.html"


def product(sku="S1", url=BASE, bitrix_id=None):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": "Фрезерный станок",
            "price": 253000,
            "currency": "RUB",
            "in_stock": 1,
            "category_paths": [],
            "description": "",
            "kit_contents": [],
            "norms": [],
            "bitrix_id": bitrix_id,
            "url": url,
            "short_url": None,
        }
    )


class StubFetcher:
    """Подставная загрузка: отдаёт заранее заданный ответ и считает обращения."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def get(self, url, etag=None, last_modified=None):  # noqa: ANN001
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


# --- Разбор страниц ------------------------------------------------------------


def test_encoding_is_detected_by_content():
    assert decode("Мяч".encode("cp1251")) == "Мяч"
    assert decode("Мяч".encode()) == "Мяч"


def test_broken_bytes_do_not_crash():
    assert decode(b"\xff\xfe\x00bad") != ""


def test_og_image_is_taken_as_main():
    page = '<meta property="og:image" content="https://vdm.ru/upload/x/1200_1200_a/1.jpg" />'
    media = extract_from_card(page, BASE)
    assert media.main == "https://vdm.ru/upload/x/1200_1200_a/1.jpg"
    assert media.source == "card"


def test_attributes_in_reverse_order_still_work():
    page = '<meta content="/upload/x/1200_1200_a/1.jpg" property="og:image">'
    assert extract_from_card(page, BASE).main == "https://vdm.ru/upload/x/1200_1200_a/1.jpg"


def test_gallery_adds_other_photos_without_duplicates():
    page = (
        '<meta property="og:image" content="https://vdm.ru/a/1200_1200_h/1.jpg" />'
        '<a data-large-picture="/a/1200_1200_h/1.jpg"></a>'
        '<a data-large-picture="/b/1200_1200_h/2.jpg"></a>'
    )
    media = extract_from_card(page, BASE)
    assert media.images == [
        "https://vdm.ru/a/1200_1200_h/1.jpg",
        "https://vdm.ru/b/1200_1200_h/2.jpg",
    ]


def test_absolute_and_relative_paths_to_one_photo_are_one_entry():
    """Регрессия: og:image абсолютный, галерея относительная — снимок дублировался."""
    page = (
        '<meta property="og:image" content="https://vdm.ru/upload/a/1200_1200_h/1.jpg" />'
        '<a data-large-picture="/upload/a/1200_1200_h/1.jpg"></a>'
    )
    assert extract_from_card(page, "https://other.example/catalog/x.html").images == [
        "https://vdm.ru/upload/a/1200_1200_h/1.jpg"
    ]


def test_thumbnails_and_interface_icons_are_skipped():
    page = (
        '<a data-large-picture="/upload/x/50_50_1/1.jpg"></a>'
        '<img itemprop="image" src="/upload/form/146/phone.png">'
        '<a data-large-picture="/upload/x/1200_1200_h/2.jpg"></a>'
    )
    assert extract_from_card(page, BASE).images == ["https://vdm.ru/upload/x/1200_1200_h/2.jpg"]


def test_page_without_photos_gives_empty_result():
    media = extract_from_card("<html><body>Нет картинок</body></html>", BASE)
    assert media.is_empty and media.main is None and media.source == ""


def test_relative_paths_become_absolute():
    page = '<a data-large-picture="/upload/x/1200_1200_h/1.jpg"></a>'
    assert extract_from_card(page, BASE).main.startswith("https://vdm.ru/")


def test_listing_gives_bitrix_id_url_and_preview():
    page = (
        '<div class="item product sku" data-product-id="52649">'
        '<a href="/catalog/x/tovar.html" class="picture">'
        '<img src="/upload/resize_cache/iblock/df1/hash/220_200_1/1.jpg"></a></div>'
    )
    items = extract_from_listing(page, "https://vdm.ru/catalog/")
    assert len(items) == 1
    assert items[0].bitrix_id == 52649
    assert items[0].url == "https://vdm.ru/catalog/x/tovar.html"
    assert items[0].image.endswith("220_200_1/1.jpg")


def test_listing_does_not_mix_neighbouring_tiles():
    """Регрессия: картинка соседней плитки не должна достаться чужому товару."""
    page = (
        '<div class="item product sku" data-product-id="1">'
        '<a href="/a.html" class="picture"><img src="/upload/a/220_200_1/1.jpg"></a></div>'
        '<div class="item product sku" data-product-id="2">'
        '<a href="/b.html" class="picture"><img src="/upload/b/220_200_1/1.jpg"></a></div>'
    )
    items = extract_from_listing(page, "https://vdm.ru/")
    assert [i.bitrix_id for i in items] == [1, 2]
    assert items[0].image.endswith("/a/220_200_1/1.jpg")
    assert items[1].image.endswith("/b/220_200_1/1.jpg")


# --- На настоящей странице заказчика --------------------------------------------


@pytest.mark.skipif(not CARD_FIXTURE.exists(), reason="страница-фикстура недоступна")
def test_real_card_yields_full_size_photos():
    media = extract_from_card(decode(CARD_FIXTURE.read_bytes()), BASE)
    assert len(media.images) >= 2
    assert all(url.startswith("https://vdm.ru/upload/") for url in media.images)
    assert "1200_1200" in media.main
    assert not any("50_50" in url for url in media.images)


# --- Сдержанность при загрузке ---------------------------------------------------


def test_user_agent_is_ascii_only():
    """Регрессия: кириллица в User-Agent роняла запрос до отправки."""
    assert _ascii("VdmBot (помощник)") == "VdmBot ()"
    assert _ascii("помощник")  # пустой результат недопустим


def test_throttle_keeps_the_interval():
    import time

    fetcher = PageFetcher(min_interval=0.05, respect_robots=False)
    started = time.monotonic()
    fetcher._throttle()
    fetcher._throttle()
    assert time.monotonic() - started >= 0.05


def test_robots_disallow_blocks_the_request(monkeypatch):
    fetcher = PageFetcher(respect_robots=True)
    monkeypatch.setattr(fetcher, "allowed", lambda url: False)
    with pytest.raises(FetchError, match="robots"):
        fetcher.get(BASE)


def test_unreadable_robots_does_not_block(monkeypatch):
    fetcher = PageFetcher()
    monkeypatch.setattr(fetcher, "_robots_for", lambda url: None)
    assert fetcher.allowed(BASE) is True


# --- Кэш и поведение сервиса -------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "t.sqlite3")


def test_photo_is_fetched_once_and_then_cached(storage):
    page = b'<meta property="og:image" content="https://vdm.ru/a/1200_1200_h/1.jpg" />'
    fetcher = StubFetcher(FetchResult(body=page, status=200))
    service = MediaService(storage, fetcher, enabled=True)

    first = service.images_for(product())
    second = service.images_for(product())

    assert first == second == ["https://vdm.ru/a/1200_1200_h/1.jpg"]
    assert fetcher.calls == 1, "второй показ карточки не должен идти на сайт"


def test_disabled_service_never_touches_the_site(storage):
    fetcher = StubFetcher(FetchResult(body=b"", status=200))
    assert MediaService(storage, fetcher, enabled=False).images_for(product()) == []
    assert fetcher.calls == 0


def test_product_without_url_is_skipped(storage):
    fetcher = StubFetcher(FetchResult(body=b"", status=200))
    service = MediaService(storage, fetcher, enabled=True)
    assert service.images_for(product(url=None)) == []
    assert fetcher.calls == 0


def test_failures_stop_repeated_attempts(storage):
    """Регрессия: без счётчика неудач бот ходил на сайт при каждом показе."""
    fetcher = StubFetcher(error=FetchError("сайт недоступен"))
    service = MediaService(storage, fetcher, enabled=True)

    for _ in range(MAX_FAILURES + 3):
        assert service.images_for(product()) == []

    assert fetcher.calls == MAX_FAILURES


def test_not_modified_keeps_cached_photos(storage):
    storage.save_media("S1", ["https://vdm.ru/a.jpg"], source="card", etag='"v1"')
    fetcher = StubFetcher(FetchResult(body=None, status=304))
    service = MediaService(storage, fetcher, enabled=True)
    # Кэш пуст только по свежести — заставляем сходить, сбросив картинки.
    storage.save_media("S1", [], source="card", etag='"v1"')

    assert service.images_for(product()) == []
    assert fetcher.calls == 1


def test_listing_warm_up_matches_by_bitrix_id(storage):
    page = (
        b'<div class="item product sku" data-product-id="52649">'
        b'<a href="/a.html" class="picture"><img src="/upload/a/220_200_1/1.jpg"></a></div>'
    )
    fetcher = StubFetcher(FetchResult(body=page, status=200))
    service = MediaService(storage, fetcher, enabled=True)

    saved = service.warm_up_from_listing(
        "https://vdm.ru/catalog/", [product(bitrix_id=52649), product("S2", bitrix_id=1)]
    )

    assert saved == 1
    assert storage.load_media("S1")["source"] == "listing"


def test_warm_up_does_not_overwrite_better_photos(storage):
    storage.save_media("S1", ["https://vdm.ru/big/1200_1200_h/1.jpg"], source="card")
    page = (
        b'<div class="item product sku" data-product-id="52649">'
        b'<a href="/a.html" class="picture"><img src="/upload/a/220_200_1/1.jpg"></a></div>'
    )
    service = MediaService(storage, StubFetcher(FetchResult(body=page, status=200)), enabled=True)

    service.warm_up_from_listing("https://vdm.ru/catalog/", [product(bitrix_id=52649)])

    assert storage.load_media("S1")["images"] == ["https://vdm.ru/big/1200_1200_h/1.jpg"]


def test_main_image_returns_none_without_photos(storage):
    page = "<html>пусто</html>".encode()
    service = MediaService(storage, StubFetcher(FetchResult(body=page, status=200)), enabled=True)
    assert service.main_image(product()) is None


def test_media_stats_counts_hits_and_misses(storage):
    storage.save_media("S1", ["https://vdm.ru/a.jpg"], source="card")
    storage.save_media("S2", [], source="", failures=2)
    assert storage.media_stats() == {"total": 2, "with_images": 1, "failed": 1}


def test_empty_media_object():
    assert ProductMedia().is_empty and ProductMedia().main is None


# --- Переливка в базу знаний ------------------------------------------------------


def _kb(tmp_path, rows):
    import json

    path = tmp_path / "products.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def test_collected_photos_land_in_the_knowledge_base(tmp_path, storage):
    import json

    from media.sync import sync_to_kb

    path = _kb(tmp_path, [{"sku_1c": "S1", "name": "Станок", "price": 253000, "images": []}])
    storage.save_media("S1", ["https://vdm.ru/a/1200_1200_h/1.jpg"], source="card")

    report = sync_to_kb(storage, path)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["images"] == ["https://vdm.ru/a/1200_1200_h/1.jpg"]
    assert saved["price"] == 253000, "остальные поля карточки не трогаем"
    assert (report.products, report.with_images, report.updated) == (1, 1, 1)


def test_sync_keeps_products_without_photos(tmp_path, storage):
    from media.sync import sync_to_kb

    path = _kb(tmp_path, [{"sku_1c": "S1", "images": []}, {"sku_1c": "S2", "images": []}])
    storage.save_media("S2", ["https://vdm.ru/b.jpg"], source="card")

    report = sync_to_kb(storage, path)

    assert report.products == 2 and report.with_images == 1
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_rebuild_of_knowledge_base_keeps_photos(tmp_path):
    """Регрессия: новая выгрузка 1С обнуляла всё, что собрано с сайта."""
    from ingest.build_kb import Product as KbProduct
    from ingest.build_kb import _carry_over_images

    path = _kb(tmp_path, [{"sku_1c": "S1", "images": ["https://vdm.ru/a.jpg"]}])
    fresh = KbProduct(
        sku_1c="S1",
        name="Станок",
        url=None,
        short_url=None,
        price=1,
        currency="RUB",
        in_stock=0,
        category_paths=[],
        description="",
        kit_contents=[],
        norms=[],
        bitrix_id=None,
    )

    _carry_over_images([fresh], path)

    assert fresh.images == ["https://vdm.ru/a.jpg"]


def test_listing_preview_is_upgraded_to_the_full_size_photo(storage):
    """Превью 220×200 из списка заменяется снимком 1200×1200 со страницы товара."""
    storage.save_media("S1", ["https://vdm.ru/a/220_200_1/1.jpg"], source="listing")
    page = b'<meta property="og:image" content="https://vdm.ru/a/1200_1200_h/1.jpg" />'
    fetcher = StubFetcher(FetchResult(body=page, status=200))
    service = MediaService(storage, fetcher, enabled=True)

    assert service.images_for(product()) == ["https://vdm.ru/a/1200_1200_h/1.jpg"]
    assert storage.load_media("S1")["source"] == "card"

    service.images_for(product())
    assert fetcher.calls == 1, "после замены на сайт больше не ходим"
