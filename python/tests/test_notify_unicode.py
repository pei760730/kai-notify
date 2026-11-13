"""Malformed Unicode from decoded external JSON must not crash callers."""

import json
from unittest.mock import MagicMock, Mock

import kai_notify
import pytest


def _call(surface, text):
    if surface == "text":
        return kai_notify.notify(text)
    if surface == "digest":
        return kai_notify.notify_digest("Daily", [text])
    return kai_notify.notify_metric(text, 0)


@pytest.mark.parametrize("surface", ["text", "digest", "metric"])
@pytest.mark.parametrize(
    "text",
    [
        json.loads(r'"PRIVATE_INPUT\ud800"'),
        json.loads(r'"PRIVATE_INPUT\udfff"'),
    ],
)
def test_unpaired_surrogate_is_failsoft_without_transport(
    monkeypatch, caplog, surface, text
):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    transport = Mock(side_effect=AssertionError("must not send invalid Unicode"))
    sleep = Mock(side_effect=AssertionError("must not retry invalid Unicode"))
    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    monkeypatch.setattr(kai_notify.time, "sleep", sleep)

    assert _call(surface, text) is False
    transport.assert_not_called()
    sleep.assert_not_called()
    assert "invalid Unicode" in caplog.text
    assert "PRIVATE_INPUT" not in caplog.text
    assert "fake-token" not in caplog.text


@pytest.mark.parametrize("text", ["漢字", json.loads(r'"\ud83d\ude80"')])
def test_valid_unicode_keeps_original_payload(monkeypatch, text):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    transport = MagicMock()
    transport.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)

    assert kai_notify.notify(text) is True
    transport.assert_called_once()
    request = transport.call_args.args[0]
    assert json.loads(request.data.decode("utf-8"))["text"] == text
