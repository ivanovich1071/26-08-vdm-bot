from ingest.html_text import html_to_text, split_kit_contents


def test_tags_become_text_and_lists_keep_bullets():
    html = "<h2><b>Набор карточек</b></h2><p>Для ДОО.</p><ul><li>Карточки 8х10&nbsp;см</li></ul>"
    assert html_to_text(html) == "Набор карточек\n\nДля ДОО.\n\n• Карточки 8х10 см"


def test_scripts_and_styles_are_dropped():
    html = "<div>Описание</div><script>var a = 1;</script><style>.x{color:red}</style>"
    assert html_to_text(html) == "Описание"


def test_broken_markup_does_not_lose_text():
    assert "Описание" in html_to_text("<div><p>Описание</b></div")


def test_plain_text_passes_through():
    assert html_to_text("Просто текст &laquo;в кавычках&raquo;") == "Просто текст «в кавычках»"


def test_empty_input():
    assert html_to_text(None) == ""
    assert split_kit_contents("") == ("", [])


def test_kit_block_after_header_line():
    text = html_to_text(
        "<p>Игра на сравнение.</p><h3>Состав комплекта:</h3>"
        "<ul><li>Карточки 8х10 см – 8шт.</li><li>Планшет</li></ul>"
    )
    description, kit = split_kit_contents(text)

    assert description == "Игра на сравнение."
    assert kit == ["Карточки 8х10 см – 8шт", "Планшет"]


def test_kit_header_inline_with_semicolons():
    """Заказчик часто пишет состав в подбор: «Материал: пластик. Состав набора: a; b»."""
    description, kit = split_kit_contents(
        "Материал: пластик. Состав набора: 90 пластиковых колец; контейнер для хранения"
    )

    assert description == "Материал: пластик."
    assert kit == ["90 пластиковых колец", "контейнер для хранения"]


def test_no_kit_block_keeps_description_intact():
    text = "Комплект для формирования представлений о составе числа."
    assert split_kit_contents(text) == (text, [])


def test_kit_items_are_deduplicated():
    _, kit = split_kit_contents("Состав комплекта: кубик; кубик; мяч")
    assert kit == ["кубик", "мяч"]
