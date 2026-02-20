import io
import json
import logging

from src.app.logging import ContextFilter, JsonFormatter, clear_request_context, set_request_context


def test_context_filter_injects_context_fields():
    set_request_context("req-1", "hash-1", "component")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="/tmp",
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    ContextFilter().filter(record)
    assert record.request_id == "req-1"
    assert record.user_hash == "hash-1"
    assert record.component == "component"
    clear_request_context()


def test_json_formatter_includes_data_payload():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("json_test")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    record = logger.makeRecord(
        name="json_test",
        level=logging.INFO,
        fn="/tmp",
        lno=1,
        msg="event",
        args=(),
        exc_info=None,
    )
    record.data = {"key": "value"}
    output = JsonFormatter().format(record)
    payload = json.loads(output)
    assert payload["message"] == "event"
    assert payload["key"] == "value"

    logger.removeHandler(handler)
