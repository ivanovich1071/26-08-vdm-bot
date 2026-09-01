"""Учёт токенов и рублей за пользовательский сценарий.

Считается для разработки, а не для показа: цифры уходят в журнал диалогов, в чат
не попадают. Ход почти никогда не равен одному обращению к модели — сначала вызовы
инструментов, потом ответ, иногда переписывание из-за выдуманной цены, — поэтому
расход складывается накопительно.
"""

import json

from agent.agent import _account, _assistant_message
from agent.client import ChatClient
from core.dialog import Session
from observability.dialog_log import DialogLogger


def client():
    # Цены Cloud.ru за DeepSeek-V4-Flash: 18,53 ₽ за миллион входных токенов,
    # 37,08 ₽ за миллион генерируемых.
    return ChatClient(api_key="x", model="deepseek-ai/DeepSeek-V4-Flash",
                      name="cloudru", price_in=18.53, price_out=37.08)


def message(tokens_in, tokens_out, content="ответ"):
    return {
        "role": "assistant",
        "content": content,
        "_usage": {
            "model": "deepseek-ai/DeepSeek-V4-Flash",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        },
    }


def test_cost_is_counted_in_rubles():
    session = Session(user_id="u1", channel="telegram")
    _account(session, client(), message(1_000_000, 1_000_000))
    assert session.usage["cost_rub"] == 55.61
    assert session.usage["provider"] == "cloudru"


def test_turn_sums_every_call_to_the_model():
    """Один ход — несколько обращений: инструменты, потом ответ."""
    session = Session(user_id="u1", channel="telegram")
    for _ in range(3):
        _account(session, client(), message(1000, 200))

    assert session.usage["calls"] == 3
    assert session.usage["tokens_in"] == 3000
    assert session.usage["tokens_out"] == 600


def test_answer_without_usage_does_not_break_accounting():
    session = Session(user_id="u1", channel="telegram")
    _account(session, client(), {"role": "assistant", "content": "без расхода"})
    assert session.usage == {}


def test_service_fields_do_not_go_back_to_the_provider():
    """Расход — наша пометка. Провайдер её не поймёт и ответит ошибкой."""
    assert "_usage" not in _assistant_message(message(10, 5))


def test_usage_reaches_the_log(tmp_path):
    logger = DialogLogger(path=tmp_path, enabled=True)
    logger.turn(
        channel="telegram",
        user_id="u1",
        kind="text",
        incoming="что есть для спортзала",
        responses=[],
        mode="agent",
        latency_ms=1200,
        cart_count=0,
        usage={"tokens_in": 900, "tokens_out": 300, "cost_rub": 0.0278},
    )
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").strip())
    assert record["usage"]["cost_rub"] == 0.0278


def test_log_without_usage_has_no_such_field(tmp_path):
    """Ход без обращения к модели ничего не стоит — и строки о стоимости не даёт."""
    logger = DialogLogger(path=tmp_path, enabled=True)
    logger.turn(
        channel="telegram",
        user_id="u1",
        kind="action",
        incoming="cart",
        responses=[],
        mode="search",
        latency_ms=3,
        cart_count=0,
    )
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").strip())
    assert "usage" not in record
