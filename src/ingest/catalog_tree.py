"""Восстановление дерева каталога из плоского прайса.

В выгрузке 1С иерархия задана строками, где заполнена только первая колонка. Уровень
такой строки определяется её типом, а не количеством точек: школьная ветка нумеруется
«Раздел N» / «Подраздел M» / «N.M.K.», садовая — «NN.» / «NN.NN» / «NN.NN.NN».
Если считать только точки, школьная ветка разваливается на два десятка псевдокорней.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_LEVEL = 4

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$")
_SECTION = re.compile(r"^Раздел\s+(\d+)\.?\s*(.*)$", re.IGNORECASE)
_SUBSECTION = re.compile(r"^Подраздел\s+(\d+)\.?\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Heading:
    title: str
    level: int
    code: str | None
    kind: str  # root | section | subsection | numbered | other


def classify(title: str) -> Heading:
    text = title.strip()

    if m := _SECTION.match(text):
        return Heading(text, 1, m.group(1), "section")
    if m := _SUBSECTION.match(text):
        return Heading(text, 2, m.group(1), "subsection")
    if m := _NUMBERED.match(text):
        code = m.group(1)
        level = min(len(code.split(".")), MAX_LEVEL)
        return Heading(text, level, code, "numbered")

    # Корневые разделы в выгрузке набраны прописными и не нумерованы.
    letters = [ch for ch in text if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        return Heading(text, 0, None, "root")
    return Heading(text, 1, None, "other")


@dataclass
class CatalogPath:
    """Текущий путь по дереву при последовательном проходе строк прайса."""

    levels: list[Heading | None] = field(default_factory=lambda: [None] * (MAX_LEVEL + 1))

    def push(self, title: str) -> Heading:
        heading = classify(title)
        if heading.kind == "root":
            self.levels = [None] * (MAX_LEVEL + 1)
        self.levels[heading.level] = heading
        for deeper in range(heading.level + 1, MAX_LEVEL + 1):
            self.levels[deeper] = None
        return heading

    @property
    def chain(self) -> list[Heading]:
        return [h for h in self.levels if h is not None]

    @property
    def titles(self) -> list[str]:
        return [h.title for h in self.chain]

    @property
    def root(self) -> str | None:
        return self.levels[0].title if self.levels[0] else None

    def deepest_numbered(self) -> Heading | None:
        """Самый глубокий нумерованный заголовок — кандидат на пункт нормативного перечня."""
        for heading in reversed(self.chain):
            if heading.kind == "numbered":
                return heading
        return None
