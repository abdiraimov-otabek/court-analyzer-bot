from src.services.database_case_client import QueryParser


def test_query_parser_extracts_year_and_quarter():
    parser = QueryParser()
    params = parser.parse("9 ААС 1 квартал 2023")

    assert params.date_from == "2023-01-01"
    assert params.date_to == "2023-03-31"


def test_query_parser_extracts_inn():
    parser = QueryParser()
    params = parser.parse("ИНН 7701234567")

    assert params.inn_or_name == "7701234567"


def test_query_parser_maps_court():
    parser = QueryParser()
    params = parser.parse("анализ решений 9 ААС 2023")

    assert params.court == "9 арбитражный апелляционный суд"


def test_query_parser_extracts_case_number():
    parser = QueryParser()
    params = parser.parse("А82-4356/2014")

    assert params.case_number == "А82-4356/2014"


def test_query_parser_extracts_quoted_name():
    parser = QueryParser()
    params = parser.parse("дело по " + '"ООО Ромашка"')

    assert params.inn_or_name == "ООО Ромашка"


def test_query_parser_maps_moscow_court_name():
    parser = QueryParser()
    params = parser.parse("практика по статье 61.2 в АС Москвы за 2024 год")

    assert params.court == "АС города Москвы"


def test_query_parser_maps_spb_court_name():
    parser = QueryParser()
    params = parser.parse("практика по ст.61.3 в АС СПб за 2025 год")

    assert params.court == "АС города Санкт-Петербурга и Ленинградской области"


def test_query_parser_extracts_generic_as_city_phrase():
    parser = QueryParser()
    params = parser.parse("практика по ст.61.2 в АС Новосибирска за 2025 год")

    assert params.court == "АС Новосибирска"


def test_query_parser_extracts_article_and_sets_bankruptcy_case_type_for_61_series():
    parser = QueryParser()
    params = parser.parse("Практика по статье 61.3 в АС Москвы за 2025 год")

    assert params.article == "61.3"
    assert params.case_type == "B"
    assert params.full_article == "ст. 61.3 Закона о банкротстве"
    assert params.law_family == "127-ФЗ"
    assert params.law_display_name == "Закона о банкротстве"
    assert params.law_inferred is True


def test_query_parser_extracts_paragraph_reference():
    parser = QueryParser()
    params = parser.parse("Практика по п.2 ст.61.3 в АС Москвы за 2025 год")

    assert params.article == "61.3"
    assert params.paragraph == "2"
    assert params.full_article == "п. 2 ст. 61.3 Закона о банкротстве"


def test_query_parser_extracts_explicit_non_bankruptcy_law():
    parser = QueryParser()
    params = parser.parse("Практика по статье 723 ГК РФ в АС Москвы за 2025 год")

    assert params.article == "723"
    assert params.full_article == "ст. 723 ГК РФ"
    assert params.law_family == "ГК РФ"
    assert params.law_display_name == "ГК РФ"
    assert params.law_inferred is False
