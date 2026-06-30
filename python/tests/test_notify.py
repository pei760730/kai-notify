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
    assert text[:-len("…(truncated)")].strip("漢") == ""


def test_200_with_ok_false_is_not_a_success(monkeypatch):
    _creds(monkeypatch)

    class _R:
        def read(self):
            return b'{"ok":false,"description":"Forbidden"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", lambda req, timeout=None: _R())
    assert kai_notify.notify("hi") is False


def test_httperror_logs_description_and_hint(monkeypatch, caplog):
    import io

    _creds(monkeypatch)

    def raise_400(req, timeout=None):
        raise kai_notify.urllib.error.HTTPError(
            "url", 400, "Bad Request", {},
            io.BytesIO(b'{"ok":false,"description":"Bad Request: chat not found"}'),
        )

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", raise_400)
    with caplog.at_level("WARNING"):
        assert kai_notify.notify("hi") is False
    logged = caplog.text
    assert "chat not found" in logged          # Telegram's own description surfaced
    assert "/start" in logged                  # actionable root-cause hint
