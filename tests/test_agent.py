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
from agent.providers import LLMRouter
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
    """Минимальный сервер, совместимый с /v1/chat/completions.

    `strict_reasoning` повторяет поведение настоящего Cloud.ru: у каждого ответа
    ассистента в присланной истории должно быть поле `reasoning_content`, иначе
    приходит 400. Ровно на этом прототип и падал со второго хода разговора.
    """

    def __init__(self, script: list[dict], strict_reasoning: bool = False) -> None:
        self.script = script
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)

                bad = outer._missing_reasoning(body) if strict_reasoning else None
                if bad is not None:
                    self._reply(
                        400,
                        {
                            "error": {
                                "message": "request param validation error, Value error, "
                                f"Missing `reasoning_content` field in the assistant "
                                f"message at index {bad}"
                            }
                        },
                    )
                    return

                message = outer.script[min(len(outer.requests) - 1, len(outer.script) - 1)]
                self._reply(200, {"choices": [{"message": message}]})

            def _reply(self, code: int, data: dict) -> None:
                payload = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _missing_reasoning(body: dict) -> int | None:
        for index, message in enumerate(body.get("messages", [])):
            if message.get("role") == "assistant" and "reasoning_content" not in message:
                return index
        return None

    def __enter__(self) -> FakeCloudRu:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"


def tool_call(name: str, arguments: dict, reasoning: str = "") -> dict:
    return {
        "role": "assistant",
        "content": "",
        # Рассуждающие модели Cloud.ru отдают это поле рядом с content и требуют
        # его обратно. Подставной сервер повторяет их поведение.
        "reasoning_content": reasoning,
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


def client(base_url: str, name: str = "cloudru", timeout: float = 10.0) -> ChatClient:
    return ChatClient(api_key="test", base_url=base_url, timeout=timeout, name=name)


def attach(engine: DialogEngine, *clients: ChatClient) -> SalesAgent:
    agent = SalesAgent(engine, LLMRouter(clients=list(clients)))
    engine.agent = agent
    return agent


def test_agent_calls_tool_then_answers(engine):
    script = [
        tool_call("search_products", {"query": "фрезерный станок"}),
        answer("Подойдёт фрезерно-гравировальный станок S1, позиция 2.20.63 приказа № 838."),
    ]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "нужен фрезерный станок")

    assert isinstance(responses[0], Message)
    assert "2.20.63" in responses[0].text
    # Карточка приложена к ответу, чтобы товар можно было положить в корзину.
    assert any(isinstance(r, ProductCard) for r in responses)


