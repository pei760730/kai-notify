"""Transport errors may contain request secrets; log only the error category."""

import http.client
import json
import urllib.error
from unittest.mock import Mock

import kai_notify
import pytest


@pytest.mark.parametrize("separator", [" ", "\t"])
def test_invalid_url_does_not_log_credentials(monkeypatch, caplog, separator):
    token = f"OFFLINE_PRIVATE_TOKEN{separator}suffix"
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", token)
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "OFFLINE_PRIVATE_CHAT")
    monkeypatch.setattr(kai_notify.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(kai_notify.random, "uniform", lambda *_: 0.0)
    sleep = Mock()
    monkeypatch.setattr(kai_notify.time, "sleep", sleep)
    connect = Mock(side_effect=lambda *_: pytest.fail("network is forbidden"))
    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)
    requests = []

    def transport(req, timeout=None):
        requests.append(req)
        # Exercise the real stdlib URL validation, before any connection.
        connection = http.client.HTTPConnection(req.host)
        connection.request(req.get_method(), req.selector, body=req.data)
        pytest.fail("invalid request path must be rejected")

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    assert kai_notify.notify("OFFLINE_PRIVATE_MESSAGE") is False
    assert len(requests) == kai_notify._MAX_ATTEMPTS
    assert [call.args[0] for call in sleep.call_args_list] == list(
        kai_notify._BACKOFF[: kai_notify._MAX_ATTEMPTS - 1]
    )
    connect.assert_not_called()
    assert "OFFLINE_PRIVATE_TOKEN" not in caplog.text
    assert "InvalidURL" in caplog.text
    assert "retrying if budget" in caplog.text


@pytest.mark.parametrize("error_type", [urllib.error.URLError, TimeoutError, OSError])
def test_transient_error_log_is_private_and_retry_still_succeeds(
    monkeypatch, caplog, error_type
):
    monkeypatch.setenv("KAI_NOTIFY_BOT_TOKEN", "OFFLINE_PRIVATE_TOKEN")
    monkeypatch.setenv("KAI_NOTIFY_CHAT_ID", "OFFLINE_PRIVATE_CHAT")
    monkeypatch.setattr(kai_notify.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(kai_notify.random, "uniform", lambda *_: 0.0)
    sleep = Mock()
    monkeypatch.setattr(kai_notify.time, "sleep", sleep)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"ok":true}'

    def transport(req, timeout=None):
        requests.append(req)
        if len(requests) == 1:
            raise error_type(f"{req.full_url}: {req.data.decode('utf-8')}")
        return Response()

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", transport)
    assert kai_notify.notify("OFFLINE_PRIVATE_MESSAGE") is True
    assert len(requests) == 2
    assert requests[0].data == requests[1].data
    assert json.loads(requests[0].data)["chat_id"] == "OFFLINE_PRIVATE_CHAT"
    sleep.assert_called_once_with(kai_notify._BACKOFF[0])
    for private in (
        "OFFLINE_PRIVATE_TOKEN",
        "OFFLINE_PRIVATE_CHAT",
        "OFFLINE_PRIVATE_MESSAGE",
    ):
        assert private not in caplog.text
    assert error_type.__name__ in caplog.text
