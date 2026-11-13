"""A HTTP 200 alone is not Telegram's delivery acknowledgement.

All transport calls are fake; an invalid reply must not trigger another send
because the first request might already have delivered its message.
"""

import kai_notify
import pytest


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"<html>PRIVATE_RESPONSE</html>",
        b'\xff{"ok":true}',
        b'{"ok":',
        b"null",
        b"[]",
        b"true",
        b'"PRIVATE_RESPONSE"',
        b"{}",
        b'{"ok":"false"}',
        b'{"ok":1}',
        b'{"ok":[true]}',
        b'{"ok":{"PRIVATE_RESPONSE":true}}',
        b'{"ok":true,"result":' + b"1" * 5000 + b"}",
        b"[" * 2000 + b"0" + b"]" * 2000,
    ],
)
def test_invalid_200_is_unconfirmed_without_resending(monkeypatch, caplog, body):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    calls = []

    class Response:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def transport(req, timeout=None):
        calls.append(req)
        return Response()

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    monkeypatch.setattr(
        kai_notify.time,
        "sleep",
        lambda _: pytest.fail("must not retry an invalid acknowledgement"),
    )
    assert kai_notify.notify("fake notification") is False
    assert len(calls) == 1
    assert "PRIVATE_RESPONSE" not in caplog.text
    assert "fake-token" not in caplog.text


@pytest.mark.parametrize("ok", [True, False])
def test_boolean_acknowledgement_preserves_result(monkeypatch, ok):
    import json
    from unittest.mock import MagicMock

    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "fake-chat")
    transport = MagicMock()
    transport.return_value.__enter__.return_value.read.return_value = json.dumps(
        {"ok": ok, "result": {"message_id": 1}}
    ).encode()
    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    assert kai_notify.notify("fake notification") is ok
    transport.assert_called_once()
