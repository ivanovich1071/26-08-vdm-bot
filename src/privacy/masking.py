"""Маскирование персональных данных перед отправкой в языковую модель.

Модель хостится у Cloud.ru — это отдельное лицо, обрабатывающее данные. Проще и
надёжнее не передавать туда персональные данные вообще, чем оформлять и защищать
такую передачу. Заодно из логов и трассировок исчезают телефоны и почта.

Маска обратима в пределах одного диалога: ответ модели восстанавливается перед
показом пользователю, поэтому «перезвоните на [PHONE_1]» превращается обратно
в настоящий номер.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PHONE = re.compile(
    r"(?:\+7|8|7)?[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]?\d{2}[\s\-]?\d{2}\b"
)
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
# ИНН юрлица (10) и ИП (12). Стоит до телефона: 10 цифр подряд иначе съедает маска номера.
INN = re.compile(r"\b(?:ИНН[\s:]*)?(\d{12}|\d{10})\b(?=\s|$|[,.;])")
FIO = re.compile(
    r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ский|цкий|ко|ук|юк|ич|ова|ева|ина|ына|ская|цкая)\b"
    r"(?:\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|мич))?"
)

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", EMAIL),
    ("INN", INN),
    ("PHONE", PHONE),
    ("NAME", FIO),
)


@dataclass
class Masker:
    """Хранит соответствие маска -> исходное значение на время диалога."""

    values: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def mask(self, text: str) -> str:
        if not text:
            return text
        result = text
        for kind, pattern in _RULES:
            result = pattern.sub(lambda m, k=kind: self._placeholder(k, m.group(0)), result)
        return result

    def unmask(self, text: str) -> str:
        if not text:
            return text
        for placeholder, original in self.values.items():
            text = text.replace(placeholder, original)
        return text

    def _placeholder(self, kind: str, value: str) -> str:
        for placeholder, known in self.values.items():
            if known == value:
                return placeholder
        self._counters[kind] = self._counters.get(kind, 0) + 1
        placeholder = f"[{kind}_{self._counters[kind]}]"
        self.values[placeholder] = value
        return placeholder

    @property
    def is_clean(self) -> bool:
        return not self.values


def contains_personal_data(text: str) -> bool:
    """Проверка для автотестов и логов: в строке не должно остаться ПДн."""
    return any(pattern.search(text or "") for _, pattern in _RULES)
