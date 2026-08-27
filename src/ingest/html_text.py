"""Описания в выгрузке 1С приходят разметкой Битрикса, а не текстом.

Агенту и полнотекстовому поиску нужен чистый текст, поэтому разметку разбираем здесь
один раз при загрузке, а не на каждом запросе.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "section", "blockquote",
}
DROP_TAGS = {"script", "style", "noscript"}

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")

# Заголовок состава комплекта заказчик пишет по-разному: «Состав комплекта:», «Состав:»,
# «В комплект входит».
_KIT_HEADER = re.compile(
    r"^\s*(состав(?:\s+комплекта|\s+набора)?\s*:?|в\s+комплект\s+вход\w+\s*:?)\s*$",
    re.IGNORECASE,
)

# Тот же заголовок, но посреди строки: «Материал: пластик. Состав набора: 90 колец…».
_KIT_INLINE = re.compile(
    r"(состав(?:\s+комплекта|\s+набора)?|в\s+комплект\s+вход\w+)\s*:\s*(?=\S)",
    re.IGNORECASE,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_TAGS:
            self._skip_depth += 1
            return
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        # У <li> перевод строки уже добавлен открывающим тегом. Второй превращает
        # список в текст через пустую строку и разрывает блок состава комплекта.
        if tag in BLOCK_TAGS and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    """Разметка -> читаемый текст с сохранением списков и абзацев."""
    if not raw:
        return ""
    if "<" not in raw:
        return _normalize(unescape(raw))
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # Битая разметка встречается: не роняем загрузку всего прайса из-за одного товара.
        return _normalize(unescape(re.sub(r"<[^>]*>", " ", raw)))
    return _normalize("".join(parser.parts))


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return _BLANKS.sub("\n\n", text).strip()


def split_kit_contents(text: str) -> tuple[str, list[str]]:
    """Отделяет состав комплекта от описания.

    Возвращает (описание без блока состава, позиции состава). Если блок не найден,
    описание возвращается как есть, список пустой.
    """
    if not text:
        return "", []
    lines = text.split("\n")
    start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip("• ").strip()
        if _KIT_HEADER.match(stripped):
            start = i
            break
        # Заголовок часто стоит посреди строки, а состав идёт следом через «;».
        m = _KIT_INLINE.search(stripped)
        if m and len(stripped) - m.end() > 3:
            head = stripped[: m.start()].rstrip()
            tail = stripped[m.end() :]
            kept = [*lines[:i], head] if head else lines[:i]
            items = tail.split(";") if ";" in tail else [tail]
            return "\n".join(kept).strip(), _clean_items(items)
    if start is None:
        return text, []

    items: list[str] = []
    rest = lines[start + 1 :]
    for i, line in enumerate(rest):
        stripped = line.strip()
        if not stripped:
            # Пустая строка заканчивает состав, только если дальше идёт обычный текст,
            # а не следующий пункт: разметка заказчика неровная.
            following = next((s for s in (r.strip() for r in rest[i + 1 :]) if s), "")
            if items and not following.startswith("•"):
                break
            continue
        if stripped.startswith("•"):
            items.append(stripped.lstrip("• ").strip())
        elif items:
            # Продолжение последнего пункта после переноса строки.
            items[-1] = f"{items[-1]} {stripped}".strip()
        else:
            items.append(stripped)
    return "\n".join(lines[:start]).strip(), _clean_items(items)


def _clean_items(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        item = item.strip(" ;.")
        if item and item not in seen:
            seen.append(item)
    return seen
