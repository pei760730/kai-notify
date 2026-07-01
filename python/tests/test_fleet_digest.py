"""Tests for scripts/fleet_digest.py — the daily fleet health digest / heartbeat.

The script lives in ../scripts (not in the installed package), so we load it by
path. No real network: _latest_run and notify are monkeypatched.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(
    os.path.join(_HERE, "..", "..", "scripts", "fleet_digest.py")
)


def _load():
    spec = importlib.util.spec_from_file_location("fleet_digest", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load()
_NOW = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


def _run(conclusion, mins_ago, url="https://gh/run/1"):
    created = (_NOW - timedelta(minutes=mins_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"conclusion": conclusion, "created_at": created, "html_url": url}


def _recent(conclusion, mins_ago, url="https://gh/run/1"):
    """Like _run but anchored to real now — main() measures age against the
    live clock, so its fixtures must be relative to now, not the fixed _NOW."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=mins_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {"conclusion": conclusion, "created_at": created, "html_url": url}


# ── _humanize ────────────────────────────────────────────────────────────────
def test_humanize_scales():
    assert fd._humanize(timedelta(minutes=5)) == "5m"
    assert fd._humanize(timedelta(minutes=59)) == "59m"
    assert fd._humanize(timedelta(hours=3)) == "3h"
    assert fd._humanize(timedelta(hours=47)) == "47h"
    assert fd._humanize(timedelta(days=3)) == "3d"


# ── _assess ──────────────────────────────────────────────────────────────────
def test_assess_ok_recent_success():
    a = fd._assess("voc daily", "daily", _run("success", 120), _NOW)
    assert a["kind"] == "ok"
    assert a["url"] == "https://gh/run/1"


def test_assess_failure_is_a_problem_with_link():
    a = fd._assess("th-ops remind", "daily", _run("failure", 180), _NOW)
    assert a["kind"] == "fail"
    assert "失敗" in a["detail"]
    assert a["url"] == "https://gh/run/1"


def test_assess_stale_daily_flagged():
    # 31h old daily run is past the 26h staleness bound -> stale.
    a = fd._assess("voc daily", "daily", _run("success", 31 * 60), _NOW)
    assert a["kind"] == "stale"
    assert "沒動靜" in a["detail"]


def test_assess_fresh_weekly_not_stale():
    # 3 days is fine for a weekly cadence.
    a = fd._assess("GOLD adoption", "weekly", _run("success", 3 * 24 * 60), _NOW)
    assert a["kind"] == "ok"


def test_assess_unknown_when_unreadable():
    a = fd._assess("KaiOS ig-sync", "daily", None, _NOW)
    assert a["kind"] == "unknown"
    assert a["url"] == ""


def test_assess_running_or_cancelled_not_a_problem():
    # In-progress (conclusion None) and cancelled both mean "moving / not a failure".
    assert fd._assess("bot", "frequent", _run(None, 3), _NOW)["kind"] == "ok"
    assert fd._assess("bot", "frequent", _run("cancelled", 3), _NOW)["kind"] == "ok"


def test_assess_frequent_stale_when_idle_too_long():
    # A 5-min collector silent for 3h is past its 2h bound.
    a = fd._assess("collector", "frequent", _run("success", 180), _NOW)
    assert a["kind"] == "stale"


# ── main() message composition ───────────────────────────────────────────────
def _capture_notify(monkeypatch):
    box = {}
    monkeypatch.setattr(
        fd, "notify", lambda text: box.__setitem__("text", text) or True
    )
    return box


def test_main_all_green_is_one_reassuring_line(monkeypatch):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(fd, "_latest_run", lambda r, w, t: _recent("success", 10))
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]
    assert "一切正常" in text
    assert "👇" not in text  # no problem list when all green
    assert "沒來" in text  # heartbeat footer present


def test_main_lists_only_problems(monkeypatch):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")

    def fake_latest(repo, wf, token):
        # Make exactly one monitored entry fail; the rest succeed (fresh).
        if repo == "th-ops" and wf == "remind.yml":
            return _recent("failure", 10, url="https://gh/run/BAD")
        return _recent("success", 10)

    monkeypatch.setattr(fd, "_latest_run", fake_latest)
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]
    assert "要你看一下" in text
    assert "th-ops remind" in text
    assert "https://gh/run/BAD" in text
    # the other N-1 are summarized, not listed
    assert f"其他 {len(fd.MONITORED) - 1} 個都正常" in text


def test_main_degraded_when_no_pat(monkeypatch):
    monkeypatch.delenv("FLEET_READ_TOKEN", raising=False)
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "看不到各 repo" in box["text"]
