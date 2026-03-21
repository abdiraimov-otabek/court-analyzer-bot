import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("bs4")

from src.services.sudact_client import SudactClient


class _DummyAsyncClient:
    async def get(self, *args, **kwargs):  # pragma: no cover - not used
        raise RuntimeError("not used in parsing tests")


def test_extract_max_int_for_range_style_total() -> None:
    client = SudactClient(async_http_client=_DummyAsyncClient())

    assert client._extract_max_int("Показано 1-11 из 491") == 491


def test_extract_max_int_for_regular_found_label() -> None:
    client = SudactClient(async_http_client=_DummyAsyncClient())

    assert client._extract_max_int("Найдено: 1 234 решения") == 1234
