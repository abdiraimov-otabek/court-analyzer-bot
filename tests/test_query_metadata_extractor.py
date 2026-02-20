from src.services.request_processor import QueryMetadataExtractor


def test_query_metadata_extractor_detects_moscow_court():
    extractor = QueryMetadataExtractor()
    metadata = extractor.extract("Практика по статье 61.2 в АС Москвы за 2025 год")
    assert metadata.court == "АС города Москвы"
    assert metadata.period == "2025 год"


def test_query_metadata_extractor_detects_aas_format():
    extractor = QueryMetadataExtractor()
    metadata = extractor.extract("Анализ споров в 9 ААС за 2024 год")
    assert metadata.court == "9 арбитражный апелляционный суд"
    assert metadata.period == "2024 год"


def test_query_metadata_extractor_detects_spb_court():
    extractor = QueryMetadataExtractor()
    metadata = extractor.extract("Практика по ст.61.3 в АС СПб за 2025 год")
    assert metadata.court == "АС города Санкт-Петербурга и Ленинградской области"
    assert metadata.period == "2025 год"


def test_query_metadata_extractor_detects_generic_as_city_phrase():
    extractor = QueryMetadataExtractor()
    metadata = extractor.extract("Практика по ст.61.2 в АС Новосибирска за 2025 год")
    assert metadata.court == "АС Новосибирска"
    assert metadata.period == "2025 год"
