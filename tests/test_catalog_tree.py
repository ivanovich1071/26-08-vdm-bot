from ingest.catalog_tree import CatalogPath, classify


def test_root_is_uppercase_unnumbered():
    heading = classify("ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА")
    assert (heading.level, heading.kind, heading.code) == (0, "root", None)


def test_section_and_subsection_levels():
    assert classify("Раздел 2. Комплекс оснащения предметных кабинетов").level == 1
    assert classify("Подраздел 14. Кабинет физики").level == 2
    assert classify("Подраздел 14. Кабинет физики").code == "14"


def test_numbered_level_follows_code_depth():
    assert classify("01. Образовательные комплекты").level == 1
    assert classify("01.01 ПРЕДШКОЛА 2025").level == 2
    assert classify("2.20.63. Фрезерно-гравировальный станок").level == 3
    assert classify("2.20.63. Фрезерно-гравировальный станок").code == "2.20.63"


def test_school_branch_keeps_single_root():
    """Регрессия: «Подраздел N» не должен становиться самостоятельным корнем."""
    path = CatalogPath()
    for title in (
        "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838",
        "Раздел 2. Комплекс оснащения предметных кабинетов",
        "Подраздел 20. Кабинет труда (технологии)",
        "2.20.63. Фрезерно-гравировальный станок",
    ):
        path.push(title)

    assert path.root == "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838"
    assert len(path.titles) == 4
    assert path.deepest_numbered().code == "2.20.63"


def test_new_root_resets_deeper_levels():
    path = CatalogPath()
    path.push("ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838")
    path.push("Раздел 1. Комплекс оснащения общешкольных помещений")
    path.push("Подраздел 7. Спортивный комплекс")
    path.push("КОРРЕКЦИОННАЯ СРЕДА")

    assert path.titles == ["КОРРЕКЦИОННАЯ СРЕДА"]
    assert path.deepest_numbered() is None


def test_sibling_heading_truncates_deeper_levels():
    path = CatalogPath()
    path.push("ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА")
    path.push("01. Образовательные комплекты")
    path.push("01.01 ПРЕДШКОЛА 2025")
    path.push("01.01.01 Патриотическое воспитание")
    path.push("01.02 ПРЕДШКОЛА 2026")

    assert path.titles == [
        "ОБОРУДОВАНИЕ ДЛЯ ДЕТСКОГО САДА",
        "01. Образовательные комплекты",
        "01.02 ПРЕДШКОЛА 2026",
    ]
