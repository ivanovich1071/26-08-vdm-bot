"""Подбор по пункту приказа: номер без документа ничего не значит.

Каждый случай здесь взят из журнала живых диалогов 31.08–02.09 и из разбора
`баги0209.мд`. Общая у них одна причина: индекс кодов был построен по номеру
пункта без документа, а инструмент документ не принимал вовсе. Поэтому на
«2.1.14 по приказу 1057» бот отвечал речевой игрой из школьного приказа 838 —
и человек справедливо писал, что бот всё перепутал.

Модель здесь не участвует: всё проверяется на поиске, инструментах и профиле.
"""

from __future__ import annotations

import json

import pytest

from agent.tools import ToolBox
from catalog.models import Product
from catalog.search import CatalogIndex, SearchQuery
from core.config import Settings
from core.dialog import DialogEngine, Session
from core.storage import Storage
from norms import items as norm_items
from norms.items import ItemIndex, NormItem
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "web"
USER = "u1"


def norm(doc_id: str, code: str, title: str | None = None) -> dict:
    citations = {
        "order_838": "приказ Минпросвещения России от 28.11.2024 № 838",
        "order_1057": "приказ Минпросвещения России от 25.12.2024 № 1057",
    }
    return {
        "doc_id": doc_id,
        "doc_citation": citations[doc_id],
        "source": "heading",
        "confidence": 0.9,
        "item_code": code,
        "item_title": title,
    }


def product(sku: str, name: str, roots: list[str], norms: list[dict], price: int = 1000):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "url": None,
            "short_url": None,
            "price": price,
            "currency": "RUB",
            "in_stock": 3,
            "category_paths": [[root] for root in roots],
            "description": "",
            "kit_contents": [],
            "norms": norms,
            "bitrix_id": None,
        }
    )


SADIK = "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"
SHKOLA = "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"


@pytest.fixture
def index() -> CatalogIndex:
    return CatalogIndex(
        [
            # Тот самый SKU 63054: лежит и в садовской, и в школьной ветке,
            # закрывает 2.1.14 приказа 838 и 1.13.4.3.1.6 приказа 1057.
            product(
                "63054",
                "Речевая игра «Составь сообщение»",
                [SADIK, SHKOLA],
                [
                    norm("order_838", "2.1.14", "Игровые наборы по русскому языку"),
                    norm("order_1057", "1.13.4.3.1.6", "Комплект эмоционального развития"),
                ],
                price=6111,
            ),
            product(
                "OBR52",
                "Обруч маленький диаметр 52 см",
                [SADIK],
                [norm("order_1057", "1.5.1.41", "Обруч гимнастический пластмассовый")],
                price=245,
            ),
            product(
                "TELEGA",
                "Тележка для спортинвентаря",
                ["ОСНАЩЕНИЕ НОВОСТРОЕК"],
                [],
                price=12748,
            ),
        ]
    )


@pytest.fixture
def engine(index, tmp_path, monkeypatch) -> DialogEngine:
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    monkeypatch.setattr(norm_items, "load", lambda *_a, **_kw: {})
    engine = DialogEngine(
        index, storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings
    )
    # Тексты приказов подменяем на два пункта: чтобы проверить «в 1057 такого
    # пункта нет», настоящие полтора мегабайта справочника не нужны.
    engine.norm_texts = ItemIndex(
        {
            "order_838": {
                "2.1.14": NormItem("order_838", "2.1.14", "Игровые наборы по русскому языку"),
                "2.20.63": NormItem("order_838", "2.20.63", "Фрезерно-гравировальный станок"),
            },
            "order_1057": {
                "1.5.1": NormItem("order_1057", "1.5.1", "Спортивное оборудование и инвентарь"),
                "1.5.1.41": NormItem("order_1057", "1.5.1.41", "Обруч гимнастический"),
            },
        }
    )
    return engine


def tools(engine: DialogEngine) -> ToolBox:
    return ToolBox(engine, Session(user_id=USER, channel=CHANNEL))


# --- Поиск по индексу ---------------------------------------------------------


def test_code_from_another_order_does_not_answer(index):
    """Живой сбой 02.09: «2.1.14 приказа 1057» вернул школьную речевую игру."""
    hits = index.search(
        SearchQuery(text="2.1.14", norm_code="2.1.14", norm_doc_id="order_1057", limit=5)
    )
    assert hits == []


def test_the_same_code_answers_in_its_own_order(index):
    hits = index.search(
        SearchQuery(text="2.1.14", norm_code="2.1.14", norm_doc_id="order_838", limit=5)
    )
    assert [hit.product.sku_1c for hit in hits] == ["63054"]
    assert "838" in hits[0].citation()


def test_audience_alone_keeps_the_school_code_away(index):
    """Приказ не назван, но подбираем для сада — школьный пункт всё равно чужой."""
    hits = index.search(
        SearchQuery(text="2.1.14", norm_code="2.1.14", audience="preschool", limit=5)
    )
    assert hits == []


def test_citation_names_the_order_the_code_came_from(index):
    hits = index.search(
        SearchQuery(text="1.5.1.41", norm_code="1.5.1.41", norm_doc_id="order_1057", limit=5)
    )
    assert hits[0].product.sku_1c == "OBR52"
    assert "1057" in hits[0].citation()