def test_tool_result_reaches_the_model(engine):
    script = [tool_call("find_by_norm_code", {"code": "2.20.63"}), answer("Нашёл.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        engine.handle_text(USER, CHANNEL, "позиция 2.20.63")
        second = cloud.requests[1]

    tool_messages = [m for m in second["messages"] if m["role"] == "tool"]
    assert tool_messages, "результат инструмента не отправлен модели"
    assert "Фрезерный станок с ЧПУ" in tool_messages[0]["content"]


def test_personal_data_never_leaves_for_the_model(engine):
    script = [answer("Записал, свяжемся.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
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
        attach(engine, client(cloud.base_url))
        engine.handle_text(USER, CHANNEL, "добавь два станка")

    assert engine.storage.load_cart(USER).count == 2


def test_unknown_tool_does_not_break_the_dialog(engine):
    script = [tool_call("teleport", {"where": "луна"}), answer("Такого не умею.")]
    with FakeCloudRu(script) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "телепортируй станок")

    assert isinstance(responses[0], Message)


def test_falls_back_to_search_when_model_is_down(engine):
    agent = attach(engine, client("http://127.0.0.1:1/v1", timeout=1.0))
    responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

    assert isinstance(responses[0], ProductList)
    assert responses[0].cards[0].product.sku_1c == "S1"
    assert not agent.available, "после сбоя модель должна уйти в паузу"


def test_cooldown_answers_instantly_without_touching_the_model(engine):
    """Регрессия: без паузы каждое сообщение ждало бы полный таймаут."""
    with FakeCloudRu([answer("привет")]) as cloud:
        only = client(cloud.base_url)
        agent = attach(engine, only)
        agent.router.mark_down(only, RuntimeError("сеть легла"))
        responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

        assert cloud.requests == [], "в паузе обращений к модели быть не должно"
    assert isinstance(responses[0], ProductList)


def test_empty_answer_falls_back_to_search(engine):
    with FakeCloudRu([answer("")]) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

    assert isinstance(responses[0], ProductList)


def test_prompts_are_sent_as_system_message(engine):
    with FakeCloudRu([answer("ок")]) as cloud:
        attach(engine, client(cloud.base_url))
        engine.handle_text(USER, CHANNEL, "привет")
        system = cloud.requests[0]["messages"][0]

    assert system["role"] == "system"
    assert "ЭЛТИ-КУДИЦ" in system["content"]
    # Служебные названия этапов продажи должны быть в промпте, но с запретом
    # показывать их пользователю.
    assert "никогда не пиши их" in system["content"].lower()


def test_tools_are_declared_to_the_model(engine):
    with FakeCloudRu([answer("ок")]) as cloud:
        attach(engine, client(cloud.base_url))
        engine.handle_text(USER, CHANNEL, "привет")
        names = {t["function"]["name"] for t in cloud.requests[0]["tools"]}

    assert {"search_products", "find_by_norm_code", "add_to_cart"} <= names


def test_reasoning_content_survives_the_tool_round(engine):
    """Регрессия 28.08: без этого поля Cloud.ru отклонял каждый второй ход.

    Модель отдаёт `reasoning_content` рядом с `content`, а мы собирали сообщение
    ассистента заново из content и tool_calls и поле теряли. Первый ход проходил,
    следующий падал с 400 — и бот молча уходил в поиск.
    """
    script = [
        tool_call("search_products", {"query": "станок"}, reasoning="ищу станок"),
        answer("Нашёл станок S1."),
    ]
    with FakeCloudRu(script, strict_reasoning=True) as cloud:
        attach(engine, client(cloud.base_url))
        responses = engine.handle_text(USER, CHANNEL, "нужен станок")
        second = cloud.requests[1]

    assert isinstance(responses[0], Message), "ответ модели не дошёл до пользователя"
    assistant = [m for m in second["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[0]["reasoning_content"] == "ищу станок"


def test_history_of_past_turns_carries_the_field_too(engine):
    """Индекс в ошибке рос вместе с разговором: падали и сообщения из истории."""
    with FakeCloudRu([answer("Слушаю.")], strict_reasoning=True) as cloud:
        attach(engine, client(cloud.base_url))
        engine.handle_text(USER, CHANNEL, "здравствуйте")
        engine.handle_text(USER, CHANNEL, "что есть для логопеда")
        second = cloud.requests[1]

    assistant = [m for m in second["messages"] if m["role"] == "assistant"]
    assert assistant, "прошлый ответ бота не попал в историю — контекст теряется"
    assert all("reasoning_content" in m for m in assistant)


def test_second_provider_picks_up_when_the_first_is_down(engine):
    with FakeCloudRu([answer("Отвечает запасной провайдер.")]) as cloud:
        agent = attach(
            engine,
            client("http://127.0.0.1:1/v1", name="cloudru", timeout=1.0),
            client(cloud.base_url, name="openrouter"),
        )
        responses = engine.handle_text(USER, CHANNEL, "нужен станок")

    assert isinstance(responses[0], Message)
    assert "запасной" in responses[0].text
    # Упавший провайдер отключён, живой — нет: пауза считается по каждому отдельно.
    assert [c.name for c in agent.router.ready()] == ["openrouter"]


def test_search_answers_only_when_every_provider_is_down(engine):
    agent = attach(
        engine,
        client("http://127.0.0.1:1/v1", name="cloudru", timeout=1.0),
        client("http://127.0.0.1:2/v1", name="openrouter", timeout=1.0),
    )
    responses = engine.handle_text(USER, CHANNEL, "фрезерный станок")

    assert isinstance(responses[0], ProductList)
    assert not agent.available


def test_auth_failure_pauses_the_provider_for_longer(engine):
    """402 «нет денег» сам не пройдёт: повторять его каждые пять минут незачем."""
    from agent.client import LLMAuthError
    from agent.providers import AUTH_COOLDOWN_SECONDS, LLMRouter

    only = client("http://127.0.0.1:1/v1")
    router = LLMRouter(clients=[only])
    router.mark_down(only, LLMAuthError("cloudru вернул 402: Not enough money"))

    assert not router.available
    assert router.status()[0]["blocked_for"] > AUTH_COOLDOWN_SECONDS - 5
