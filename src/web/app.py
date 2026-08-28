"""HTTP-слой: виджет для сайта и служебные ручки.

Виджет подключается к сайту одним тегом:

    <script src="https://<хост>/widget.js" defer></script>

Пока доступа к шаблону vdm.ru нет, прототип показывается на своей демо-странице
по адресу `/demo`.

    python -m web.app
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from core.app import build_engine
from core.config import Settings
from web.render import to_json

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
CHANNEL = "web"


class SessionIn(BaseModel):
    # Посетитель, вернувшийся на сайт, присылает свой прежний идентификатор,
    # чтобы не потерять корзину. Проверяем только формат: он анонимный.
    session_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class MessageIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    text: str = Field(default="", max_length=2000)


class ActionIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    action: str = Field(min_length=1, max_length=128)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = build_engine(settings)
    app = FastAPI(title="ЭЛТИ-КУДИЦ · бот-консультант", docs_url=None, redoc_url=None)

    # Виджет ставится на сайт заказчика, поэтому список источников задаётся явно:
    # открывать его всему интернету незачем.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.widget_allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "products": len(engine.index.products),
            "llm": settings.llm_enabled,
            "order_sink": getattr(engine.orders.sink, "name", "?"),
        }

    @app.post("/widget/session")
    def start_session(payload: SessionIn | None = None) -> JSONResponse:
        """Анонимный идентификатор посетителя: до согласия никаких персональных данных.

        Прежний идентификатор переиспользуется — иначе вернувшийся посетитель теряет
        собранную корзину, — но приветствие отдаётся всегда, чтобы окно не открывалось
        пустым.
        """
        session_id = (payload.session_id if payload else None) or uuid.uuid4().hex
        responses = engine.start(session_id, CHANNEL)
        cart = engine.storage.load_cart(session_id)
        if not cart.is_empty:
            responses += engine.handle_action(session_id, CHANNEL, "cart")
        return JSONResponse({"session_id": session_id, "responses": to_json(responses)})

    @app.post("/widget/message")
    def message(payload: MessageIn) -> JSONResponse:
        responses = engine.handle_text(payload.session_id, CHANNEL, payload.text)
        return JSONResponse({"responses": to_json(responses)})

    @app.post("/widget/action")
    def action(payload: ActionIn) -> JSONResponse:
        responses = engine.handle_action(payload.session_id, CHANNEL, payload.action)
        return JSONResponse({"responses": to_json(responses)})

    @app.get("/media/{sku_1c}")
    def product_photo(sku_1c: str) -> FileResponse:
        """Снимок товара из нашего хранилища.

        Отдаём файл сами, а не ссылаемся на vdm.ru: с части сетей сайт заказчика
        не открывается, и виджет тогда показывает битую картинку вместо товара.
        """
        product = engine.index.get(sku_1c)
        path = engine.photo_path(product) if product is not None else None
        if path is None:
            raise HTTPException(status_code=404, detail="Снимок не собран")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/widget.js")
    def widget_js() -> FileResponse:
        # no-cache, а не запрет кэша: браузер держит копию, но каждый раз
        # сверяется с сервером. Иначе обновление виджета доедет до посетителей
        # сайта только после того, как у них истечёт кэш.
        return FileResponse(
            STATIC / "widget.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/demo", response_class=HTMLResponse)
    def demo(request: Request) -> HTMLResponse:
        html = (STATIC / "demo.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__BASE_URL__", str(request.base_url).rstrip("/")))

    return app


app = create_app() if __name__ != "__main__" else None


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.widget_host, port=settings.widget_port)


if __name__ == "__main__":
    main()