def test_unbound_position_says_so_in_the_list(engine):
    """Позиция без основания не должна молчать: пустая строка читается как «есть».

    Почти половина каталога к перечням не привязана — реестра «пункт 838 → код
    1С» у заказчика нет. В подробной карточке об этом было сказано давно, а в
    строке выдачи позиция стояла молча, вперемешку с обоснованными.
    """
    hits = engine.index.search(
        SearchQuery(text="тележка для спортинвентаря", audience="preschool", limit=3)
    )
    listing = engine._list(hits, "Выдача", len(hits), offset=0)
    telega = next(card for card in listing.cards if card.product.sku_1c == "TELEGA")
    assert "не числится" in telega.citation


# --- Инструмент подбора по пункту ---------------------------------------------


def test_tool_says_the_code_lives_elsewhere(engine):
    box = tools(engine)
    result = json.loads(box.run("find_by_norm_code", {"code": "2.1.14", "document": "1057"}))

    assert result["found"] == 0
    assert "1057" in result["note"] and "не содержит" in result["note"]
    assert result["also_in"]["document_id"] == "order_838"


def test_tool_finds_by_code_inside_the_named_order(engine):
    box = tools(engine)
    result = json.loads(box.run("find_by_norm_code", {"code": "1.5.1.41", "document": "1057"}))

    assert result["found"] == 1
    assert result["products"][0]["sku_1c"] == "OBR52"
    assert "1057" in result["norm_item_document"]


def test_tool_takes_the_order_from_the_conversation(engine):
    box = tools(engine)
    box.session.profile.norm_doc_ids.append("order_1057")
    result = json.loads(box.run("find_by_norm_code", {"code": "2.1.14"}))

    assert result["found"] == 0, "приказ известен из разговора — чужой пункт не подходит"


def test_lookup_is_written_to_the_journal(engine):
    box = tools(engine)
    box.run("find_by_norm_code", {"code": "1.5.1.41", "document": "1057"})

    assert box.norm_lookups == [
        {"tool": "find_by_norm_code", "code": "1.5.1.41", "document": "1057", "found": 1}
    ]


# --- Справочник пунктов -------------------------------------------------------


def test_norm_item_found_by_words(engine):
    box = tools(engine)
    result = json.loads(
        box.run("find_norm_item", {"query": "спортивное оборудование", "document": "1057"})
    )

    assert result["found"] >= 1
    assert result["items"][0]["code"] == "1.5.1"


def test_norm_item_answers_that_the_code_does_not_exist(engine):
    """Пункта 2.1.14 в приказе 1057 нет — это надо сказать, а не подбирать похожее."""
    box = tools(engine)
    result = json.loads(box.run("find_norm_item", {"query": "2.1.14", "document": "1057"}))

    assert result["found"] == 0
    assert "не содержит" in result["note"]
    assert result["also_in"]["document"].endswith("838")


def test_norm_item_returns_the_wording_with_its_document(engine):
    box = tools(engine)
    result = json.loads(box.run("find_norm_item", {"query": "2.20.63"}))

    assert result["items"][0]["title"].startswith("Фрезерно")
    assert result["items"][0]["document_id"] == "order_838"


# --- Аудитория и справка ------------------------------------------------------


def test_asking_about_a_document_does_not_switch_the_audience(engine):
    """Живой сбой 31.08: вопрос про 838 перевёл в школьный режим весь разговор."""
    engine.handle_text(USER, CHANNEL, "что значит указ 838")
    session = engine.session(USER, CHANNEL)
    assert session.profile.asked_about_docs == ["order_838"]
    assert session.profile.norm_doc_ids == []

    engine.handle_text(USER, CHANNEL, "покажи позиции для кабинета логопеда в детском саду")
    assert session.profile.audience == "preschool"


def test_supplying_by_a_document_does_switch_the_audience(engine):
    engine.handle_text(USER, CHANNEL, "подбери по приказу 1057 всё для спортзала")
    profile = engine.session(USER, CHANNEL).profile
    assert profile.norm_doc_ids == ["order_1057"]
    assert profile.audience == "preschool"


def test_norm_question_without_the_model_gets_a_reference(engine):
    """01.09: на вопрос про приказ без модели бот выдал полсотни случайных товаров."""
    from core.ui import ProductList

    responses = engine.offer(
        engine.session(USER, CHANNEL), "по какому приказу оснащается детский сад"
    )
    assert not any(isinstance(item, ProductList) for item in responses)
    actions = [button.action for row in responses[0].keyboard.rows for button in row]
    assert "norm_doc:order_1057" in actions


def test_named_document_without_the_model_gets_its_reference(engine):
    responses = engine.offer(engine.session(USER, CHANNEL), "что значит приказ 838")
    assert "838" in responses[0].text


def test_reference_tells_the_whole_size_of_the_order(engine):
    from norms import reference

    text = reference.explain(
        "order_1057", reference.coverage(engine.index, "order_1057", engine.norm_texts.count("order_1057"))
    )
    assert "из 2" in text, "в справке должно быть и закрыто, и всего пунктов"
