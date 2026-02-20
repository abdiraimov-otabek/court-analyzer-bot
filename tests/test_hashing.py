from src.services.hashing import HashingService


def test_hashing_service_is_stable():
    service = HashingService(salt="pepper")

    assert service.hash_value("123") == service.hash_value("123")
    assert service.hash_value("123") != service.hash_value("456")
