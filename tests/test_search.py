import pytest

from catalog.models import Product
from catalog.search import CatalogIndex, SearchQuery, apply_text_filters

NORM_838 = {
    "doc_id": "order_838",
    "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
    "source": "heading",
    "confidence": 0.9,
}


def product(sku, name, **kw):
    raw = {
        "sku_1c": sku,
        "name": name,
        "url": kw.get("url"),
        "short_url": None,
        "price": kw.get("price", 1000),
        "currency": "RUB",
        "in_stock": kw.get("in_stock", 0),
        "category_paths": kw.get("category_paths", [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]]),
        "description": kw.get("description", ""),
        "kit_contents": kw.get("kit_contents", []),
        "norms": kw.get("norms", []),
        "bitrix_id": None,
    }
    return Product.from_dict(raw)


def norm(code, title=None):
    return {**NORM_838, "item_code": code, "item_title": title}


@pytest.fixture
def index():
    return CatalogIndex(
        [
            product("A1", "Мяч резиновый d=150mm", price=333, in_stock=20, norms=[norm("4.4.5")]),
            product("A2", "Мяч фитбол диаметр 65 см", price=970, in_stock=0),
            product(
                "A3",
                "Фрезерно-гравировальный станок с ЧПУ",
                price=253000,
                in_stock=1,
                norms=[norm("2.20.63", "Фрезерно-гравировальный станок")],
            ),
            product(
                "A4",
                "Речевая игра «Составь сообщение»",
                price=6111,
                in_stock=5,
                norms=[norm("2.1.12"), norm("2.1.14")],
            ),
            product(
                "A5",
                "Интерактивная панель без стойки",
                price=384426,
                description="Познавательное развитие. Интерактивное оборудование.",
            ),
        ]
    )


def test_exact_norm_code_wins(index):
    hits = index.search(SearchQuery(text="2.20.63"))
    assert [hit.product.sku_1c for hit in hits] == ["A3"]
    assert hits[0].by_norm


def test_citation_names_the_requested_code(index):
    """Регрессия: у товара несколько пунктов, назвать надо тот, о котором спросили."""
    hit = index.search(SearchQuery(text="п. 2.1.14"))[0]
    assert hit.matched_code == "2.1.14"
    assert hit.citation().startswith("позиция 2.1.14")


def test_code_only_query_does_not_pull_text_matches(index):
    """Регрессия: цифры из «2.1.14» совпадали с обрывками чужих артикулов."""
    hits = index.search(SearchQuery(text="2.1.14"))
    assert [hit.product.sku_1c for hit in hits] == ["A4"]


def test_prefix_finds_whole_subsection_once(index):
    hits = index.search(SearchQuery(text="2.1"))
    assert [hit.product.sku_1c for hit in hits] == ["A4"]


def test_unknown_code_falls_back_to_text(index):
    assert index.search(SearchQuery(text="9.9.9")) == []


def test_text_search_ranks_by_name(index):
    hits = index.search(SearchQuery(text="фрезерный станок"))
    assert hits[0].product.sku_1c == "A3"


def test_in_stock_hint_becomes_filter(index):
    hits = index.search(SearchQuery(text="мяч в наличии"))
    assert {hit.product.sku_1c for hit in hits} == {"A1"}


def test_price_ceiling_from_text(index):
    hits = index.search(SearchQuery(text="мяч до 500 руб"))
    assert {hit.product.sku_1c for hit in hits} == {"A1"}


def test_thousands_shorthand():
    query = apply_text_filters(SearchQuery(text="панели от 300 тыс руб"))
    assert (query.price_min, query.text) == (300000, "панели")


def test_stock_phrase_removed_from_text():
    """Регрессия: от «в наличии» оставался хвост «и», попадавший в поиск."""
    assert apply_text_filters(SearchQuery(text="мячи в наличии")).text == "мячи"


def test_explicit_filters_are_respected(index):
    hits = index.search(SearchQuery(text="мяч", price_max=500))
    assert {hit.product.sku_1c for hit in hits} == {"A1"}


def test_lookup_by_sku(index):
    assert index.get("A3").name.startswith("Фрезерно")
    assert index.get("нет такого") is None


def test_morphology_matches_plural_and_singular(index):
    assert index.search(SearchQuery(text="мячи"))
    assert index.search(SearchQuery(text="станки"))
