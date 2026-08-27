"""Реестр соответствия «пункт приказа 1057 → код 1С».

Заказчик ведёт это соответствие сам: на нём построен его прежний бот «Иван».
Файл приходит выгрузкой в PDF, две колонки — наименование позиции перечня
с номером пункта и код товара в 1С.

Это самый надёжный источник нормативной привязки из всех, что у нас есть:
не догадка по описанию и не разбор адреса страницы, а решение заказчика,
какой товар какой позиции перечня соответствует. Поэтому у источника `registry`
вес выше остальных.

Разбор устроен так, чтобы молчаливая ошибка была невозможна: каждый код
сверяется с выгрузкой 1С, и всё, чего в ней нет, попадает в отчёт отдельной
строкой, а не растворяется.

    python run.py norms --source Baza-Ivan-25-11-25.pdf
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REGISTRY = Path("data/kb/norms_1057.json")

# Строка реестра: номер пункта, наименование по перечню, код 1С в конце.
# Наименование в исходном PDF обрезано по ширине колонки и может слипаться
# с кодом («…музыкальный инструм35386») — поэтому код ищем с конца строки,
# не рассчитывая на пробел перед ним.
_LINE = re.compile(r"^(\d+(?:\.\d+)+)\s+(.*)$")
_CODE_TAIL = re.compile(r"(0[ЭЮ]-\d{4,}|\d{3,5})\s*$")


@dataclass
class RegistryEntry:
    item_code: str
    item_title: str
    sku_1c: str


@dataclass
class RegistryReport:
    source_file: str = ""
    lines_with_code: int = 0
    lines_without_code: int = 0
    item_codes: int = 0
    sku_codes: int = 0
    matched: int = 0
    unmatched: list[str] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        return self.matched / self.lines_with_code if self.lines_with_code else 0.0


def parse_pdf(path: Path) -> list[RegistryEntry]:
    """Читает реестр из PDF. Требует pypdf — он нужен только для этого шага."""
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover — зависит от окружения
        raise RuntimeError(
            "Для разбора реестра нужен pypdf: pip install pypdf"
        ) from exc

    reader = pypdf.PdfReader(str(path))
    lines: list[str] = []
    for page in reader.pages:
        # Режим layout сохраняет колонки: в обычном режиме наименование и код
        # склеиваются так, что код теряет часть цифр.
        lines += (page.extract_text(extraction_mode="layout") or "").splitlines()
    return parse_lines(lines)


def parse_lines(lines: list[str]) -> list[RegistryEntry]:
    entries: list[RegistryEntry] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        row = _LINE.match(line)
        if not row:
            continue
        item_code, rest = row.group(1), row.group(2).strip()
        tail = _CODE_TAIL.search(rest)
        if not tail:
            # Позиция перечня без товара: заказчик её не закрывает.
            continue
        entries.append(
            RegistryEntry(
                item_code=item_code,
                item_title=rest[: tail.start()].strip(),
                sku_1c=tail.group(1),
            )
        )
    return entries


def build(source: Path, known_skus: set[str], out: Path = DEFAULT_REGISTRY) -> RegistryReport:
    """Разбирает реестр и сохраняет соответствие рядом с базой знаний."""
    entries = parse_pdf(source)
    report = RegistryReport(source_file=source.name, lines_with_code=len(entries))

    mapping: dict[str, list[dict[str, str]]] = {}
    unmatched: list[str] = []
    for entry in entries:
        if entry.sku_1c not in known_skus:
            unmatched.append(f"{entry.item_code} · {entry.item_title} · код {entry.sku_1c}")
            continue
        links = mapping.setdefault(entry.sku_1c, [])
        if all(link["item_code"] != entry.item_code for link in links):
            links.append({"item_code": entry.item_code, "item_title": entry.item_title})
        report.matched += 1

    report.item_codes = len({e.item_code for e in entries})
    report.sku_codes = len({e.sku_1c for e in entries})
    report.unmatched = unmatched

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"doc_id": "order_1057", "source": source.name, "products": mapping},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    return report


def load(path: Path = DEFAULT_REGISTRY) -> dict[str, list[dict[str, str]]]:
    """Соответствие для сборки базы знаний. Нет файла — работаем без него."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("products", {})
