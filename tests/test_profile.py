"""Профиль разговора: что бот запоминает о задаче и чего не запоминает никогда."""

from core.profile import DialogProfile


def test_first_message_gives_institution_and_room():
    profile = DialogProfile()
    profile.update_from_text("какое оборудование необходимо в спортзал детского сада?")

    assert profile.institution == "детский сад"
    assert profile.room == "спортивный зал"


def test_details_accumulate_across_messages():
    """Регрессия 28.08: бот переспрашивал возраст, который ему назвали ходом раньше."""
    profile = DialogProfile()
    profile.update_from_text("нужно оснастить спортзал в детском саду")
    profile.update_from_text("сейчас пусто, только ремонт сделали. возраст 3-6 лет")
    profile.update_from_text("бюджет 200 тысяч, надо к 1 сентября, по приказу 838")

    assert profile.age == "3–6 лет"
    assert profile.budget == "до 200 000 ₽"
    assert profile.deadline == "к 1 сентября"
    assert profile.norm_doc_ids == ["order_838"]


def test_prompt_tells_the_model_not_to_ask_again():
    profile = DialogProfile(institution="школа", room="кабинет физики")
    prompt = profile.as_prompt()

    assert "кабинет физики" in prompt
    assert "Переспрашивать" in prompt


def test_empty_profile_adds_nothing_to_the_prompt():
    assert DialogProfile().as_prompt() == ""


def test_personal_data_is_not_a_profile_field():
    """Профиль лежит на диске и целиком уходит модели — ПДн в нём быть не может."""
    fields = set(DialogProfile().to_dict())

    assert not fields & {"name", "phone", "email", "organization", "contact", "inn"}


def test_unknown_fields_from_disk_are_ignored():
    profile = DialogProfile.from_dict({"room": "столовая", "phone": "+7 916 330-02-79"})

    assert profile.room == "столовая"
    assert not hasattr(profile, "phone")


def test_rejection_marks_the_last_shown_positions():
    profile = DialogProfile()
    profile.remember_offered(["S1", "S2"])
    profile.update_from_text("дорого, покажите дешевле")

    assert profile.rejected == ["S1", "S2"]


def test_quantities_are_not_mistaken_for_a_budget():
    profile = DialogProfile()
    profile.update_from_text("нужно 20 мячей и 5 обручей")

    assert profile.budget is None


def test_a_kindergarten_is_recognised_without_the_word_kindergarten():
    """02.09: «спортзал в саду» не дал учреждения — и человек остался без кнопок."""
    profile = DialogProfile()
    profile.update_from_text("чем оснастить спортзал в саду, дети 3–6 лет")

    assert profile.institution == "детский сад"
    assert profile.room == "спортивный зал"
    assert profile.task_known


def test_a_question_about_a_document_is_not_a_purchase_decision():
    """31.08: вопрос про приказ 838 перевёл весь разговор в школьный режим."""
    profile = DialogProfile()
    profile.update_from_text("что значит приказ 838")

    assert profile.asked_about_docs == ["order_838"]
    assert profile.norm_doc_ids == []

    profile.update_from_text("нужно оборудование для группы в детском саду")
    assert profile.audience == "preschool"
