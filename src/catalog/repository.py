"""Загрузка каталога из собранной базы знаний."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from catalog.models import Product
from catalog.search import CatalogIndex

DEFAULT_KB = Path("data/kb/products.jsonl")


def load_products(path: str | Path = DEFAULT_KB) -> list[Product]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"База знаний не собрана: {path}. Запустите "
            "`python -m ingest.build_kb --source <выгрузка.xlsx>`."
        )
    with path.open(encoding="utf-8") as fh:
        return [Product.from_dict(json.loads(line)) for line in fh if line.strip()]


@lru_cache(maxsize=4)
def load_index(path: str | Path = DEFAULT_KB) -> CatalogIndex:
    """Индекс собирается один раз на процесс: построение занимает секунды."""
    return CatalogIndex(load_products(path))
