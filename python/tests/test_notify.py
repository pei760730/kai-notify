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
