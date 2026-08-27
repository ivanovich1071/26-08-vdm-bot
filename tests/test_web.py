import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from catalog.models import Product  # noqa: E402
from catalog.search import CatalogIndex  # noqa: E402
from core.config import Settings  # noqa: E402
from core.dialog import DialogEngine  # noqa: E402
from core.storage import Storage  # noqa: E402
from orders.service import OrderService  # noqa: E402
from orders.sinks import JsonlSink  # noqa: E402
from web import app as web_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    index = CatalogIndex(
        [
            Product.from_dict(
                {
                    "sku_1c": "S1",
                    "name": "Мяч баскетбольный",
                    "price": 908,
                    "currency": "RUB",
                    "in_stock": 4,
                    "category_paths": [["ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА"]],
                    "description": "",
                    "kit_contents": [],
                    "norms": [],
                    "bitrix_id": None,
                    "url": None,
                    "short_url": None,
                }
            )
        ]
    )
    storage = Storage(tmp_path / "t.sqlite3")
    settings = Settings(orders_jsonl_path=str(tmp_path / "orders.jsonl"))
    engine = DialogEngine(index, storage, OrderService(storage, JsonlSink(tmp_path / "o.jsonl")), settings)
    monkeypatch.setattr(web_app, "build_engine", lambda _settings: engine)
    return TestClient(web_app.create_app(settings))


def test_health_reports_catalog_size(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["products"] == 1


def test_session_returns_greeting(client):
    body = client.post("/widget/session").json()
    assert len(body["session_id"]) == 32
    assert body["responses"][0]["type"] == "text"


def test_returning_visitor_keeps_cart_and_still_gets_greeting(client):
    """Регрессия: с сохранённым идентификатором окно открывалось пустым."""
    session_id = client.post("/widget/session").json()["session_id"]
    client.post("/widget/action", json={"session_id": session_id, "action": "add:S1"})

    body = client.post("/widget/session", json={"session_id": session_id}).json()

    assert body["session_id"] == session_id
    kinds = [response["type"] for response in body["responses"]]
    assert kinds[0] == "text" and "order" in kinds


def test_bad_session_id_is_rejected(client):
    assert client.post("/widget/session", json={"session_id": "не-hex"}).status_code == 422


def test_search_returns_list(client):
    session_id = client.post("/widget/session").json()["session_id"]
    body = client.post("/widget/message", json={"session_id": session_id, "text": "мяч"}).json()
    assert body["responses"][0]["type"] == "list"


def test_widget_js_is_revalidated(client):
    response = client.get("/widget.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
