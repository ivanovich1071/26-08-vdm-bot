from ingest.catalog_tree import CatalogPath
from norms import documents as docs
from norms.extract import (
    code_anomalies,
    code_from_url,
    codes_in_query,
    document_ids_in_text,
    extract,
    root_document_id,
)

SCHOOL_URL = (
    "https://vdm.ru/catalog/oborudovanie_dlya_shkoly_po_prikazu_838/"
    "razdel_2_kompleks_osnashcheniya_predmetnykh_kabinetov/"
    "podrazdel_20_kabinet_truda_tekhnologii/2_20_63_frezerno_gravirovalnyy_stanok/"
    "shr_frezerno_gravirovalnyy_stanok_s_chpu_.html"
)
GARDEN_URL = (
    "https://vdm.ru/catalog/oborudovanie_dlya_detskogo_sada/"
    "12_sportivnoe_oborudovanie_i_inventar/12_04_myachi/mfl_myach.html"
)


def school_path() -> CatalogPath:
    path = CatalogPath()
    for title in (
        "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838",
        "Раздел 2. Комплекс оснащения предметных кабинетов",
        "Подраздел 20. Кабинет труда (технологии)",
        "2.20.63. Фрезерно-гравировальный станок",
    ):
        path.push(title)
    return path


def test_document_recognized_by_number_and_date():
    assert document_ids_in_text("ПРИКАЗЕ Минпросвещения России от 25.12.2024 N 1057") == [
        docs.ORDER_1057.id
    ]
    assert docs.ORDER_838.id in document_ids_in_text("Согласно приказу № 838 школа оснащается")
    assert document_ids_in_text("Комплекты по ФГОС ДО и ФОП ДО") == [
        docs.FGOS_DO.id,
        docs.FOP_DO.id,
    ]


def test_no_document_in_neutral_text():
    assert document_ids_in_text("Набор счетного материала на магнитах") == []


def test_root_declares_document_only_when_number_present():
    assert root_document_id("ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838") == docs.ORDER_838.id
    # Нумерация похожа на перечень, но документ не назван — догадку не пишем.
    assert root_document_id("ОСНАЩЕНИЕ НОВОСТРОЕК") is None


def test_code_from_url_takes_last_numbered_segment():
    assert code_from_url(SCHOOL_URL) == "2.20.63"
    assert code_from_url(None) is None


def test_codes_in_query():
    assert codes_in_query("нужен п. 2.1.14 и ещё 2.11.3") == ["2.1.14", "2.11.3"]
    assert codes_in_query("мячи для зала") == []


def test_extract_uses_heading_and_url():
    links = extract(path=school_path(), url=SCHOOL_URL, description="Станок с ЧПУ")
    codes = {(link.doc_id, link.item_code, link.source) for link in links}

    assert (docs.ORDER_838.id, "2.20.63", "heading") in codes
    assert links[0].citation.startswith("позиция 2.20.63")


def test_url_from_other_branch_does_not_become_norm_code():
    """Регрессия: товар лежит в приказе 838, но ссылка ведёт в раздел детского сада.

    Без проверки ветки код категории «12.04 Мячи» попадал в базу как пункт приказа.
    """
    path = CatalogPath()
    for title in (
        "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838",
        "Раздел 1. Комплекс оснащения общешкольных помещений",
        "Подраздел 7. Спортивный комплекс",
        "1.7.11. Мячи",
    ):
        path.push(title)

    links = extract(path=path, url=GARDEN_URL, description="")
    codes = {link.item_code for link in links}

    assert codes == {"1.7.11"}
    assert all(link.source != "url_slug" for link in links)


def test_description_adds_second_document():
    links = extract(
        path=CatalogPath(),
        url=None,
        description="Комплект входит в перечень ПРИКАЗА Минпросвещения России от 25.12.2024 N 1057",
    )
    assert [link.doc_id for link in links] == [docs.ORDER_1057.id]
    assert links[0].source == "description"


def test_product_without_any_evidence_has_no_norms():
    assert extract(path=CatalogPath(), url=GARDEN_URL, description="Мяч резиновый") == []


def test_anomaly_when_item_code_outside_its_section():
    path = CatalogPath()
    for title in (
        "ОБОРУДОВАНИЕ ДЛЯ ШКОЛЫ ПО ПРИКАЗУ № 838",
        "Раздел 2. Комплекс оснащения предметных кабинетов",
        "Подраздел 1. Кабинет начальных классов",
        "2.3.10. Не из этого подраздела",
    ):
        path.push(title)

    links = extract(path=path, url=None, description="")
    assert code_anomalies(path, links) == ["пункт 2.3.10 в разделе 2.1"]


def test_no_anomaly_for_consistent_code():
    path = school_path()
    assert code_anomalies(path, extract(path=path, url=None, description="")) == []
