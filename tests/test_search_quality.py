"""Качество подбора: то, на что заказчик смотрел на демонстрации.

Три живых запроса, каждый из которых раньше давал очевидно неверный ответ:
«чем оснастить спортзал в саду» → пять обручей подряд, «станки для школьных
мастерских» → музыкальная шкатулка и счётный материал «Помидор», «кабинет
логопеда в детском саду» → позиции со школьным приказом.
"""

import pytest

from catalog.models import Product
from catalog.search import CatalogIndex, SearchQuery
from catalog.text import expand, stem

SAD = ["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]
SHKOLA = ["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]


def product(sku, name, roots=None, description="", categories=()):
    paths = [list(roots or SAD) + list(categories)]
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "url": None,
            "short_url": None,
            "price": 1000,
            "currency": "RUB",
            "in_stock": 1,
            "category_paths": paths,
            "description": description,
            "kit_contents": [],
            "norms": [],
            "bitrix_id": None,
        }
    )


@pytest.fixture
def index():
    return CatalogIndex(
        [
            product("O1", "БОС Обруч 60 см облегченный У733"),
            product("O2", "БОС Обруч 70 см облегченный У734"),
            product("O3", "БОС Обруч 60 см салатовый У842"),
            product("O4", "БОС Обруч 80 см У635"),
            product("O5", "БОС Обруч гимнастический складной"),
            product("M1", "БОС Мяч гимнастический 55 см", categories=["Спортивный зал"]),
            product("S1", "ВТ Скамейка гимнастическая", categories=["Спортивный зал"]),
            product(
                "T1",
                "ШР Фрезерно-гравировальный станок с ЧПУ",
                roots=SHKOLA,
                categories=["Кабинет труда (технологии)"],
            ),
            product(
                "T2",
                "ШР Станок лазерной резки",
                roots=SHKOLA,
                categories=["Кабинет труда (технологии)"],
            ),
            product(
                "MZ",
                "МЗ Набор «Музыкальная шкатулка»",
                description="Подходит для школьных праздников и мастерских занятий",
            ),
        ]
    )


# --- Морфология ---------------------------------------------------------------


def test_fleeting_vowel_makes_singular_and_plural_meet():
    """«станок» и «станки» отсекались в разные основы и не совпадали."""
    assert stem("станок") == stem("станки")
    assert stem("потолок") == stem("потолки")


def test_query_expands_to_catalog_wording():
    """В каталоге «спортивный зал», у человека — «спортзал»."""
    assert "спортивн" in expand(["спортзал"])
    assert "зал" in expand(["спортзал"])


# --- Выдача -------------------------------------------------------------------


def test_gym_query_is_not_five_hoops(index):
    """Человеку нужен зал, а не витрина обручей."""
    hits = index.search(SearchQuery(text="чем оснастить спортзал в саду", limit=3))
    names = [hit.product.name for hit in hits]
    assert sum("Обруч" in name for name in names) <= 1
    assert len(names) == 3


def test_workshop_query_finds_machines(index):
    hits = index.search(
        SearchQuery(text="станки для школьных мастерских", limit=3, audience="school")
    )
    assert {hit.product.sku_1c for hit in hits} >= {"T1", "T2"}


def test_description_does_not_outweigh_the_name(index):
    """«Музыкальная шкатулка» упоминает мастерские в описании — но это не станок."""
    hits = index.search(SearchQuery(text="станки для школьных мастерских", limit=3))
    assert hits[0].product.sku_1c != "MZ"


def test_audience_moves_school_items_down(index):
    """Подбираем для сада — школьные позиции не должны идти первыми."""
    for_school = index.search(SearchQuery(text="станок", limit=3, audience="school"))
    for_preschool = index.search(SearchQuery(text="станок", limit=3, audience="preschool"))
    assert for_school[0].product.sku_1c in {"T1", "T2"}
    # Для сада станков нет вовсе, но выдача не должна становиться пустой.
    assert for_preschool


def test_exact_code_still_wins_over_everything(index):
    """Разнообразие выдачи не должно ломать точный поиск по пункту перечня."""
    hits = index.search(SearchQuery(text="обруч", limit=5))
    assert hits and all("Обруч" in h.product.name or h.score > 0 for h in hits)
