"""Нормативная привязка товара.

Заказчик продаёт не «игрушки», а исполнение приказов Минпросвещения: покупатель ищет
позицию перечня, а не товар. Привязка собирается из четырёх независимых источников,
чтобы одно расхождение не превращалось в выдуманную ссылку на закон.

Правило, которое соблюдает и загрузка, и агент: ссылка на документ появляется только
там, где документ назван в самих данных. Ничего не достраиваем по смыслу.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ingest.catalog_tree import CatalogPath, Heading
from norms import documents as docs

# Источники привязки, от самого надёжного к самому слабому.
SOURCE_WEIGHTS = {
    "registry": 0.98,  # реестр заказчика «пункт приказа → код 1С»
    "description": 0.95,  # документ назван в описании товара дословно
    "heading": 0.90,  # строка-заголовок прайса = пункт перечня
    "url_slug": 0.85,  # номер пункта в адресе страницы товара
    "root_section": 0.70,  # корневой раздел каталога объявляет документ
    "mention": 0.60,  # упомянут без номера пункта (ФГОС ДО, ФОП ДО)
}


@dataclass(frozen=True)
class NormLink:
    doc_id: str
    item_code: str | None
    item_title: str | None
    source: str

    @property
    def confidence(self) -> float:
        return SOURCE_WEIGHTS.get(self.source, 0.5)

    @property
    def citation(self) -> str:
        doc = docs.get(self.doc_id)
        if self.item_code:
            return f"позиция {self.item_code} — {doc.citation}"
        return doc.citation


# --- Распознавание документа в свободном тексте ---------------------------------

# Номер документа называют как угодно: «приказ № 838», «838 приказ», «закон 1057»,
# «по 1057-му приказу». Порядок слов свободный, поэтому смотрим обе стороны от
# числа. Голое число документом не считаем: в разговоре о закупке четырёхзначных
# чисел хватает и без номеров приказов.
_DOC_WORD = r"приказ\w*|закон\w*|постановлени\w*|распоряжени\w*|перечн\w*|№|N"
_RE_1057 = re.compile(
    rf"(?:{_DOC_WORD})\s*\.?\s*1057\b"
    rf"|\b1057\s*(?:-?\s*(?:м|му|го|й))?\s*(?:{_DOC_WORD})"
    r"|от\s*25\.12\.2024",
    re.IGNORECASE,
)
_RE_838 = re.compile(
    rf"(?:{_DOC_WORD})\s*\.?\s*838\b"
    rf"|\b838\s*(?:-?\s*(?:м|му|го|й))?\s*(?:{_DOC_WORD})"
    r"|от\s*28\.11\.2024",
    re.IGNORECASE,
)
_RE_FGOS = re.compile(r"\bФГОС\s*ДО\b", re.IGNORECASE)
_RE_FOP = re.compile(r"\bФОП\s*ДО\b", re.IGNORECASE)
_RE_FUNC_KITS = re.compile(r"перечн\w*\s+функциональных\s+комплектов", re.IGNORECASE)

# Номер пункта в slug адреса: .../2_20_63_frezerno_gravirovalnyy.../
_RE_SLUG_CODE = re.compile(r"/(\d+(?:_\d+){1,3})_[a-z0-9_]+")

# Номер пункта в свободном тексте: «п. 2.1.14», «пункт 2.11.3».
#
# Глубина — до шести чисел. Пункты 1057 уходят так далеко («1.13.4.3.1.6»), и в
# реестре заказчика их большинство. Пока предел стоял на четырёх, номер резался
# пополам: «покажи 1.13.4.3.1.6» превращалось в «1.13.4.3» и фантомный «1.6», и
# человек получал соседний пункт вместо того, который назвал.
_RE_TEXT_CODE = re.compile(r"\b(?:п\.?|пункт)\s*(\d+(?:\.\d+){1,5})\b", re.IGNORECASE)


def document_ids_in_text(text: str) -> list[str]:
    """Какие документы названы в тексте. Порядок — от конкретного к общему."""
    if not text:
        return []
    found: list[str] = []
    if _RE_1057.search(text):
        found.append(docs.ORDER_1057.id)
    if _RE_838.search(text):
        found.append(docs.ORDER_838.id)
    if _RE_FUNC_KITS.search(text):
        found.append(docs.FUNC_KITS.id)
    if _RE_FGOS.search(text):
        found.append(docs.FGOS_DO.id)
    if _RE_FOP.search(text):
        found.append(docs.FOP_DO.id)
    return found


def root_document_id(root_title: str | None) -> str | None:
    """Документ, объявленный корневым разделом каталога.

    Только для школьной ветки: её название прямо содержит номер приказа. Нумерация
    раздела «ОСНАЩЕНИЕ НОВОСТРОЕК» на приказ похожа, но в данных не подтверждена —
    догадку в нормативную привязку не пишем, она уходит в отчёт загрузки.
    """
    if not root_title:
        return None
    return docs.ORDER_838.id if _RE_838.search(root_title) else None


def normalize_code(code: str) -> str:
    return code.replace("_", ".").strip(". ")


def code_from_url(url: str | None) -> str | None:
    if not url:
        return None
    matches = _RE_SLUG_CODE.findall(url)
    return normalize_code(matches[-1]) if matches else None


def codes_in_query(text: str) -> list[str]:
    """Номера пунктов, названные пользователем: «2.1.14», «п. 2.11.3»."""
    if not text:
        return []
    found = [normalize_code(c) for c in _RE_TEXT_CODE.findall(text)]
    for token in re.findall(r"\b\d+(?:\.\d+){1,5}\b", text):
        code = normalize_code(token)
        if code not in found:
            found.append(code)
    return found


# --- Сборка привязки товара -----------------------------------------------------


def extract(
    *,
    path: CatalogPath,
    url: str | None,
    description: str | None,
) -> list[NormLink]:
    """Нормативные привязки одного товара, отсортированные по убыванию надёжности."""
    links: dict[tuple[str, str | None], NormLink] = {}

    def add(link: NormLink) -> None:
        key = (link.doc_id, link.item_code)
        current = links.get(key)
        if current is None or link.confidence > current.confidence:
            links[key] = link

    root_doc = root_document_id(path.root)
    heading = path.deepest_numbered()

    # 1. Строка-заголовок прайса — прямой номер пункта перечня.
    if root_doc and heading is not None and heading.code:
        add(
            NormLink(
                doc_id=root_doc,
                item_code=normalize_code(heading.code),
                item_title=_heading_title(heading),
                source="heading",
            )
        )

    # 2. Номер пункта в адресе страницы товара — независимая проверка заголовка.
    #    Только если адрес ведёт в ветку самого перечня: один и тот же товар лежит
    #    в нескольких разделах каталога, и ссылка нередко указывает на другой раздел,
    #    где то же число означает обычную категорию, а не пункт приказа.
    if root_doc and _url_in_branch(url, root_doc):
        slug_code = code_from_url(url)
        if slug_code:
            add(
                NormLink(
                    doc_id=root_doc,
                    item_code=slug_code,
                    item_title=_heading_title(heading) if heading else None,
                    source="url_slug",
                )
            )

    # 3. Документ, названный в описании товара.
    for doc_id in document_ids_in_text(description or ""):
        code = None
        if doc_id == root_doc and heading is not None and heading.code:
            code = normalize_code(heading.code)
        source = "description" if doc_id in {docs.ORDER_1057.id, docs.ORDER_838.id} else "mention"
        add(NormLink(doc_id=doc_id, item_code=code, item_title=None, source=source))

    # 4. Документ, объявленный корневым разделом, — на случай товара без описания.
    if root_doc and not any(link.doc_id == root_doc for link in links.values()):
        add(NormLink(doc_id=root_doc, item_code=None, item_title=None, source="root_section"))

    # 5. ФГОС ДО / ФОП ДО могут быть названы только в заголовке подраздела.
    for title in path.titles:
        for doc_id in document_ids_in_text(title):
            if doc_id in {docs.FGOS_DO.id, docs.FOP_DO.id}:
                add(NormLink(doc_id=doc_id, item_code=None, item_title=title, source="mention"))

    return sorted(
        links.values(),
        key=lambda link: (-link.confidence, link.doc_id, link.item_code or ""),
    )


def code_anomalies(path: CatalogPath, links: list[NormLink]) -> list[str]:
    """Пункт перечня, лежащий не в своём разделе.

    Разные номера пунктов у одного товара — норма: он вправе закрывать несколько
    позиций перечня. А вот пункт 2.3.10 внутри «Раздел 2 → Подраздел 1» — это уже
    несостыковка самой выгрузки, и она должна попасть в отчёт, а не в базу молча.
    """
    section = next((h for h in path.chain if h.kind == "section"), None)
    subsection = next((h for h in path.chain if h.kind == "subsection"), None)
    if section is None or subsection is None:
        return []
    expected = f"{section.code}.{subsection.code}"
    return [
        f"пункт {link.item_code} в разделе {expected}"
        for link in links
        if link.source == "heading"
        and link.item_code
        and not link.item_code.startswith(f"{expected}.")
    ]


def _url_in_branch(url: str | None, doc_id: str) -> bool:
    marker = docs.get(doc_id).url_marker
    if marker is None:
        return False
    return bool(url) and marker in url.lower()


def _heading_title(heading: Heading | None) -> str | None:
    if heading is None:
        return None
    text = heading.title
    if heading.code and text.startswith(heading.code):
        text = text[len(heading.code) :]
    return text.lstrip(". ").strip() or None
