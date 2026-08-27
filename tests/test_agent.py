"""Проверка агента на подставном сервере, отвечающем как Cloud.ru.

Живой Cloud.ru из тестов не дёргаем: он платный, медленный и недоступен с части
сетей. А вот всё остальное — цикл вызова инструментов, маскирование персональных
данных, откат на поиск и пауза после сбоя — проверяется здесь целиком, включая
настоящий HTTP-запрос нашего клиента.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.agent import SalesAgent
from agent.client import ChatClient
from catalog.models import Product
from catalog.search import CatalogIndex
from core.config import Settings
from core.dialog import DialogEngine
from core.storage import Storage
from core.ui import Message, ProductCard, ProductList
from orders.service import OrderService
from orders.sinks import JsonlSink

CHANNEL = "telegram"
USER = "u1"


class FakeCloudRu:
    """Минимальный сервер, совместимый с /v1/chat/completions."""

    def __init__(self, script: list[dict]) -> None:
        self.script = script
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                message = outer.script[min(len(outer.requests) - 1, len(outer.script) - 1)]
                payload = json.dumps({"choices": [{"message": message}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeCloudRu:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"


def tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def answer(text: str) -> dict:
    return {"role": "assistant", "content": text}


@pytest.fixture
def engine(tmp_path):
    index = CatalogIndex(
        [
            Product.from_dict(
                {
                    "sku_1c": "S1",
                    "name": "Фрезерный станок с ЧПУ",
                    "price": 253000,
                    "currency": "RUB",
                    "in_stock": 1,
                    "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"]],
                    "description": "",
                    "kit_contents": [],
                    "norms": [
                        {
                            "doc_id": "order_838",
                            "doc_citation": "приказ Минпросвещения России от 28.11.2024 № 838",
                            "item_code": "2.20.63",
                            "item_title": None,
                            "source": "heading",
                            "confidence": 0.9,
                        }
                    ],
                    "bitrix_id": None,
                    "url": "https://vdm.ru/s1",
                    "short_url": None,
                }
            )
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    return DialogEngine(
        index, storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings
    )


def attach(engine: DialogEngine, base_url: str, timeout: float = 10.0) -> SalesAgent:
    agent = SalesAgent(engine, ChatClient(api_key="test", base_url=base_url, timeout=timeout))
    engine.agent = agent
    return agent


def test_agent_calls_tool_then_answers(engine):
    script = [
        tool_call("search_products", {"query": "фрезерный станок"}),
        answer("Подойдёт фрезерно-гравировальный станок S1, позиция 2.20.63 приказа № 838."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(engine, cloud.base_url)
        responses = engine.handle_text(USER, CHANNEL, "нужен фрезерный станок")

    assert isinstance(responses[0], Message)
    assert "2.20.63" in responses[0].text
    # Карточка приложена к ответу, чтобы товар можно было положить в корзину.
    assert any(isinstance(r, ProductCard) for r in responses)


def test_tool_result_reaches_the_model(engine):
    script = [tool_call("find_by_norm_code", {"code": "2.20.63"}), answer("Нашёл.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, cloud.base_url)
        engine.handle_text(USER, CHANNEL, "позиция 2.20.63")
        second = cloud.requests[1]

    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert tool_messages, "результат инструмента не отправлен модели"
    assert "Фрезерный станок с ЧПУ" in tool_messages[0]["content"]


def test_personal_data_never_leaves_for_the_model(engine):
    script = [answer("Записал, свяжемся.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, cloud.base_url)
        engine.handle_text(
            USER, CHANNEL, "Я Петров Пётр, телефон +7 916 330-02-79, почта p@example.ru"
        )
        sent = json.dumps(cloud.requests[0], ensure_ascii=False)

    assert "916" not in sent and "p@example.ru" not in sent and "Петров" not in sent
    assert "[PHONE_1]" in sent


def test_agent_can_add_to_cart_through_tool(engine):
    script = [
        tool_call("add_to_cart", {"sku_1c": "S1", "quantity": 2}),
        answer("Добавил две штуки."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(engine, cloud.base_url)
        engine.handle_text(USER, CHANNEL, "добавь два станка")

    assert engine.storage.load_cart(USER).count == 2


def test_unknown_tool_does_not_break_the_dialog(engine):
    script = [tool_call("teleport", {"where": "луна"}), answer("Такого не умею.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, cloud.base_url)
        responses = engine.handle_text(USER, CHANNEL, "телепортируй станок")

    assert isinstance(responses[0], Message)


def test_falls_back_to_search_when_model_is_down(engine):
    agent = attach(engine, "http://127.0.0.1:1/v1", timeout=1.0)
    responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

    assert isinstance(responses[0], ProductList)
    assert responses[0].cards[0].product.sku_1c == "S1"
    assert not agent.available, "после сбоя модель должна уйти в паузу"


def test_cooldown_answers_instantly_without_touching_the_model(engine):
    """Регрессия: без паузы каждое сообщение ждало бы полный таймаут."""
    with FakeCloudRu([answer("привет")]) as cloud:
        agent = attach(engine, cloud.base_url)
        agent._mark_unavailable()
        responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

        assert cloud.requests == [], "в паузе обращений к модели быть не должно"
    assert isinstance(responses[0], ProductList)


def test_empty_answer_falls_back_to_search(engine):
    with FakeCloudRu([answer("")]) as cloud:
        attach(engine, cloud.base_url)
        responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

    assert isinstance(responses[0], ProductList)


def test_prompts_are_sent_as_system_message(engine):
    with FakeCloudRu([answer("ок")]) as cloud:
        attach(engine, cloud.base_url)
        engine.handle_text(USER, CHANNEL, "привет")
        system = cloud.requests[0]["messages"][0]

    assert system["role"] == "system"
    assert "ЭЛТИ-КУДИЦ" in system["content"]
    # Служебные названия этапов продажи должны быть в промпте, но с запретом
    # показывать их пользователю.
    assert "никогда не пиши их" in system["content"].lower()


def test_tools_are_declared_to_the_model(engine):
    with FakeCloudRu([answer("ок")]) as cloud:
        attach(engine, cloud.base_url)
        engine.handle_text(USER, CHANNEL, "привет")
        names = {t["function"]["name"] for t in cloud.requests[0]["tools"]}

    assert {"search_products", "find_by_norm_code", "add_to_cart"} <= names
