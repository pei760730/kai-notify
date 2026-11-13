"""Bad rate-limit metadata must not escape the notifier's fail-soft boundary."""

import io
import json
import math
from unittest.mock import MagicMock

import kai_notify
import pytest


def _error(source, delay, *, header=None):
    body = {"parameters": {"retry_after": delay}} if source == "body" else {}
    headers = {"Retry-After": str(delay)} if source == "header" else {}
    if header is not None:
        headers["Retry-After"] = str(header)
    raw = json.dumps(body).encode()
    return (
        kai_notify.urllib.error.HTTPError(
            "fake-url", 429, "rate limited", headers, io.BytesIO(raw)
        ),
        raw,
    )


@pytest.mark.parametrize("source", ["body", "header"])
@pytest.mark.parametrize("delay", [-1, "NaN", "Infinity"])
def test_invalid_delay_is_not_returned(source, delay):
    error, raw = _error(source, delay)
    assert kai_notify._retry_after(error, raw) is None


@pytest.mark.parametrize("delay", [-1, "NaN", "Infinity"])
def test_invalid_body_delay_falls_back_to_valid_header(delay):
    error, raw = _error("body", delay, header=7)
    assert kai_notify._retry_after(error, raw) == 7.0


@pytest.mark.parametrize("source", ["body", "header"])
@pytest.mark.parametrize("delay", [0, 2])
def test_valid_delay_remains_supported(source, delay):
    error, raw = _error(source, delay)
    assert kai_notify._retry_after(error, raw) == delay


def test_valid_body_still_takes_precedence():
    error, raw = _error("body", 2, header=7)
    assert kai_notify._retry_after(error, raw) == 2


@pytest.mark.parametrize("source", ["body", "header"])
@pytest.mark.parametrize("delay", [-1, "NaN"])
def test_malformed_429_uses_bounded_fallback_without_raising(
    monkeypatch, source, delay
):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok":true}'
    error, _ = _error(source, delay)
    transport = MagicMock(side_effect=[error, response])
    waits = []

    def sleep(seconds):
        # Match time.sleep's rejection of negative/non-finite intervals without
        # spending any wall-clock time on valid backoff.
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid sleep interval")
        waits.append(seconds)

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    monkeypatch.setattr(kai_notify.time, "sleep", sleep)
    monkeypatch.setattr(kai_notify.random, "uniform", lambda *args: 0)
    assert kai_notify.notify("fake notification") is True
    assert transport.call_count == 2
    assert waits == [kai_notify._BACKOFF[0]]


@pytest.mark.parametrize("delay", [-1, "NaN"])
def test_repeated_malformed_429_still_stops_after_max_attempts(monkeypatch, delay):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    errors = [_error("body", delay)[0] for _ in range(kai_notify._MAX_ATTEMPTS)]
    transport = MagicMock(side_effect=errors)
    waits = []

    def sleep(seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid sleep interval")
        waits.append(seconds)

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    monkeypatch.setattr(kai_notify.time, "sleep", sleep)
    monkeypatch.setattr(kai_notify.random, "uniform", lambda *args: 0)
    assert kai_notify.notify("fake notification") is False
    assert transport.call_count == kai_notify._MAX_ATTEMPTS
    assert waits == list(kai_notify._BACKOFF)
