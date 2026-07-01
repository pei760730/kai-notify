"""Tests for kai_notify. No real network: urlopen is monkeypatched."""

from __future__ import annotations

import json

import kai_notify


class _Resp:
    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch):
    """Patch urlopen to record the request instead of sending it."""
    sent: dict = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        sent["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", fake_urlopen)
    return sent


def _creds(monkeypatch):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "660156312")


def test_notify_sends_text(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    assert kai_notify.notify("hello") is True
    assert sent["body"]["text"] == "hello"
    assert sent["body"]["chat_id"] == "660156312"
    assert "bot123:abc/sendMessage" in sent["url"]


def test_notify_strips_and_skips_empty(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    assert kai_notify.notify("  spaced  ") is True
    assert sent["body"]["text"] == "spaced"
    # empty / whitespace -> skipped, no send
    sent.clear()
    assert kai_notify.notify("   ") is False
    assert kai_notify.notify("") is False
    assert sent == {}


def test_notify_digest_formats_bullets(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    assert kai_notify.notify_digest("Today", ["a", "b", "  ", "c"]) is True
    assert sent["body"]["text"] == "Today\n• a\n• b\n• c"


def test_notify_digest_empty_skips(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    assert kai_notify.notify_digest("", []) is False
    assert kai_notify.notify_digest("", ["   ", ""]) is False
    assert sent == {}


def test_failsoft_when_no_credentials(monkeypatch):
    monkeypatch.delenv("KAI_NOTIFY_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KAI_NOTIFY_CHAT_ID", raising=False)
    # must not raise, must not attempt a send
    called = {"hit": False}

    def boom(*a, **k):
        called["hit"] = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", boom)
    assert kai_notify.notify("hi") is False
    assert kai_notify.notify_digest("t", ["x"]) is False
    assert called["hit"] is False


def test_failsoft_when_telegram_errors(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(kai_notify.time, "sleep", lambda s: None)  # no real backoff

    def boom(req, timeout=None):
        raise OSError("telegram down")

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", boom)
    # error is swallowed, caller's cron survives
    assert kai_notify.notify("hi") is False


def test_long_message_is_clipped_by_codepoint(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    # 5000 CJK chars: over Telegram's 4096 limit, and would 400 if not clipped.
    assert kai_notify.notify("漢" * 5000) is True
    text = sent["body"]["text"]
    assert len(text) <= 4096
    assert text.endswith("…(truncated)")
    # codepoint-clean: no broken half-characters, body is valid as sent
    assert text[: -len("…(truncated)")].strip("漢") == ""


def test_200_with_ok_false_is_not_a_success(monkeypatch):
    _creds(monkeypatch)

    class _R:
        def read(self):
            return b'{"ok":false,"description":"Forbidden"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        kai_notify.urllib.request, "urlopen", lambda req, timeout=None: _R()
    )
    assert kai_notify.notify("hi") is False


def test_httperror_logs_description_and_hint(monkeypatch, caplog):
    import io

    _creds(monkeypatch)

    def raise_400(req, timeout=None):
        raise kai_notify.urllib.error.HTTPError(
            "url",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"ok":false,"description":"Bad Request: chat not found"}'),
        )

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", raise_400)
    with caplog.at_level("WARNING"):
        assert kai_notify.notify("hi") is False
    logged = caplog.text
    assert "chat not found" in logged  # Telegram's own description surfaced
    assert "/start" in logged  # actionable root-cause hint


# ── retry (bounded, budgeted, fail-soft) ─────────────────────────────────────
import io  # noqa: E402


def _http_error(code, body=b"{}"):
    return kai_notify.urllib.error.HTTPError("url", code, "err", {}, io.BytesIO(body))


def test_retries_transient_then_succeeds(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(kai_notify.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, b'{"parameters":{"retry_after":1}}')  # rate limit
        if calls["n"] == 2:
            raise _http_error(503)  # server error
        return _Resp()  # 3rd attempt succeeds

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", flaky)
    assert kai_notify.notify("hi") is True
    assert calls["n"] == 3  # retried through 429 then 5xx, then delivered


def test_gives_up_after_max_attempts(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(kai_notify.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_500(req, timeout=None):
        calls["n"] += 1
        raise _http_error(500)

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", always_500)
    assert kai_notify.notify("hi") is False
    assert calls["n"] == kai_notify._MAX_ATTEMPTS  # bounded, no infinite loop


def test_does_not_retry_permanent_errors(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(kai_notify.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def forbidden(req, timeout=None):
        calls["n"] += 1
        raise _http_error(403, b'{"description":"Forbidden"}')

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", forbidden)
    assert kai_notify.notify("hi") is False
    assert calls["n"] == 1  # 403 won't get better by retrying — fail fast


def test_honors_retry_after(monkeypatch):
    _creds(monkeypatch)
    slept = []
    monkeypatch.setattr(kai_notify.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def rate_limited(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, b'{"parameters":{"retry_after":2}}')
        return _Resp()

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", rate_limited)
    assert kai_notify.notify("hi") is True
    # waited at least Telegram's requested retry_after (jitter only adds).
    assert slept and slept[0] >= 2


# ── notify_metric — "green but empty" product-quantity guard ──────────────────
def test_metric_below_floor_alerts(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    # 0 produced with default floor 1 -> loud alert.
    assert kai_notify.notify_metric("voc daily", 0, unit="篇") is True
    text = sent["body"]["text"]
    assert "voc daily" in text
    assert "0 篇" in text
    assert "門檻 1 篇" in text


def test_metric_at_or_above_floor_is_silent(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    # Healthy: at/above floor sends nothing and returns False (success is silent).
    assert kai_notify.notify_metric("voc daily", 5, floor=1) is False
    assert kai_notify.notify_metric("voc daily", 1, floor=1) is False
    assert sent == {}


def test_metric_custom_floor(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    # 3 rows when we expect >= 10 is still "empty enough" to flag.
    assert kai_notify.notify_metric("radar", 3, floor=10) is True
    assert "3" in sent["body"]["text"] and "門檻 10" in sent["body"]["text"]


def test_metric_integers_render_without_decimal(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    assert kai_notify.notify_metric("x", 0.0, floor=2.0) is True
    text = sent["body"]["text"]
    assert "0.0" not in text and "2.0" not in text and "門檻 2" in text


def test_metric_unparseable_value_is_surfaced_not_swallowed(monkeypatch):
    _creds(monkeypatch)
    sent = _capture(monkeypatch)
    # A non-numeric value must not silently pass as healthy — say we can't read it.
    assert kai_notify.notify_metric("voc", "N/A") is True
    assert "無法判讀" in sent["body"]["text"]


def test_metric_never_raises_without_creds(monkeypatch):
    monkeypatch.delenv("KAI_NOTIFY_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KAI_NOTIFY_CHAT_ID", raising=False)
    # Below floor wants to alert, but no creds -> fail-soft False, never raises.
    assert kai_notify.notify_metric("voc", 0) is False
