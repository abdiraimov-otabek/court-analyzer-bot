from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from logging.config import dictConfig
from pathlib import Path
from typing import Any

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_user_hash_var: ContextVar[str] = ContextVar("user_hash", default="-")
_component_var: ContextVar[str] = ContextVar("component", default="app")


def set_request_context(request_id: str, user_hash: str | None, component: str | None) -> None:
    _request_id_var.set(request_id)
    if user_hash is not None:
        _user_hash_var.set(user_hash)
    if component:
        _component_var.set(component)


def clear_request_context() -> None:
    _request_id_var.set("-")
    _user_hash_var.set("-")
    _component_var.set("app")


def new_request_id() -> str:
    return str(uuid.uuid4())


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        record.user_hash = _user_hash_var.get()
        record.component = _component_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_hash": getattr(record, "user_hash", "-"),
            "component": getattr(record, "component", "app"),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        def default_serializer(obj: Any) -> Any:
            try:
                from dataclasses import asdict, is_dataclass
                if is_dataclass(obj):
                    return asdict(obj)
            except ImportError:
                pass
            return str(obj)

        return json.dumps(payload, ensure_ascii=True, default=default_serializer)


def configure_logging(app_name: str) -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.getenv("LOG_FILE", f"logs/{app_name}.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"context": {"()": ContextFilter}},
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "filters": ["context"],
                },
                "file": {
                    "class": "logging.handlers.TimedRotatingFileHandler",
                    "formatter": "json",
                    "filters": ["context"],
                    "filename": log_file,
                    "when": "midnight",
                    "backupCount": 7,
                    "encoding": "utf-8",
                },
            },
            "root": {"level": level, "handlers": ["console", "file"]},
        }
    )
    httpx_level = os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper()
    logging.getLogger("httpx").setLevel(httpx_level)


def log_event(logger: logging.Logger, message: str, **data: Any) -> None:
    logger.info(message, extra={"data": data})


def log_debug(logger: logging.Logger, message: str, **data: Any) -> None:
    logger.debug(message, extra={"data": data})
