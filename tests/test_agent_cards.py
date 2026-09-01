"""Сведение текста ответа модели с карточками товаров.

Регрессия с демонстрации: модель перечислила интерактивное зеркало, песочницу и
метроном, а карточками пришли парта логопеда и карточки «Овощи». Совпадение
искалось по кодам 1С, а модель их не пишет — и на месте ненайденных подставлялись
первые попавшиеся позиции из поиска.
"""

import pytest

from agent.agent import SalesAgent, _without_codes
from agent.providers import LLMRouter
from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"


def product(sku, name):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "url": None,
            "short_url": None,
            "price": 1000,
            "currency": "RUB",
            "in_stock": 1,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]],
            "description": "",
            "kit_contents": [],
            "norms": [],
            "bitrix_id": None,
        }
    )


class FakeTools:
    def __init__(self, skus, session):
        self.shown_skus = list(skus)
        self.session = session


@pytest.fixture
def agent(tmp_path):
    index = CatalogIndex(
        [
            product("11", "ИНТ Интерактивное зеркало логопеда"),
            product("22", "ИНТ Интерактивная песочница"),
            product("33", "ВТ ПЛ Парта логопеда"),
            product("44", "ЛОГ Карточки по лексической теме «Овощи»"),
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    orders = OrderService(storage, JsonlSink(path=tmp_path / "orders.jsonl"))
    engine = DialogEngine(index, storage, orders, settings)
    return SalesAgent(engine, LLMRouter(clients=[]))


def session_of(agent):
    return agent.engine.session("u1", CHANNEL)


def test_cards_follow_the_names_in_the_answer(agent):
    answer = (
        "Для кабинета логопеда подойдут интерактивное зеркало и интерактивная "
        "песочница — обе позиции есть под заказ."
    )
    tools = FakeTools(["33", "44", "11", "22"], session_of(agent))
    assert agent._mentioned_skus(tools, answer) == ["11", "22"]


def test_codes_win_when_the_model_names_them(agent):
    answer = "Парта логопеда (код 1С 33) — 24 210 ₽."
    tools = FakeTools(["11", "33"], session_of(agent))
    assert agent._mentioned_skus(tools, answer) == ["33"]


def test_nothing_matched_means_no_cards(agent):
    """Показать наугад хуже, чем не показать: именно так и родилась жалоба."""
    answer = "По этому кабинету нужен проект, подключу менеджера."
    tools = FakeTools(["33", "44"], session_of(agent))
    assert agent._mentioned_skus(tools, answer) == []


def test_single_common_word_is_not_a_match(agent):
    """«Набор» есть у половины каталога — одного общего слова мало."""
    answer = "Могу собрать набор под ваш бюджет."
    tools = FakeTools(["44"], session_of(agent))
    assert agent._mentioned_skus(tools, answer) == []


def test_service_codes_are_cut_before_showing():
    answer = "Парта логопеда (код 1С 34605) — 24 210 ₽, есть под заказ."
    assert _without_codes(answer) == "Парта логопеда — 24 210 ₽, есть под заказ."


def test_code_cut_handles_square_brackets_and_article():
    assert _without_codes("Мяч [артикул У733] — 900 ₽") == "Мяч — 900 ₽"
