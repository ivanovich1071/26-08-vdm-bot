"""Пункты перечней из текстов самих приказов.

Раньше бот знал только номер пункта — «позиция 2.4.35». Что за этим номером стоит,
не знал ни он, ни пользователь: чтобы это выяснить, надо было открыть приказ на
147 страницах и найти строку глазами. Теперь формулировка берётся из документа
дословно и показывается рядом с товаром.

Разбор отдельный для каждого приказа: у них разная вёрстка.

**838** — обычный список: «2.4.35. Дидактические пособия и обучающие игры…».
Номер стоит в начале строки, после него точка. Разделы и подразделы идут
заголовками, их запоминаем — по ним видно, что 2.4 это кабинет учителя-логопеда.

**1057** — таблица, из которой pdf вынимает текст построчно и с двумя видами
порчи. Номер иногда склеивается с названием («1.13.4.3.1.2Игровой комплект»),
а иногда, наоборот, разрывается пробелом («1.13.4.3.1.1 0» — это пункт
1.13.4.3.1.10). Из-за второго 482 наших пункта «не находились» в приказе, хотя
были в нём. Обе порчи чиним до разбора, иначе сверка врёт.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_ITEMS = Path("data/kb/norm_items.json")

# Единицы измерения из таблицы 1057. По ним отделяется название пункта от
# количества: название кончается там, где начинается единица.
_UNITS = ("Шт.", "Компл.", "Комплект", "Набор", "Пар", "Пара", "Ед.", "М2", "М.")

# Заголовки разделов в 838: «Раздел 2. Комплекс оснащения предметных кабинетов»,
# «Подраздел 4. Кабинет учителя-логопеда».
_HEADING = re.compile(r"^(Раздел|Подраздел)\s+(\d+)\.\s*(.+)$")

# Пункт перечня в 838: номер, точка, название.
_ITEM_838 = re.compile(r"^(\d+(?:\.\d+){1,4})\.\s+(\S.*)$")

# Пункт перечня в 1057: номер в начале строки, дальше название. Название может
# начаться и со следующей строки — после починки разорванного номера он часто
# остаётся на строке один.
_ITEM_1057 = re.compile(r"^(\d+(?:\.\d+){1,5})(?:\s+(\S.*))?$")

# Разорванный номер: «1.13.4.3.1.1 0 Комплект». Цифру после пробела возвращаем
# на место, но только если дальше начинается название, а не количество.
_SPLIT_CODE = re.compile(r"(\d(?:\.\d+){2,})\s(\d)(?=\s+[А-ЯЁA-Z«\"(])")

# Номер, склеенный с названием: «1.13.4.3.1.2Игровой».
_GLUED_CODE = re.compile(r"(\d(?:\.\d+){2,})(?=[А-ЯЁA-Z«\"(])")


@dataclass(frozen=True)
class NormItem:
    """Пункт перечня так, как он написан в приказе."""

    doc_id: str
    code: str
    title: str
    # Раздел и подраздел, в которых пункт стоит. В 838 по ним видно назначение
    # («Кабинет учителя-логопеда»), в 1057 — направление развития.
    section: str | None = None
    unit: str | None = None
    quantity: str | None = None

    @property
    def full_title(self) -> str:
        if self.section:
            return f"{self.title} ({self.section})"
        return self.title


def parse_838(text: str) -> list[NormItem]:
    items: list[NormItem] = []
    section = subsection = None

    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue

        heading = _HEADING.match(line)
        if heading:
            kind, number, title = heading.groups()
            if kind == "Раздел":
                section, subsection = title.strip(" .*"), None
            else:
                subsection = title.strip(" .*")
            continue

        match = _ITEM_838.match(line)
        if not match:
            continue
        code, title = match.groups()
        items.append(
            NormItem(
                doc_id="order_838",
                code=code,
                title=title.strip(" .*"),
                section=subsection or section,
            )
        )
    return items


def parse_1057(text: str) -> list[NormItem]:
    """Разбор таблицы. Название пункта переносится на несколько строк.

    Поэтому строки накапливаются до следующего номера, а потом из накопленного
    отделяются единица измерения и количество.
    """
    items: list[NormItem] = []
    code: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if code is None:
            return
        title, unit, quantity = _split_tail(" ".join(buffer))
        if title:
            items.append(
                NormItem(
                    doc_id="order_1057",
                    code=code,
                    title=title,
                    unit=unit,
                    quantity=quantity,
                )
            )

    for raw in _repaired(text).splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        match = _ITEM_1057.match(line)
        if match:
            flush()
            code, rest = match.groups()
            buffer = [rest] if rest else []
        elif code is not None:
            buffer.append(line)
    flush()

    # Один и тот же пункт встречается на нескольких страницах (шапка таблицы
    # повторяется). Оставляем первое вхождение — оно полное.
    unique: dict[str, NormItem] = {}
    for item in items:
        unique.setdefault(item.code, item)
    return list(unique.values())


def _repaired(text: str) -> str:
    """Чинит номера, испорченные вёрсткой таблицы."""
    previous = None
    while previous != text:
        previous = text
        text = _SPLIT_CODE.sub(r"\1\2", text)
    return _GLUED_CODE.sub(r"\1 ", text)


def _split_tail(text: str) -> tuple[str, str | None, str | None]:
    """Отделяет от накопленного хвост таблицы: единицу измерения и количество."""
    text = " ".join(text.split()).strip()
    for unit in _UNITS:
        position = text.find(f" {unit}")
        if position <= 0:
            continue
        title = text[:position].strip(" -–—")
        tail = text[position + len(unit) + 1 :].strip(" +")
        return title, unit, " ".join(tail.split()) or None
    return text.strip(" +"), None, None


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def build(sources: dict[str, Path], out: Path = DEFAULT_ITEMS) -> dict[str, int]:
    """Собирает справочник пунктов из PDF приказов.

    Приказы в git не хранятся (как и любые PDF заказчика), поэтому команда
    запускается вручную у того, у кого файлы лежат рядом с проектом.
    """
    parsers = {"order_838": parse_838, "order_1057": parse_1057}
    collected: dict[str, list[dict]] = {}

    for doc_id, path in sources.items():
        if doc_id not in parsers or not path.exists():
            continue
        items = parsers[doc_id](read_pdf(path))
        collected[doc_id] = [asdict(item) for item in items]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(collected, ensure_ascii=False, indent=1), encoding="utf-8")
    return {doc_id: len(items) for doc_id, items in collected.items()}


class ItemIndex:
    """Справочник пунктов с поиском по словам.

    Появился из-за того, что модель не могла найти пункт по смыслу. Она знала
    номера из разговора и достраивала остальное сама: «Спортивный комплекс
    соответствует пункту 2.20.63 приказа 1057», хотя 2.20.63 — это фрезерный
    станок из приказа 838, а спортивное оборудование в 1057 стоит под 1.5.1.
    Тексты приказов у нас разобраны давно, не хватало только способа спросить
    их словами.

    Поиск нарочно простой: совпадение основ слов, длинные пункты чуть ниже
    коротких. Это не полнотекстовый движок, это замена выдумыванию.
    """

    def __init__(self, items: dict[str, dict[str, NormItem]]) -> None:
        self.items = items
        self._tokens: dict[tuple[str, str], set[str]] = {}
        for doc_id, by_code in items.items():
            for code, item in by_code.items():
                self._tokens[(doc_id, code)] = _stems(f"{item.title} {item.section or ''}")

    @property
    def loaded(self) -> bool:
        return bool(self.items)

    def get(self, doc_id: str, code: str) -> NormItem | None:
        return self.items.get(doc_id, {}).get(code)

    def documents_with(self, code: str) -> list[str]:
        """В каких приказах есть пункт с таким номером."""
        return sorted(doc_id for doc_id, by_code in self.items.items() if code in by_code)

    def count(self, doc_id: str) -> int:
        return len(self.items.get(doc_id, {}))

    def search(self, text: str, doc_id: str | None = None, limit: int = 5) -> list[NormItem]:
        from catalog.text import expand

        wanted = _stems(text)
        if not wanted:
            return []
        # «Спортзал» в приказе называется «спортивным оборудованием», «мастерская» —
        # «кабинетом технологии». Раскрываем запрос теми же синонимами, что и в
        # каталоге, иначе поиск по смыслу молчит ровно там, где он нужен.
        wanted |= {token for token in expand(sorted(wanted)) if len(token) > 2}
        scored: list[tuple[float, NormItem]] = []
        for (item_doc, code), tokens in self._tokens.items():
            if doc_id and item_doc != doc_id:
                continue
            common = wanted & tokens
            if not common:
                continue
            # Доля запроса, которую пункт покрыл, минус наказание за многословие:
            # иначе абзац на сорок слов обгоняет точную формулировку из трёх.
            score = len(common) / len(wanted) - 0.01 * len(tokens)
            scored.append((score, self.items[item_doc][code]))
        scored.sort(key=lambda pair: (-pair[0], pair[1].code))
        return [item for _, item in scored[:limit]]


def _stems(text: str) -> set[str]:
    from catalog.text import stems

    return {token for token in stems(text) if len(token) > 2}


def load(path: Path = DEFAULT_ITEMS) -> dict[str, dict[str, NormItem]]:
    """Справочник в память: документ → номер пункта → пункт.

    Файла может не быть — приказы лежат не у всех. Тогда бот работает как раньше,
    называя номер пункта без формулировки.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    result: dict[str, dict[str, NormItem]] = {}
    for doc_id, items in raw.items():
        result[doc_id] = {
            item["code"]: NormItem(
                doc_id=doc_id,
                code=item["code"],
                title=item.get("title", ""),
                section=item.get("section"),
                unit=item.get("unit"),
                quantity=item.get("quantity"),
            )
            for item in items
        }
    return result
