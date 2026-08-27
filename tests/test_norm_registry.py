"""Разбор реестра «пункт приказа 1057 → код 1С».

Сам реестр — файл заказчика и в репозиторий не попадает, поэтому проверяем
на строках ровно тех форм, что встречаются в его выгрузке.
"""

from __future__ import annotations

import json

import pytest

from ingest.norm_registry import load, parse_lines

LINES = [
    "Наименование по приказу 1057 код по 1с8",
    "1.3.4.1          Барабан с палочками                              36341",
    "1.3.4.5          Бубен большой                                    60864",
    "1.3.4.8          Вертушка (шумовой музыкальный инструм35386",
    "1.3.4.24         Кукла в нарядной одежде                          0Э-00005388",
    "1.3.4.44         Хореографический станок пристенный - комплект",
    "",
    "1.2.1 Герб республики, города",
]


def test_registry_lines_are_parsed():
    entries = parse_lines(LINES)
    assert [(e.item_code, e.sku_1c) for e in entries] == [
        ("1.3.4.1", "36341"),
        ("1.3.4.5", "60864"),
        ("1.3.4.8", "35386"),
        ("1.3.4.24", "0Э-00005388"),
    ]


def test_clipped_name_does_not_eat_the_code():
    """В исходном PDF наименование обрезано по ширине колонки и слипается с кодом."""
    entry = parse_lines(["1.3.4.8          Вертушка (шумовой музыкальный инструм35386"])[0]
    assert entry.sku_1c == "35386"
    assert entry.item_title == "Вертушка (шумовой музыкальный инструм"


def test_position_without_a_product_is_skipped():
    """Позицию перечня, которую заказчик ничем не закрывает, не выдумываем."""
    assert parse_lines(["1.3.4.44   Хореографический станок пристенный - комплект"]) == []


def test_mapping_and_report_are_written(tmp_path, monkeypatch):
    from ingest import norm_registry

    monkeypatch.setattr(norm_registry, "parse_pdf", lambda path: parse_lines(LINES))
    out = tmp_path / "norms_1057.json"

    report = norm_registry.build(tmp_path / "реестр.pdf", {"36341", "60864"}, out)

    assert report.lines_with_code == 4
    assert report.matched == 2
    assert len(report.unmatched) == 2
    assert report.match_rate == pytest.approx(0.5)

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["doc_id"] == "order_1057"
    assert saved["products"]["36341"] == [
        {"item_code": "1.3.4.1", "item_title": "Барабан с палочками"}
    ]


def test_one_product_can_close_several_positions(tmp_path, monkeypatch):
    from ingest import norm_registry

    lines = [
        "1.3.4.40         Тамбурины - набор                                60863",
        "1.3.4.7          Бубен средний                                    60863",
        "1.3.4.7          Бубен средний                                    60863",
    ]
    monkeypatch.setattr(norm_registry, "parse_pdf", lambda path: parse_lines(lines))
    out = tmp_path / "norms_1057.json"

    norm_registry.build(tmp_path / "реестр.pdf", {"60863"}, out)

    codes = [e["item_code"] for e in json.loads(out.read_text(encoding="utf-8"))["products"]["60863"]]
    assert codes == ["1.3.4.40", "1.3.4.7"], "повтор строки не должен множить привязку"


def test_missing_registry_is_not_an_error(tmp_path):
    assert load(tmp_path / "нет-файла.json") == {}
