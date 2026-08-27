"""Нормализация русского текста для поиска.

Полноценная морфология тут избыточна: запросы короткие («мячи для зала», «столы
регулируемые»), а каталог узкий. Хватает отсечения окончаний — оно склеивает
«мячи/мяч/мячей» и «столы/стол/столов», не притягивая посторонние слова.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_MIN_STEM = 3

# Порядок важен: длинные окончания проверяются раньше коротких.
_SUFFIXES = (
    "иями", "ями", "ами", "иях", "ыми", "ими", "ому", "ему", "ого", "его",
    "ях", "ах", "ов", "ев", "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие",
    "ом", "ем", "ам", "ям", "ую", "юю", "ей", "ии",
    "ь", "я", "ю", "у", "е", "о", "и", "ы", "а", "й",
)


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower().replace("ё", "е"))


def stem(token: str) -> str:
    """Отсечение окончания. Латиница и артикулы вроде `min95030` не трогаются."""
    if len(token) <= _MIN_STEM or not token[0].isalpha() or token.isascii():
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
            return token[: -len(suffix)]
    return token


def stems(text: str) -> list[str]:
    return [stem(token) for token in tokenize(text)]


def trigrams(text: str) -> set[str]:
    """Триграммы для устойчивости к опечаткам и слитному написанию."""
    padded = f"  {text.lower().replace('ё', 'е')} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}
