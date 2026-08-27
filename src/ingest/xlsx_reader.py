"""Чтение XLSX без внешних зависимостей.

Выгрузка из 1С приходит обычным .xlsx. Полноценный openpyxl тут не нужен: нам требуется
последовательный проход по строкам двух листов и текст ячеек. Стандартной библиотеки
(zipfile + ElementTree) для этого достаточно, зато загрузка прайса работает в любом
окружении, включая контейнер воркера без сборочных зависимостей.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

Row = dict[str, str]


def column_index(cell_ref: str) -> int:
    """`A1` -> 0, `B7` -> 1, `AA3` -> 26."""
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
    return index - 1


def column_name(index: int) -> str:
    """0 -> `A`, 26 -> `AA`."""
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


class XlsxFile:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path)
        self._shared: list[str] | None = None
        self._sheets = self._read_sheet_index()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> XlsxFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def sheet_names(self) -> list[str]:
        return list(self._sheets)

    def _read_sheet_index(self) -> dict[str, str]:
        rels: dict[str, str] = {}
        with self._zip.open("xl/_rels/workbook.xml.rels") as fh:
            for rel in ET.parse(fh).getroot():
                rels[rel.attrib["Id"]] = rel.attrib["Target"].lstrip("/")
        sheets: dict[str, str] = {}
        with self._zip.open("xl/workbook.xml") as fh:
            for sheet in ET.parse(fh).getroot().iter(f"{NS}sheet"):
                target = rels.get(sheet.attrib.get(f"{DOC_REL_NS}id", ""), "")
                if not target:
                    continue
                sheets[sheet.attrib["name"]] = target if target.startswith("xl/") else f"xl/{target}"
        return sheets

    @property
    def shared_strings(self) -> list[str]:
        if self._shared is not None:
            return self._shared
        shared: list[str] = []
        if "xl/sharedStrings.xml" in self._zip.namelist():
            with self._zip.open("xl/sharedStrings.xml") as fh:
                for _, elem in ET.iterparse(fh, events=("end",)):
                    if elem.tag != f"{NS}si":
                        continue
                    # Форматированный текст разложен на несколько <t>: склеиваем в исходном порядке.
                    shared.append("".join(t.text or "" for t in elem.iter(f"{NS}t")))
                    elem.clear()
        self._shared = shared
        return shared

    def rows(self, sheet: str | int = 0) -> Iterator[Row]:
        """Строки листа как {'A': значение, ...}. Пустые ячейки отсутствуют в словаре."""
        name = self.sheet_names[sheet] if isinstance(sheet, int) else sheet
        strings = self.shared_strings
        with self._zip.open(self._sheets[name]) as fh:
            for _, elem in ET.iterparse(fh, events=("end",)):
                if elem.tag != f"{NS}row":
                    continue
                row: Row = {}
                for pos, cell in enumerate(elem.iter(f"{NS}c")):
                    ref = cell.attrib.get("r")
                    col = column_name(column_index(ref)) if ref else column_name(pos)
                    value = _cell_value(cell, strings)
                    if value:
                        row[col] = value
                elem.clear()
                yield row


def _cell_value(cell: ET.Element, strings: list[str]) -> str:
    kind = cell.attrib.get("t", "n")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{NS}t")).strip()
    node = cell.find(f"{NS}v")
    if node is None or node.text is None:
        return ""
    raw = node.text
    if kind == "s":
        try:
            return strings[int(raw)].strip()
        except (ValueError, IndexError):
            return ""
    return raw.strip()
