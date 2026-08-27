import json

import pytest

from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from observability.dialog_log import DialogLogger, read_dialogs
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"


def product(sku="S1", name="Фрезерный станок с ЧПУ"):
    return Product.from_dict(
        {
            "sku_1c": sku,
            "name": name,
            "price": 253000,
            "currency": "RUB",
            "in_stock": 1,
            "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
            "description": "",
            "kit_contents": [],
            "norms": [],
            "bitrix_id": None,
            "url": None,
            "short_url": None,
        }
    )


@pytest.fixture
def engine(tmp_path):
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    logger = DialogLogger(path=tmp_path / "dialogs")
    return DialogEngine(
        CatalogIndex([product()]),
        storage,
        OrderService(storage, JsonlSink(tmp_path / "o.jsonl")),
        settings,
        dialog_log=logger,
    )


def records(tmp_path):
    files = sorted((tmp_path / "dialogs").glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text("utf-8").splitlines()]


def test_text_and_answer_are_recorded(engine, tmp_path):
    engine.handle_text(USER, CHANNEL, "фрезерный станок")
    entry = records(tmp_path)[0]

    assert entry["channel"] == CHANNEL
    assert entry["in"] == "фрезерный станок"
    assert entry["out"][0]["type"] == "list"
    assert entry["out"][0]["items"][0]["sku"] == "S1"


def test_button_presses_are_recorded(engine, tmp_path):
    engine.handle_action(USER, CHANNEL, "add:S1")
    entry = records(tmp_path)[0]
    assert entry["kind"] == "action" and entry["in"] == "add:S1"
    assert entry["cart_count"] == 1


def test_user_id_is_pseudonymized(engine, tmp_path):
    engine.handle_text(USER, CHANNEL, "мяч")
    entry = records(tmp_path)[0]
    assert USER not in json.dumps(entry, ensure_ascii=False)
    assert len(entry["session"]) == 16


def test_same_user_keeps_one_session_id(engine, tmp_path):
    engine.handle_text(USER, CHANNEL, "мяч")
    engine.handle_text(USER, CHANNEL, "станок")
    sessions = {entry["session"] for entry in records(tmp_path)}
    assert len(sessions) == 1


def test_personal_data_is_masked_in_input(engine, tmp_path):
    engine.handle_text(USER, CHANNEL, "почта school@example.ru, тел +7 916 330-02-79")
    dump = json.dumps(records(tmp_path), ensure_ascii=False)
    assert "school@example.ru" not in dump and "916" not in dump


def test_echoed_query_in_title_is_masked(engine, tmp_path):
    """Регрессия: заголовок выдачи цитировал запрос и возвращал телефон в журнал."""
    engine.handle_text(USER, CHANNEL, "тел +7 916 330-02-79")
    dump = json.dumps(records(tmp_path), ensure_ascii=False)
    assert "330-02-79" not in dump


def test_checkout_answers_are_not_written(engine, tmp_path):
    """Ответы на вопросы о контактах — чистые ПДн, в журнале им делать нечего."""
    engine.handle_action(USER, CHANNEL, "add:S1")
    engine.handle_action(USER, CHANNEL, "checkout")
    engine.handle_action(USER, CHANNEL, "consent_yes")
    engine.handle_text(USER, CHANNEL, "Гимназия 1")
    engine.handle_text(USER, CHANNEL, "Петрова Мария")

    entries = [e for e in records(tmp_path) if e["kind"] == "text"]
    assert entries and all(e["in"] == "<контактные данные при оформлении>" for e in entries)
    assert "Гимназия" not in json.dumps(records(tmp_path), ensure_ascii=False)


def test_disabled_logger_writes_nothing(tmp_path):
    logger = DialogLogger(path=tmp_path / "off", enabled=False)
    logger.turn(
        channel="web", user_id="u", kind="text", incoming="привет",
        responses=[], mode="search", latency_ms=1, cart_count=0,
    )
    assert not (tmp_path / "off").exists()


def test_broken_logger_does_not_break_the_dialog(engine, tmp_path):
    engine.dialog_log.path = tmp_path / "dialogs" / "занято.jsonl"
    (tmp_path / "dialogs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dialogs" / "занято.jsonl").write_text("", encoding="utf-8")

    responses = engine.handle_text(USER, CHANNEL, "станок")
    assert responses, "ошибка журнала не должна ронять ответ пользователю"


def test_read_dialogs_groups_by_session(engine, tmp_path):
    engine.handle_text(USER, CHANNEL, "мяч")
    engine.handle_text("u2", CHANNEL, "станок")
    sessions = read_dialogs(tmp_path / "dialogs")
    assert len(sessions) == 2
