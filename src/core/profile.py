"""Профиль разговора: что бот уже знает о задаче.

Отдельно от истории и по белому списку полей. История — это переписка, её
приходится обрезать и маскировать; профиль — короткая выжимка, которая целиком
уходит в системный промпт под заголовком «что уже известно». Именно из-за её
отсутствия бот переспрашивал возраст детей, который ему назвали ходом раньше.

**Персональных данных здесь нет по построению.** Имя, телефон, почта, организация
и адрес в профиль не принимаются: поля перечислены явно, и все они описывают
задачу — учреждение, зону, норматив, возраст, бюджет, срок, — а не человека.
Поэтому профиль можно хранить на диске и целиком показывать модели.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from norms.extract import document_ids_in_text

MAX_REMEMBERED_SKUS = 20

# --- Распознавание задачи в свободной речи -------------------------------------
#
# Пользователь описывает задачу словами, а не заполняет анкету. Разбор
# детерминированный: лишний вызов модели ради «это детский сад» стоит секунд
# сорок и легко ошибается, а перечисленные обороты покрывают почти всё, чем
# заказчику пишут в первых двух сообщениях.

_INSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("детский сад", r"детск\w*\s+сад\w*|\bдоу\b|\bдетсад\w*|\bясл\w+|дошкольн\w+"),
    ("школа", r"\bшкол\w+|\bгимнази\w+|\bлице\w+|\bсош\b|\bмбоу\b|начальн\w+\s+класс"),
    ("колледж", r"\bколледж\w*|\bтехникум\w*|\bспо\b"),
    ("центр развития", r"центр\w*\s+развити\w+|развивающ\w+\s+центр"),
)

_ROOMS: tuple[tuple[str, str], ...] = (
    ("спортивный зал", r"спорт\w*\s*зал\w*|спортивн\w+\s+зал\w*|физкультурн\w+\s+зал\w*"),
    ("музыкальный зал", r"музыкальн\w+\s+зал\w*|актов\w+\s+зал\w*"),
    ("кабинет логопеда", r"логопед\w*"),
    ("кабинет психолога", r"психолог\w*|сенсорн\w+\s+комнат\w*"),
    ("кабинет физики", r"кабинет\w*\s+физик\w*|физик\w*\s+кабинет"),
    ("кабинет химии", r"кабинет\w*\s+хими\w*|хими\w*\s+кабинет"),
    ("кабинет биологии", r"кабинет\w*\s+биологи\w*"),
    ("кабинет технологии", r"кабинет\w*\s+технологи\w*|мастерск\w+"),
    ("групповая комната", r"группов\w+\s+(?:комнат\w*|ячейк\w*)|\bв\s+групп\w+"),
    ("столовая", r"столов\w+|пищеблок\w*"),
    ("медицинский кабинет", r"медицинск\w+\s+кабинет|\bмедкабинет\w*|\bмедблок\w*"),
    ("библиотека", r"библиотек\w+"),
    ("игровая площадка", r"\bплощадк\w+|улич\w+\s+оборудован\w*"),
)

_AGE_RANGE = re.compile(r"\b(\d)\s*[-–—]\s*(\d{1,2})\s*лет", re.IGNORECASE)
_AGE_GROUPS: tuple[tuple[str, str], ...] = (
    ("младшая группа", r"младш\w+\s+групп\w*|ясельн\w+"),
    ("средняя группа", r"средн\w+\s+групп\w*"),
    ("старшая группа", r"старш\w+\s+групп\w*|подготовительн\w+\s+групп\w*"),
    ("начальная школа", r"начальн\w+\s+(?:школ\w*|класс\w*)|1\s*[-–]\s*4\s*класс"),
    ("средняя школа", r"5\s*[-–]\s*9\s*класс|средн\w+\s+звен\w+"),
    ("старшая школа", r"10\s*[-–]\s*11\s*класс|старш\w+\s+класс"),
)

# «бюджет 200 тысяч», «до 500 тыс», «выделили 1,5 млн»
_BUDGET = re.compile(
    r"(?:бюджет\w*|уложить\w*|выделен\w*|выделил\w*|есть|до|не\s+больше|в\s+пределах)"
    r"\D{0,12}?(\d[\d\s.,]*)\s*(млн|миллион\w*|тыс\w*|т\.?\s*р\.?)?\s*(?:руб\w*|₽|р\.)?",
    re.IGNORECASE,
)
_DEADLINES: tuple[tuple[str, str], ...] = (
    ("к 1 сентября", r"к\s*1\s*сентябр\w*|\bк\s+учебн\w+\s+год\w*|\bк\s+сентябр\w+"),
    ("к концу учебного года", r"конц\w+\s+учебн\w+\s+год\w*"),
    ("до конца года", r"до\s+конц\w+\s+год\w*|\bв\s+этом\s+году"),
    ("в этом квартале", r"\bквартал\w*"),
    ("срочно", r"\bсрочн\w*|как\s+можно\s+быстрее|\bгорит\b"),
)
_REGION = re.compile(
    r"(?:город|г\.|регион|область|край|доставк\w+\s+в)\s+([А-ЯЁ][а-яё-]{2,})", re.IGNORECASE
)
# Явный отказ от предложенного: «это не подходит», «дорого», «не то».
_REJECTION = re.compile(
    r"не\s+подход\w+|\bне\s+то\b|\bдорог\w+|\bдешевле\b|не\s+нужн\w+", re.IGNORECASE
)


@dataclass
class DialogProfile:
    """Всё, что бот выяснил о задаче. Ни одного поля о человеке."""

    institution: str | None = None
    room: str | None = None
    age: str | None = None
    norm_doc_ids: list[str] = field(default_factory=list)
    budget: str | None = None
    deadline: str | None = None
    region: str | None = None
    # Коды 1С, уже показанные пользователю, и те, что он отклонил. Нужны, чтобы
    # бот не предлагал по кругу одно и то же.
    offered: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.institution,
                self.room,
                self.age,
                self.norm_doc_ids,
                self.budget,
                self.deadline,
                self.region,
                self.offered,
            )
        )

    def update_from_text(self, text: str) -> list[str]:
        """Разбор реплики пользователя. Возвращает названия изменившихся полей.

        Уже известное не перезаписывается вслепую: пользователь уточняет задачу,
        а не начинает её заново. Тип учреждения фиксируется один раз — он в
        разговоре не меняется, а вот зона, возраст и бюджет уточняются.
        """
        changed: list[str] = []
        low = (text or "").lower()
        if not low:
            return changed

        if self.institution is None:
            self.institution = _first_match(low, _INSTITUTIONS)
            if self.institution:
                changed.append("institution")

        room = _first_match(low, _ROOMS)
        if room and room != self.room:
            self.room = room
            changed.append("room")

        age = _age(low)
        if age and age != self.age:
            self.age = age
            changed.append("age")

        for doc_id in document_ids_in_text(text):
            if doc_id not in self.norm_doc_ids:
                self.norm_doc_ids.append(doc_id)
                changed.append("norm")

        budget = _budget(low)
        if budget and budget != self.budget:
            self.budget = budget
            changed.append("budget")

        deadline = _first_match(low, _DEADLINES)
        if deadline and deadline != self.deadline:
            self.deadline = deadline
            changed.append("deadline")

        region = _REGION.search(text or "")
        if region and region.group(1).capitalize() != self.region:
            self.region = region.group(1).capitalize()
            changed.append("region")

        if _REJECTION.search(low) and self.offered:
            # Отклонили то, что показали последним: конкретную позицию пользователь
            # называет редко, а «дорого» почти всегда относится к последней выдаче.
            for sku in self.offered[-3:]:
                if sku not in self.rejected:
                    self.rejected.append(sku)
            changed.append("rejected")
        return changed

    def remember_offered(self, skus: list[str]) -> None:
        for sku in skus:
            if sku not in self.offered:
                self.offered.append(sku)
        if len(self.offered) > MAX_REMEMBERED_SKUS:
            del self.offered[:-MAX_REMEMBERED_SKUS]

    def as_prompt(self) -> str:
        """Профиль в виде, который читает модель.

        Пустой профиль даёт пустую строку: заголовок «что уже известно» без
        содержимого сбивает модель сильнее, чем его отсутствие.
        """
        if self.is_empty:
            return ""
        lines = ["## Что уже известно о задаче", ""]
        for label, value in (
            ("Учреждение", self.institution),
            ("Зона или кабинет", self.room),
            ("Возраст детей", self.age),
            ("Бюджет", self.budget),
            ("Срок", self.deadline),
            ("Регион", self.region),
        ):
            if value:
                lines.append(f"- {label}: {value}")
        if self.norm_doc_ids:
            lines.append(f"- Норматив: {', '.join(_doc_names(self.norm_doc_ids))}")
        if self.offered:
            lines.append(f"- Уже показано позиций: {len(self.offered)}")
        if self.rejected:
            lines.append(
                f"- Отклонено пользователем, повторно не предлагать: {len(self.rejected)}"
            )
        lines += ["", "Это уже сказано пользователем. Переспрашивать перечисленное запрещено."]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution": self.institution,
            "room": self.room,
            "age": self.age,
            "norm_doc_ids": self.norm_doc_ids,
            "budget": self.budget,
            "deadline": self.deadline,
            "region": self.region,
            "offered": self.offered,
            "rejected": self.rejected,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> DialogProfile:
        """Чтение с диска по белому списку.

        Лишние ключи отбрасываются молча: файл состояния переживает обновления
        бота, а неизвестное поле не должно ронять диалог.
        """
        known = set(cls().to_dict())
        return cls(**{key: value for key, value in (raw or {}).items() if key in known})


def _first_match(low: str, rules: tuple[tuple[str, str], ...]) -> str | None:
    for label, pattern in rules:
        if re.search(pattern, low, re.IGNORECASE):
            return label
    return None


def _age(low: str) -> str | None:
    match = _AGE_RANGE.search(low)
    if match:
        return f"{match.group(1)}–{match.group(2)} лет"
    return _first_match(low, _AGE_GROUPS)


def _budget(low: str) -> str | None:
    match = _BUDGET.search(low)
    if match is None:
        return None
    raw = match.group(1).replace(" ", "").replace(",", ".").rstrip(".")
    try:
        amount = float(raw)
    except ValueError:
        return None
    unit = (match.group(2) or "").lower().replace(" ", "")
    if unit.startswith(("млн", "миллион")):
        amount *= 1_000_000
    elif unit.startswith(("тыс", "т.р", "тр")):
        amount *= 1_000
    # Меньше десяти тысяч на оснащение кабинета не бывает — почти наверняка
    # под маску попало число из другого предложения: возраст, класс, количество.
    if amount < 10_000:
        return None
    return f"до {int(amount):,} ₽".replace(",", " ")


def _doc_names(doc_ids: list[str]) -> list[str]:
    from norms import documents as docs

    names = []
    for doc_id in doc_ids:
        if doc_id in docs.DOCUMENTS:
            names.append(docs.get(doc_id).short_name)
    return names
