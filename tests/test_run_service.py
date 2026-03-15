import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.app import run_service


def test_run_service_starts_admin_on_configured_port(monkeypatch):
    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        async def serve(self):
            captured["served"] = True

    monkeypatch.setenv("APP_ROLE", "admin")
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )

    run_service.main()

    assert captured["args"] == ("src.app.admin_api:app",)
    assert captured["kwargs"] == {
        "host": "0.0.0.0",
        "port": 8123,
        "reload": False,
    }
    assert captured["served"] is True


def test_run_service_starts_bot_by_default(monkeypatch):
    fake_module = ModuleType("src.app.run_bot")
    captured = {"ran": False}

    async def fake_main():
        captured["ran"] = True

    fake_module.main = fake_main

    monkeypatch.delenv("APP_ROLE", raising=False)
    monkeypatch.setitem(sys.modules, "src.app.run_bot", fake_module)

    run_service.main()

    assert captured["ran"] is True


def test_run_service_supports_combined_role(monkeypatch):
    captured = {"ran": False}

    async def fake_run_combined():
        captured["ran"] = True

    monkeypatch.setenv("APP_ROLE", "combined")
    monkeypatch.setattr(run_service, "_run_combined", fake_run_combined)

    run_service.main()

    assert captured["ran"] is True


def test_run_service_rejects_unknown_role(monkeypatch):
    monkeypatch.setenv("APP_ROLE", "worker")

    with pytest.raises(RuntimeError, match="Unsupported APP_ROLE"):
        run_service.main()
