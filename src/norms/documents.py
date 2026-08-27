"""Реестр нормативных документов, на которые опирается продажа.

Здесь только те документы, которые реально названы в данных заказчика — на сайте
или в описаниях выгрузки. Формулировки взяты оттуда же, чтобы бот цитировал основание
дословно, а не пересказывал по памяти.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormDocument:
    id: str
    short_name: str
    full_name: str | None
    subject: str  # school | preschool | any
    # Как основание звучит в ответе пользователю.
    citation: str
    # Фрагмент адреса страницы, по которому видно, что товар открыт именно в ветке
    # этого перечня. Товары размещены сразу в нескольких разделах каталога, и без
    # такой проверки в номер пункта приказа попадает код обычной категории.
    url_marker: str | None = None


ORDER_838 = NormDocument(
    id="order_838",
    short_name="приказ № 838",
    full_name=(
        "Приказ Министерства просвещения Российской Федерации "
        "от 28 ноября 2024 года № 838"
    ),
    subject="school",
    citation="приказ Минпросвещения России от 28.11.2024 № 838",
    url_marker="prikazu_838",
)

ORDER_1057 = NormDocument(
    id="order_1057",
    short_name="приказ № 1057",
    full_name=(
        "Приказ Минпросвещения России от 25.12.2024 № 1057 «Об утверждении перечня "
        "средств обучения и воспитания, необходимых для реализации образовательных программ»"
    ),
    subject="preschool",
    citation="приказ Минпросвещения России от 25.12.2024 № 1057",
)

FGOS_DO = NormDocument(
    id="fgos_do",
    short_name="ФГОС ДО",
    full_name=None,
    subject="preschool",
    citation="ФГОС ДО",
)

FOP_DO = NormDocument(
    id="fop_do",
    short_name="ФОП ДО",
    full_name=None,
    subject="preschool",
    citation="ФОП ДО",
)

# «Примерный перечень функциональных комплектов» — формулировка из описаний выгрузки.
# Отдельный документ, потому что в тексте он упоминается без номера приказа.
FUNC_KITS = NormDocument(
    id="func_kits",
    short_name="Примерный перечень функциональных комплектов",
    full_name=None,
    subject="preschool",
    citation="Примерный перечень функциональных комплектов",
)

DOCUMENTS: dict[str, NormDocument] = {
    doc.id: doc for doc in (ORDER_838, ORDER_1057, FGOS_DO, FOP_DO, FUNC_KITS)
}


def get(doc_id: str) -> NormDocument:
    return DOCUMENTS[doc_id]
