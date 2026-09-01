"""Разбор текстов приказов в справочник пунктов.

Проверяется не «работает ли pypdf», а две конкретные порчи вёрстки, из-за которых
сверка врала: разорванный пробелом номер и номер, склеенный с названием.
"""

from norms.items import NormItem, load, parse_838, parse_1057

# Так выглядит выдача pdf для приказа 838: заголовки разделов отдельными
# строками, пункт — номер с точкой и название.
TEXT_838 = """
Раздел 2. Комплекс оснащения предметных кабинетов
Подраздел 4. Кабинет учителя-логопеда (учителя-дефектолога)
Основное оборудование
2.4.35. Дидактические пособия и обучающие игры для формирования словарного запаса
2.4.40. Набор дидактических картинок с изображением предметов, действий, понятий
Подраздел 20. Кабинет труда (технологии)
2.20.63. Фрезерно-гравировальный станок с числовым программным управлением
"""

# А так — приказ 1057: таблица, из которой номер приезжает то склеенным с
# названием, то разорванным переносом строки.
TEXT_1057 = """
1.13.4.3.1.9Интерактивная панель (доска с
потолочным проектором)
Шт. 1  +
1.13.4.3.1.1
0
Комплект интерактивно-цифровых
комплексов
Шт. 1  +
1.3.4.1 Барабан с палочками Шт. 10 +
"""


def test_838_keeps_section_of_item():
    items = {item.code: item for item in parse_838(TEXT_838)}
    assert items["2.4.35"].title.startswith("Дидактические пособия")
    assert items["2.4.35"].section == "Кабинет учителя-логопеда (учителя-дефектолога)"
    assert items["2.20.63"].section == "Кабинет труда (технологии)"


def test_838_ignores_headings():
    codes = {item.code for item in parse_838(TEXT_838)}
    assert codes == {"2.4.35", "2.4.40", "2.20.63"}


def test_1057_glues_code_split_by_layout():
    """«1.13.4.3.1.1 0» — это пункт 1.13.4.3.1.10, а не 1.13.4.3.1.1.

    Из-за этой порчи 482 пункта базы знаний «не находились» в приказе, и сверка
    показывала ошибку там, где данные верны.
    """
    codes = {item.code for item in parse_1057(TEXT_1057)}
    assert "1.13.4.3.1.10" in codes
    assert "1.13.4.3.1.1" not in codes


def test_1057_separates_code_glued_to_title():
    items = {item.code: item for item in parse_1057(TEXT_1057)}
    assert items["1.13.4.3.1.9"].title.startswith("Интерактивная панель")


def test_1057_splits_unit_and_quantity():
    items = {item.code: item for item in parse_1057(TEXT_1057)}
    assert items["1.3.4.1"].title == "Барабан с палочками"
    assert items["1.3.4.1"].unit == "Шт."
    assert items["1.3.4.1"].quantity == "10"


def test_missing_file_gives_empty_reference(tmp_path):
    """Приказов рядом с проектом может не быть — бот всё равно должен работать."""
    assert load(tmp_path / "нет-такого.json") == {}


def test_load_reads_written_reference(tmp_path):
    import json

    target = tmp_path / "norm_items.json"
    target.write_text(
        json.dumps(
            {
                "order_838": [
                    {"code": "2.4.35", "title": "Дидактические пособия", "section": "Логопед"}
                ]
            }
        ),
        encoding="utf-8",
    )
    known = load(target)
    assert known["order_838"]["2.4.35"] == NormItem(
        doc_id="order_838", code="2.4.35", title="Дидактические пособия", section="Логопед"
    )
