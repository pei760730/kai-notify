"""Unusable metric values must neither look healthy nor crash the caller."""

from __future__ import annotations

import kai_notify
import pytest

INVALID_VALUES = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
    pytest.param("NaN", id="nan-string"),
    pytest.param("Infinity", id="infinity-string"),
    pytest.param("-Infinity", id="negative-infinity-string"),
    pytest.param("1e9999", id="overflow-string"),
    pytest.param(10**400, id="overflow-integer"),
    pytest.param(10**5000, id="overflow-integer-beyond-repr-limit"),
]


@pytest.mark.parametrize("value", INVALID_VALUES)
@pytest.mark.parametrize("delivered", [True, False])
def test_invalid_metric_reports_uncertainty_and_delivery(monkeypatch, value, delivered):
    messages = []
    monkeypatch.setattr(
        kai_notify, "notify", lambda text: messages.append(text) or delivered
    )

    assert kai_notify.notify_metric("synthetic", value) is delivered
    assert len(messages) == 1
    assert "synthetic" in messages[0]
    assert "無法判讀的產出量" in messages[0]
    assert "但只產出" not in messages[0]


@pytest.mark.parametrize("value", INVALID_VALUES)
def test_invalid_metric_without_credentials_stays_fail_soft(monkeypatch, value):
    monkeypatch.delenv("KAI_NOTIFY_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KAI_NOTIFY_CHAT_ID", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("missing credentials must not reach the transport")

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", no_network)
    assert kai_notify.notify_metric("synthetic", value) is False


@pytest.mark.parametrize("value", [1, "1", 1.5, "1.5", 1e308])
def test_finite_healthy_metrics_remain_silent(monkeypatch, value):
    def no_notification(*args, **kwargs):
        pytest.fail("finite metrics at or above the floor must stay silent")

    monkeypatch.setattr(kai_notify, "notify", no_notification)
    assert kai_notify.notify_metric("synthetic", value) is False


def test_existing_unparseable_metric_keeps_value_in_alert(monkeypatch):
    messages = []
    monkeypatch.setattr(
        kai_notify, "notify", lambda text: messages.append(text) or True
    )
    assert kai_notify.notify_metric("synthetic", "N/A") is True
    assert "（'N/A'）" in messages[0]
