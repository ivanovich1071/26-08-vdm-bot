"""Кому подбираем — и каким перечнем это обосновывать.

Главная жалоба заказчика: на вопрос про кабинет логопеда в детском саду бот
цитировал школьный приказ № 838. Причина не в данных — товар действительно лежит
у заказчика в обеих ветках каталога, — а в том, что основание выбиралось по
правилу «у кого есть номер пункта, тот и прав».
"""

import pytest

from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.profile import DialogProfile
from core.storage import Storage
from core.ui import ProductCard
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"

# Реальный случай из каталога: карточки по лексической теме лежат в садовской
# ветке, но пункт перечня у них только школьный.
LOGO_CARDS = {
    "sku_1c": "52165",
    "name": "ЛОГ Карточки по лексической теме «Овощи»",
    "url": None,
    "short_url": None,
    "price": 160,
    "currency": "RUB",
    "in_stock": 5,
    "category_paths": [
        ["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА", "07. Дидактические игры"],
        ["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838", "Кабинет учителя-логопеда"],
    ],
    "description": "",
    "kit_contents": [],
    "bitrix_id": None,
    "norms": [
        {
            "doc_id": "order_838",
            "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
            "item_code": "2.4.35",
            "item_title": "Дидактические пособия для формирования словарного запаса",
            "source": "heading",
            "confidence": 0.9,
        },
        {
            "doc_id": "order_838",
            "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
            "item_code": "4.4.17",
            "item_title": "Наборы для развития способностей по классификации",
            "source": "heading",
            "confidence": 0.9,
        },
    ],
}

KIT = {
    **LOGO_CARDS,
    "sku_1c": "71258",
    "name": "КМО 2024 Классификация",
    "norms": [
        *LOGO_CARDS["norms"],
        {
            "doc_id": "fgos_do",
            "doc_citation": "ФГОС ДО",
            "item_code": None,
            "item_title": None,
            "source": "mention",
            "confidence": 0.6,
        },
    ],
}


@pytest.fixture
def engine(tmp_path):
    index = CatalogIndex([Product.from_dict(LOGO_CARDS), Product.from_dict(KIT)])
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    return DialogEngine(index, storage, orders, settings)


# --- Профиль ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("оснащаю кабинет логопеда в детском саду", "preschool"),
        ("нужно оборудование для школы", "school"),
        ("что есть для ДОУ", "preschool"),
        ("кабинет физики", "school"),
        ("подберите оборудование", None),
    ],
)
def test_audience_from_text(text, expected):
    profile = DialogProfile()
    profile.update_from_text(text)
    assert profile.audience == expected


def test_named_document_decides_audience():
    """Назвали приказ прямо — это надёжнее любых догадок по словам."""
    profile = DialogProfile()
    profile.update_from_text("подбери по приказу 838")
    assert profile.audience == "school"


# --- Выбор основания ----------------------------------------------------------


def test_preschool_does_not_get_school_citation():
    product = Product.from_dict(LOGO_CARDS)
    assert product.norm_for("preschool") is None
    assert product.norms_for("preschool") == []


def test_school_gets_its_own_citation():
    product = Product.from_dict(LOGO_CARDS)
    assert product.norm_for("school").item_code == "2.4.35"


def test_preschool_keeps_preschool_document():
    """У комплекта есть и школьный пункт, и ФГОС ДО — саду показываем ФГОС ДО."""
    product = Product.from_dict(KIT)
    assert product.norm_for("preschool").doc_id == "fgos_do"


def test_closest_item_to_the_question_wins():
    """У товара два пункта. Спросили про классификацию — покажем пункт про неё."""
    product = Product.from_dict(LOGO_CARDS)
    assert product.norm_for("school", "классификация предметов").item_code == "4.4.17"
    assert product.norm_for("school", "словарный запас").item_code == "2.4.35"


# --- Карточка -----------------------------------------------------------------


def test_card_for_preschool_hides_school_order(engine):
    session = engine.session("u1", CHANNEL)
    session.profile.update_from_text("кабинет логопеда в детском саду")
    card = engine.handle_action("u1", CHANNEL, "card:52165")[0]

    assert isinstance(card, ProductCard)
    assert not any("838" in line for line in card.norms)
    assert card.citation is None


def test_card_says_plainly_that_there_is_no_basis(engine):
    """Молчание об основании читается как «не проверяли». Говорим честно."""
    session = engine.session("u1", CHANNEL)
    session.profile.update_from_text("детский сад")
    card = engine.handle_action("u1", CHANNEL, "card:52165")[0]
    assert card.norms == [
        "в перечнях для дошкольных организаций эта позиция не числится — уточнит менеджер"
    ]


def test_card_for_school_lists_all_items(engine):
    """В подробной карточке — все пункты: по ним собирают спецификацию."""
    session = engine.session("u2", CHANNEL)
    session.profile.update_from_text("оснащаем кабинет логопеда в школе")
    card = engine.handle_action("u2", CHANNEL, "card:52165")[0]

    assert len(card.norms) == 2
    assert any("2.4.35" in line for line in card.norms)
    assert any("4.4.17" in line for line in card.norms)
