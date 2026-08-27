"""Загрузка страниц сайта заказчика.

Сайт принадлежит заказчику, и бот собирается для него же, так что вопрос
разрешения снят. Остаётся техническая сдержанность: vdm.ru — рабочий сайт
с живыми покупателями, и укладывать его выгрузкой собственных же фотографий
незачем. Отсюда ограничение частоты, честный User-Agent с контактом и условные
запросы — повторно скачивать неизменившуюся страницу бессмысленно.

`robots.txt` по умолчанию не спрашиваем: он написан для поисковых роботов,
а мы действуем от имени владельца. Проверку можно вернуть одной настройкой.

Модуль ничего не разбирает: его дело — принести байты либо внятно сказать, почему
не вышло.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

# Только латиница: заголовки HTTP кодируются в latin-1, и кириллица в User-Agent
# роняет запрос ещё до отправки.
DEFAULT_USER_AGENT = "VdmBot/0.1 (catalog assistant for vdm.ru; contact: elti@vdm.ru)"


class FetchError(RuntimeError):
    """Страницу получить не удалось. Вызывающий решает, повторять ли позже."""


@dataclass
class FetchResult:
    body: bytes | None
    status: int
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


@dataclass
class PageFetcher:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 20.0
    # Минимальный промежуток между запросами. Значение по умолчанию — один запрос
    # в секунду: столько выдержит любой сайт, а согласованный лимит выставляется
    # в настройках.
    min_interval: float = 1.0
    retries: int = 2
    respect_robots: bool = False

    _last_request: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict, init=False
    )

    def get(
        self,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        if self.respect_robots and not self.allowed(url):
            raise FetchError(f"robots.txt запрещает загрузку {url}")

        headers = {"User-Agent": _ascii(self.user_agent), "Accept": "text/html,*/*"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return FetchResult(
                        body=response.read(),
                        status=response.status,
                        etag=response.headers.get("ETag"),
                        last_modified=response.headers.get("Last-Modified"),
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    return FetchResult(body=None, status=304, etag=etag, last_modified=last_modified)
                if exc.code in {404, 410}:
                    # Товар снят с сайта — повторять бессмысленно.
                    raise FetchError(f"{url}: {exc.code}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt < self.retries:
                # Выдержка растёт: сайту могло стать плохо именно от нас.
                time.sleep(self.min_interval * (attempt + 1) * 2)

        raise FetchError(f"{url}: {last_error}")

    def allowed(self, url: str) -> bool:
        parser = self._robots_for(url)
        if parser is None:
            # robots.txt недоступен — не запрещаем, но оставляем след в журнале.
            return True
        return parser.can_fetch(self.user_agent, url)

    # --- Внутреннее -----------------------------------------------------------

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(urljoin(origin, "/robots.txt"))
        try:
            self._throttle()
            parser.read()
        except Exception as exc:
            log.warning("robots.txt не прочитан (%s): %s", origin, exc)
            parser = None  # type: ignore[assignment]
        self._robots[origin] = parser
        return parser

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()


def _ascii(value: str) -> str:
    """Заголовок без кириллицы: HTTP допускает только latin-1."""
    cleaned = value.encode("ascii", "ignore").decode("ascii").strip()
    return cleaned or DEFAULT_USER_AGENT
