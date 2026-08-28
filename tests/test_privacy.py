from privacy.masking import Masker, contains_personal_data


def test_phone_is_masked_and_restored():
    masker = Masker()
    masked = masker.mask("Перезвоните на +7 916 330-02-79")

    assert "916" not in masked
    assert masked.endswith("[ТЕЛЕФОН_1]")
    assert masker.unmask(masked) == "Перезвоните на +7 916 330-02-79"


def test_email_and_name_are_masked():
    masker = Masker()
    masked = masker.mask("Иванов Иван, почта school@example.ru")

    assert "school@example.ru" not in masked
    assert "Иванов" not in masked
    assert not contains_personal_data(masked)


def test_name_is_recognised_by_patronymic_and_by_introduction():
    masker = Masker()
    assert "Соколова" not in masker.mask("Заведующая Соколова Мария Петровна")
    assert "Пётр" not in masker.mask("меня зовут Пётр")


def test_catalog_words_are_not_mistaken_for_a_name():
    """Регрессия 28.08: «Дидактический набор» уходил пользователю как «[ИМЯ_1] набор».

    Одного фамильного окончания мало — в названиях товаров таких слов сотни.
    """
    masker = Masker()
    text = 'Дидактический набор "Произносим звуки", Опасные ситуации, Логопедический уголок'
    assert masker.mask(text) == text


def test_unresolved_placeholder_never_reaches_the_user():
    """После перезапуска соответствия нет — показываем нейтральное слово, не метку."""
    masker = Masker()
    assert "[" not in masker.unmask("Перезвоните на [ТЕЛЕФОН_1], [ИМЯ_2]")


def test_same_value_gets_one_placeholder():
    masker = Masker()
    masked = masker.mask("тел +7 916 330-02-79 и ещё раз +7 916 330-02-79")

    assert masked.count("[ТЕЛЕФОН_1]") == 2
    assert len(masker.values) == 1


def test_inn_is_masked():
    masker = Masker()
    masked = masker.mask("ИНН 7701234567 для счёта")
    assert "7701234567" not in masked


def test_nothing_to_mask_leaves_text_alone():
    masker = Masker()
    text = "Нужны мячи для спортивного зала по приказу 838"
    assert masker.mask(text) == text
    assert masker.is_clean


def test_product_codes_are_not_mistaken_for_personal_data():
    """Регрессия: артикулы и номера пунктов не должны маскироваться."""
    masker = Masker()
    text = "Позиция 2.20.63, артикул MIN 95030, код 0Э-00005662"
    assert masker.mask(text) == text


def test_detector_finds_personal_data():
    assert contains_personal_data("почта a@b.ru")
    assert not contains_personal_data("мяч резиновый d=150mm")
