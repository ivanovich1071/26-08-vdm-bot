"""Маскирование персональных данных перед отправкой в языковую модель.

Модель хостится у Cloud.ru — это отдельное лицо, обрабатывающее данные. Проще и
надёжнее не передавать туда персональные данные вообще, чем оформлять и защищать
такую передачу. Заодно из логов и трассировок исчезают телефоны и почта.

Маска обратима в пределах диалога: объект живёт вместе с сессией, поэтому
«перезвоните на [ТЕЛЕФОН_1]» превращается обратно в настоящий номер и на пятом
ходу разговора, а не только на первом.

Отдельная забота — не маскировать лишнего. Имя ищется по отчеству или по прямому
указанию («меня зовут», «контактное лицо»), а не по окончанию слова: иначе
«Дидактический набор» превращается в [ИМЯ_1] и в таком виде уходит пользователю.
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

_WORD = r"[А-ЯЁ][а-яё]+"
# Отчество — самый надёжный признак имени в русском тексте: слов с такими
# окончаниями вне ФИО практически нет.
_PATRONYMIC = r"[А-ЯЁ][а-яё]+(?:ович|евич|ьевич|овна|евна|ьевна|инична|ична)"
# Фамильные окончания. Одного такого слова мало: «Дидактический» тоже кончается
# на «ский», и в каталоге подобных прилагательных тысячи.
_SURNAME = r"[А-ЯЁ][а-яё]+(?:ов|ев|ёв|ин|ын|ский|цкий|ко|ук|юк|ич|ова|ева|ина|ына|ская|цкая)"
# 1. Фамилия Имя Отчество и Имя Отчество — распознаются сами по себе.
FIO_FULL = re.compile(rf"\b(?:{_WORD}\s+)?{_WORD}\s+{_PATRONYMIC}\b")
# 2. «Фамилия Имя» и «Имя Фамилия»: два заглавных слова подряд, одно с фамильным
#    окончанием. Название товара так почти не пишут — второе слово в нём строчное.
FIO_PAIR = re.compile(rf"\b(?:{_SURNAME}\s+{_WORD}|{_WORD}\s+{_SURNAME})\b")
# 3. Одиночное имя — только там, где на него прямо указали. Без такого указания
#    заглавное слово остаётся словом: в каталоге их тысячи.
_INTRO = (
    r"(?:меня\s+зовут|зовут|контактное\s+лицо|контакт|ответственн\w+|ФИО|"
    r"заведующ\w+|директор|завхоз|методист|бухгалтер|менеджер)"
)
FIO_NAMED = re.compile(rf"{_INTRO}[\s:—-]+({_WORD}(?:\s+{_WORD}){{0,2}})", re.IGNORECASE)

# Порядок важен: почта и ИНН содержат цифры, которые иначе съест маска телефона.
_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("ПОЧТА", EMAIL, 0),
    ("ИНН", INN, 0),
    ("ТЕЛЕФОН", PHONE, 0),
    ("ИМЯ", FIO_FULL, 0),
    ("ИМЯ", FIO_PAIR, 0),
    ("ИМЯ", FIO_NAMED, 1),
)

# Чем заменить метку, которую нечем раскрыть: история диалога хранится на диске
# уже замаскированной, и после перезапуска соответствия в памяти нет. Показывать
# пользователю «[ИМЯ_1]» нельзя ни при каких обстоятельствах.
_NEUTRAL = {
    "ИМЯ": "вы",
    "ТЕЛЕФОН": "ваш телефон",
    "ПОЧТА": "ваша почта",
    "ИНН": "ваш ИНН",
}
_PLACEHOLDER = re.compile(r"\[(ИМЯ|ТЕЛЕФОН|ПОЧТА|ИНН)_\d+\]")


@dataclass
class Masker:
    """Соответствие «метка → исходное значение» на время диалога.

    Живёт в сессии, а не в одном ходе: пользователь называет телефон один раз,
    а всплыть в разговоре он может через несколько сообщений.
    """

    values: dict[str, str] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    def mask(self, text: str) -> str:
        if not text:
            return text
        result = text
        for kind, pattern, group in _RULES:
            result = pattern.sub(
                lambda m, k=kind, g=group: self._replace(m, k, g), result
            )
        return result

    def unmask(self, text: str) -> str:
        """Вернуть настоящие значения, а нераскрытые метки заменить нейтральными.

        Метка в тексте, который видит пользователь, — это всегда наша ошибка,
        поэтому подстраховка тут не лишняя.
        """
        if not text:
            return text
        for placeholder, original in self.values.items():
            text = text.replace(placeholder, original)
        return _PLACEHOLDER.sub(lambda m: _NEUTRAL.get(m.group(1), "—"), text)

    def _replace(self, match: re.Match[str], kind: str, group: int) -> str:
        """Замена одного совпадения. Группа нужна, когда маскируется не весь матч.

        У правила «меня зовут Пётр» под маску идёт только имя: вводное слово —
        часть фразы пользователя, и без него ответ модели читается странно.
        """
        value = match.group(group) if group else match.group(0)
        placeholder = self._placeholder(kind, value)
        whole = match.group(0)
        return whole.replace(value, placeholder) if group else placeholder

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
    return any(pattern.search(text or "") for _, pattern, _ in _RULES)
