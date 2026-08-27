"""Сборка базы знаний из выгрузки 1С.

Вход — XLSX-выгрузка заказчика (обновляется 2–3 раза в месяц), выход — products.jsonl
и отчёт о покрытии. Загрузка идемпотентна: один и тот же файл даёт один и тот же результат.

    python -m ingest.build_kb --source data/raw/Pricelist20260826.xlsx
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ingest import norm_registry
from ingest.catalog_tree import CatalogPath
from ingest.html_text import html_to_text, split_kit_contents
from ingest.xlsx_reader import XlsxFile
from norms import documents as norm_docs
from norms.extract import SOURCE_WEIGHTS, NormLink, code_anomalies, extract, root_document_id

# Колонки листа с товарами. Заголовки проверяем при загрузке — если выгрузка изменится,
# лучше упасть с внятной ошибкой, чем молча собрать пустую базу.
EXPECTED_HEADERS = {
    "A": "Код в 1с8",
    "B": "Наименование",
    "C": "URL страницы детального просмотра",
    "D": "Розничная цена",
    "E": "Доступное количество",
    "F": "Короткая ссылка",
    "G": "Описание",
}

_SPACES = re.compile(r"\s+")


@dataclass
class Product:
    sku_1c: str
    name: str
    url: str | None
    short_url: str | None
    price: int | None
    currency: str
    in_stock: int
    # Один товар размещён сразу в нескольких разделах: например, мяч лежит и в спортивном
    # инвентаре детского сада, и в пункте приказа 838 по спортивному комплексу. Все
    # размещения нужны: по ним работают и навигация, и нормативная привязка.
    category_paths: list[list[str]]
    description: str
    kit_contents: list[str]
    norms: list[dict[str, Any]]
    bitrix_id: int | None
    images: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""


@dataclass
class Report:
    source_file: str
    generated_at: str
    rows_with_product: int = 0
    products: int = 0
    cross_listed: int = 0
    with_price: int = 0
    with_stock: int = 0
    with_description: int = 0
    with_kit_contents: int = 0
    with_bitrix_id: int = 0
    with_images: int = 0
    with_norms: int = 0
    with_norm_item_code: int = 0
    from_registry: int = 0
    headings: int = 0
    roots: dict[str, int] = field(default_factory=dict)
    norm_documents: dict[str, int] = field(default_factory=dict)
    norm_sources: dict[str, int] = field(default_factory=dict)
    norm_anomalies: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)


def build(source: Path, out_dir: Path) -> Report:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    report = Report(source_file=source.name, generated_at=now)

    with XlsxFile(source) as book:
        bitrix_ids = _read_bitrix_ids(book)
        products = list(_read_products(book, bitrix_ids, now, report))

    kb_file = out_dir / "products.jsonl"
    _apply_registry(products, report)
    _carry_over_images(products, kb_file)
    _fill_report(report, products)
    _write_jsonl(kb_file, products)
    (out_dir / "report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


# --- Чтение листов ---------------------------------------------------------------


def _read_products(
    book: XlsxFile,
    bitrix_ids: dict[str, int],
    now: str,
    report: Report,
) -> list[Product]:
    rows = book.rows(0)
    header = next(rows, {})
    _check_headers(header)

    path = CatalogPath()
    products: dict[str, Product] = {}
    unnumbered_roots: set[str] = set()

    for row in rows:
        code = row.get("A", "").strip()
        name = row.get("B", "").strip()

        if code and not name:
            heading = path.push(code)
            report.headings += 1
            if heading.kind == "numbered" and root_document_id(path.root) is None and path.root:
                unnumbered_roots.add(path.root)
            continue
        if not name:
            continue

        report.rows_with_product += 1
        text = html_to_text(row.get("G", ""))
        description, kit = split_kit_contents(text)
        url = row.get("C", "").strip() or None
        links = extract(path=path, url=url, description=text)
        key = code or f"{_normalize_name(name)}|{url or ''}"
        report.norm_anomalies.extend(f"{code}: {c}" for c in code_anomalies(path, links))

        existing = products.get(key)
        if existing is not None:
            _merge_placement(existing, path.titles, links)
            continue

        products[key] = Product(
            sku_1c=code,
            name=name,
            url=url,
            short_url=row.get("F", "").strip() or None,
            price=_to_int(row.get("D")),
            currency="RUB",
            in_stock=_to_int(row.get("E")) or 0,
            category_paths=[path.titles] if path.titles else [],
            description=description,
            kit_contents=kit,
            norms=[_link_to_dict(link) for link in links],
            bitrix_id=bitrix_ids.get(_normalize_name(name)),
            sources={"catalog": report.source_file},
            updated_at=now,
        )

    if unnumbered_roots:
        report.open_questions.append(
            "Нумерация разделов не подтверждена номером документа, привязка не проставлена: "
            + "; ".join(sorted(unnumbered_roots))
            + ". Уточнить у заказчика, какому перечню она соответствует."
        )
    return list(products.values())


def _merge_placement(
    product: Product,
    titles: list[str],
    links: list[NormLink],
) -> None:
    """Добавляет товару ещё одно размещение в каталоге и его нормативные основания."""
    if titles and titles not in product.category_paths:
        product.category_paths.append(titles)
    known = {(n["doc_id"], n["item_code"]): n for n in product.norms}
    for link in links:
        key = (link.doc_id, link.item_code)
        current = known.get(key)
        if current is None:
            product.norms.append(_link_to_dict(link))
            known[key] = product.norms[-1]
        elif link.confidence > current["confidence"]:
            current.update(_link_to_dict(link))
    product.norms.sort(key=lambda n: (-n["confidence"], n["doc_id"], n["item_code"] or ""))


def _read_bitrix_ids(book: XlsxFile) -> dict[str, int]:
    """Второй лист выгрузки: ID элемента Битрикса и его наименование.

    Ключ связи с сайтом. Неоднозначные названия отбрасываем — лучше пустой ID,
    чем ссылка на чужой товар.
    """
    if len(book.sheet_names) < 2:
        return {}
    by_name: dict[str, set[int]] = defaultdict(set)
    rows = book.rows(1)
    next(rows, None)
    for row in rows:
        raw_id, name = row.get("A", "").strip(), row.get("B", "").strip()
        if not raw_id.isdigit() or not name:
            continue
        by_name[_normalize_name(name)].add(int(raw_id))
    return {name: ids.pop() for name, ids in by_name.items() if len(ids) == 1}


def _check_headers(header: dict[str, str]) -> None:
    missing = {
        col: expected
        for col, expected in EXPECTED_HEADERS.items()
        if _normalize_name(header.get(col, "")) != _normalize_name(expected)
    }
    if missing:
        got = {col: header.get(col, "") for col in EXPECTED_HEADERS}
        raise ValueError(
            "Структура выгрузки изменилась. Ожидались колонки "
            f"{EXPECTED_HEADERS}, получены {got}. Загрузка остановлена."
        )


# --- Отчёт -----------------------------------------------------------------------


def _fill_report(report: Report, products: list[Product]) -> None:
    roots: Counter[str] = Counter()
    by_doc: Counter[str] = Counter()
    by_source: Counter[str] = Counter()

    for product in products:
        report.products += 1
        report.cross_listed += len(product.category_paths) > 1
        report.with_price += product.price is not None
        report.with_stock += product.in_stock > 0
        report.with_description += bool(product.description)
        report.with_kit_contents += bool(product.kit_contents)
        report.with_bitrix_id += product.bitrix_id is not None
        report.with_images += bool(product.images)
        if product.norms:
            report.with_norms += 1
        if any(norm["item_code"] for norm in product.norms):
            report.with_norm_item_code += 1
        for path in product.category_paths:
            if path:
                roots[path[0]] += 1
        for norm in product.norms:
            by_doc[norm["doc_id"]] += 1
            by_source[norm["source"]] += 1

    report.roots = dict(roots.most_common())
    report.norm_documents = dict(by_doc.most_common())
    report.norm_sources = dict(by_source.most_common())
    report.norm_anomalies = report.norm_anomalies[:50]
    if report.with_price < report.products:
        report.open_questions.append(
            f"Без цены {report.products - report.with_price} позиций. "
            "Что бот отвечает по ним: «цена по запросу» или скрывать из выдачи?"
        )


def _apply_registry(products: list[Product], report: Report) -> None:
    """Достраивает привязку к приказу 1057 по реестру заказчика.

    Реестр — его собственное решение, какой товар какой позиции перечня
    соответствует, поэтому он старше всего, что мы вывели сами: если пункт уже
    был найден по описанию или адресу страницы, запись реестра его заменяет.
    """
    mapping = norm_registry.load()
    if not mapping:
        return

    doc = norm_docs.get("order_1057")
    for product in products:
        entries = mapping.get(product.sku_1c)
        if not entries:
            continue
        known = {
            norm["item_code"]
            for norm in product.norms
            if norm["doc_id"] == "order_1057" and norm["item_code"]
        }
        for entry in entries:
            if entry["item_code"] in known:
                continue
            product.norms.append(
                {
                    "doc_id": "order_1057",
                    "doc_citation": doc.citation,
                    "item_code": entry["item_code"],
                    "item_title": entry["item_title"],
                    "source": "registry",
                    "confidence": SOURCE_WEIGHTS["registry"],
                }
            )
        report.from_registry += 1

    # Привязка без номера пункта теряет смысл, когда точный пункт уже известен.
    for product in products:
        if any(n["source"] == "registry" for n in product.norms):
            product.norms = [
                n
                for n in product.norms
                if n["item_code"] or n["doc_id"] != "order_1057"
            ]


def _carry_over_images(products: list[Product], previous: Path) -> None:
    """Сохраняет фотографии, собранные до этой пересборки.

    Выгрузка приходит два-три раза в месяц, а фотографии берутся с сайта отдельным
    проходом. Без переноса каждая новая выгрузка обнуляла бы собранное, и обход
    сайта пришлось бы начинать заново.
    """
    if not previous.exists():
        return

    known: dict[str, list[str]] = {}
    with previous.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("images"):
                known[row["sku_1c"]] = row["images"]

    for product in products:
        if not product.images:
            product.images = known.get(product.sku_1c, [])


def _write_jsonl(path: Path, products: list[Product]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for product in products:
            fh.write(json.dumps(asdict(product), ensure_ascii=False) + "\n")


# --- Мелочи ----------------------------------------------------------------------


def _link_to_dict(link: NormLink) -> dict[str, Any]:
    doc = norm_docs.get(link.doc_id)
    return {
        "doc_id": link.doc_id,
        "doc_citation": doc.citation,
        "item_code": link.item_code,
        "item_title": link.item_title,
        "source": link.source,
        "confidence": link.confidence,
    }


def _normalize_name(name: str) -> str:
    return _SPACES.sub(" ", name.replace("\xa0", " ")).strip().casefold()


def _to_int(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(round(float(raw.replace(",", ".").replace("\xa0", "").replace(" ", ""))))
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка базы знаний из выгрузки 1С")
    parser.add_argument("--source", default="data/raw/Pricelist20260826.xlsx")
    parser.add_argument("--out", default="data/kb")
    args = parser.parse_args()

    report = build(Path(args.source), Path(args.out))
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
