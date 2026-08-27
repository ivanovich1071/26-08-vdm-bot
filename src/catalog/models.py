"""Доменные типы каталога.

Общие для поиска, агента, адаптеров и заказа: ядро и адаптеры обмениваются именно
этими объектами, а не сырыми строками выгрузки.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormRef:
    """Нормативное основание: документ и, если известен, пункт перечня."""

    doc_id: str
    doc_citation: str
    item_code: str | None
    item_title: str | None
    source: str
    confidence: float

    @property
    def citation(self) -> str:
        if self.item_code:
            return f"позиция {self.item_code} — {self.doc_citation}"
        return self.doc_citation

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> NormRef:
        return cls(
            doc_id=raw["doc_id"],
            doc_citation=raw["doc_citation"],
            item_code=raw.get("item_code"),
            item_title=raw.get("item_title"),
            source=raw.get("source", ""),
            confidence=float(raw.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class Product:
    sku_1c: str
    name: str
    url: str | None
    short_url: str | None
    price: int | None
    currency: str
    in_stock: int
    category_paths: list[list[str]]
    description: str
    kit_contents: list[str]
    norms: list[NormRef]
    bitrix_id: int | None
    images: list[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def roots(self) -> list[str]:
        return [path[0] for path in self.category_paths if path]

    @property
    def available(self) -> bool:
        return self.in_stock > 0

    @property
    def norm_codes(self) -> list[str]:
        return [ref.item_code for ref in self.norms if ref.item_code]

    def best_norm(self) -> NormRef | None:
        """Основание, которое стоит назвать в ответе: с пунктом и максимально надёжное."""
        if not self.norms:
            return None
        return max(self.norms, key=lambda ref: (ref.item_code is not None, ref.confidence))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Product:
        return cls(
            sku_1c=raw["sku_1c"],
            name=raw["name"],
            url=raw.get("url"),
            short_url=raw.get("short_url"),
            price=raw.get("price"),
            currency=raw.get("currency", "RUB"),
            in_stock=raw.get("in_stock", 0),
            category_paths=raw.get("category_paths", []),
            description=raw.get("description", ""),
            kit_contents=raw.get("kit_contents", []),
            norms=[NormRef.from_dict(n) for n in raw.get("norms", [])],
            bitrix_id=raw.get("bitrix_id"),
            images=raw.get("images", []),
            updated_at=raw.get("updated_at", ""),
        )
