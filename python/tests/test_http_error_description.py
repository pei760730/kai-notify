"""Malformed error metadata must not break permanent-error fail-soft handling."""

import io
import json
from unittest.mock import Mock

import kai_notify
import pytest


def _send_error(monkeypatch, code, raw, reason="Bad Request"):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    error = kai_notify.urllib.error.HTTPError(
        "fake-url", code, reason, {}, io.BytesIO(raw)
    )
    transport = Mock(side_effect=error)
    sleep = Mock(side_effect=AssertionError("permanent errors must not retry"))
    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    monkeypatch.setattr(kai_notify.time, "sleep", sleep)
    assert kai_notify.notify("fake notification") is False
    transport.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("code", [400, 403])
@pytest.mark.parametrize(
    "description", [None, {"PRIVATE_BODY": "secret"}, ["PRIVATE_BODY"], 7, False]
)
def test_wrong_typed_description_is_failsoft(monkeypatch, caplog, code, description):
    raw = json.dumps({"ok": False, "description": description}).encode()
    _send_error(monkeypatch, code, raw)
    assert f"HTTP {code} Bad Request" in caplog.text
    assert "PRIVATE_BODY" not in caplog.text
    assert "fake-token" not in caplog.text
    if code == 403:
        assert "bot token invalid/revoked" in caplog.text


@pytest.mark.parametrize("reason", [None, 7])
def test_non_string_reason_is_not_used_as_a_description(monkeypatch, caplog, reason):
    _send_error(monkeypatch, 400, b"not JSON", reason)
    assert "HTTP 400" in caplog.text


@pytest.mark.parametrize(
    ("code", "description", "hint"),
    [
        (400, "Bad Request: chat not found", "bot was never /start-ed"),
        (401, "Unauthorized", "bot token invalid/revoked"),
        (400, "Bad Request: message is too long", "message is too long"),
    ],
)
def test_valid_description_and_hint_are_preserved(
    monkeypatch, caplog, code, description, hint
):
    raw = json.dumps({"ok": False, "description": description}).encode()
    _send_error(monkeypatch, code, raw)
    assert description in caplog.text
    assert hint in caplog.text
