"""Non-finite floors report uncertainty without losing exact integer comparisons."""

from __future__ import annotations

import kai_notify
import pytest

NONFINITE_FLOORS = [
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="positive-infinity"),
    pytest.param(float("-inf"), id="negative-infinity"),
]


@pytest.mark.parametrize("floor", NONFINITE_FLOORS)
@pytest.mark.parametrize("value", [0, 42])
@pytest.mark.parametrize("delivered", [True, False])
def test_nonfinite_floor_reports_uncertainty_and_delivery(
    monkeypatch, floor, value, delivered
):
    messages = []
    monkeypatch.setattr(
        kai_notify, "notify", lambda text: messages.append(text) or delivered
    )

    assert kai_notify.notify_metric("synthetic floor", value, floor=floor) is delivered
    assert len(messages) == 1
    assert "synthetic floor" in messages[0]
    assert "門檻無法判讀" in messages[0]
    assert "請檢查門檻設定" in messages[0]
    assert "但只產出" not in messages[0]
    assert "產品是空的" not in messages[0]
    assert "無法判讀的產出量" not in messages[0]


@pytest.mark.parametrize("floor", NONFINITE_FLOORS)
def test_nonfinite_floor_without_credentials_stays_fail_soft(monkeypatch, floor):
    monkeypatch.delenv("KAI_NOTIFY_BOT_TOKEN", raising=False)
    monkeypatch.delenv("KAI_NOTIFY_CHAT_ID", raising=False)

    def no_network(*args, **kwargs):
        pytest.fail("missing credentials must not reach the transport")

    monkeypatch.setattr(kai_notify.urllib.request, "urlopen", no_network)
    assert kai_notify.notify_metric("synthetic floor", 42, floor=floor) is False


@pytest.mark.parametrize("floor", NONFINITE_FLOORS)
def test_unreadable_value_keeps_existing_diagnostic_priority(monkeypatch, floor):
    messages = []
    monkeypatch.setattr(
        kai_notify, "notify", lambda text: messages.append(text) or True
    )

    assert kai_notify.notify_metric("synthetic floor", "N/A", floor=floor) is True
    assert len(messages) == 1
    assert "無法判讀的產出量" in messages[0]
    assert "門檻無法判讀" not in messages[0]


@pytest.mark.parametrize(
    ("value", "floor", "alerted"),
    [
        (0, 1, True),
        (1, 1, False),
        (42, 1, False),
        (0, 0, False),
        (-1, 0, True),
        (0, -1, False),
        (2**53, 2**53 + 1, True),
        (2**53, 2**53, False),
        (1e308, 10**400, True),
        (0, -(10**5000), False),
        (0.5, 0.75, True),
        (0.75, 0.75, False),
    ],
    ids=[
        "below",
        "equal",
        "above",
        "zero-floor",
        "negative-value",
        "negative-floor",
        "exact-integer-boundary",
        "exact-integer-equality",
        "integer-beyond-float-range",
        "negative-integer-beyond-display-limit",
        "fraction-below",
        "fraction-equal",
    ],
)
def test_finite_floor_keeps_existing_comparison(monkeypatch, value, floor, alerted):
    messages = []
    monkeypatch.setattr(
        kai_notify, "notify", lambda text: messages.append(text) or True
    )

    assert kai_notify.notify_metric("synthetic floor", value, floor=floor) is alerted
    assert len(messages) == int(alerted)
    if alerted:
        assert "門檻無法判讀" not in messages[0]
        assert "但只產出" in messages[0]
        assert str(floor) in messages[0]
