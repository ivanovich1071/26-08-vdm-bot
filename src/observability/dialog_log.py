"""Журнал диалогов — сырьё для доработки промптов.

Пишется на уровне ядра, а не адаптеров: тогда Telegram, виджет и будущий MAX
логируются одинаково, и сравнивать их поведение можно напрямую.

ФЗ-152: в журнал уходит **обезличенный** текст — имена, телефоны, почта и ИНН
заменяются метками до записи. Для настройки промптов реальные контакты не нужны,
а хранить их — значит защищать и удалять ещё одну копию персональных данных.
Идентификатор пользователя заменяется устойчивым хешем: диалог одного человека
остаётся склеенным, но сам человек по журналу не восстанавливается.

Сбой записи никогда не роняет диалог: журнал — вспомогательный инструмент.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.ui import Message, OrderSummary, ProductCard, ProductList, Response
from privacy.masking import Masker

log = logging.getLogger(__name__)

SALT = "vdm-dialog-log"


@dataclass
class DialogLogger:
    path: Path = Path("data/dialogs")
    enabled: bool = True
    mask_personal_data: bool = True

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def turn(
        self,
        *,
        channel: str,
        user_id: str,
        kind: str,
        incoming: str,
        responses: list[Response],
        mode: str,
        latency_ms: int,
        cart_count: int,
        usage: dict | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            record = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "channel": channel,
                "session": self._pseudonym(channel, user_id),
                "kind": kind,
                "mode": mode,
                "latency_ms": latency_ms,
                "cart_count": cart_count,
                "in": self._clean(incoming),
                "out": [self._describe(r) for r in responses],
            }
            # Расход модели за ход. Складывая его по сессии, получаем стоимость
            # одного пользовательского сценария — то, ради чего это и пишется.
            # Пользователю цифры не показываются: это данные для разработки.
            if usage:
                record["usage"] = usage
            self._write(record)
        except Exception as exc:  # журнал не должен ломать разговор
            log.warning("Не удалось записать диалог: %s", exc)

    # --- Внутреннее ---------------------------------------------------------

    def _write(self, record: dict) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        # Файл на сутки: удобно и выгружать, и удалять по сроку хранения.
        target = self.path / f"{datetime.now(UTC):%Y-%m-%d}.jsonl"
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock, target.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _clean(self, text: str) -> str:
        return Masker().mask(text) if self.mask_personal_data else text

    def _pseudonym(self, channel: str, user_id: str) -> str:
        return hashlib.sha256(f"{SALT}:{channel}:{user_id}".encode()).hexdigest()[:16]

    def _describe(self, response: Response) -> dict:
        if isinstance(response, Message):
            return {"type": "text", "text": self._clean(response.text)}
        if isinstance(response, ProductCard):
            return {
                "type": "card",
                "sku": response.product.sku_1c,
                "name": response.product.name,
                "price": response.product.price,
                "norm": response.citation,
            }
        if isinstance(response, ProductList):
            return {
                # Заголовок выдачи цитирует запрос пользователя дословно, поэтому
                # его тоже надо обезличивать: иначе телефон, замаскированный во
                # входящем поле, возвращается в журнал через ответ бота.
                "type": "list",
                "title": self._clean(response.title),
                "total": response.total_found,
                "items": [
                    {
                        "sku": card.product.sku_1c,
                        "name": card.product.name,
                        "price": card.product.price,
                        "norm": card.citation,
                    }
                    for card in response.cards
                ],
            }
        if isinstance(response, OrderSummary):
            return {
                "type": "order",
                "total": response.total,
                "positions": len(response.lines),
            }
        return {"type": "unknown"}


def read_dialogs(path: Path, limit_sessions: int = 20) -> dict[str, list[dict]]:
    """Собирает записи в диалоги по сессиям — для просмотра и работы над промптами."""
    records: list[dict] = []
    for file in sorted(path.glob("*.jsonl")):
        with file.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))

    sessions: dict[str, list[dict]] = {}
    for record in records:
        sessions.setdefault(record["session"], []).append(record)
    latest = sorted(sessions.items(), key=lambda item: item[1][-1]["ts"], reverse=True)
    return dict(latest[:limit_sessions])
