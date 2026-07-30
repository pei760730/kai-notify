"""Tests for scripts/fleet_digest.py — the daily fleet health digest / heartbeat.

The script lives in ../scripts (not in the installed package), so we load it by
path. No real network: _latest_run and notify are monkeypatched.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest

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


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Never touch the repo's real state/ file during tests."""
    monkeypatch.setattr(fd, "_STATE_FILE", str(tmp_path / "fleet_history.json"))


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


def test_assess_running_is_not_a_problem():
    # In-progress (conclusion None) means "moving", not a failure.
    assert fd._assess("bot", "frequent", _run(None, 3), _NOW)["kind"] == "ok"


def test_assess_cancelled_is_a_problem():
    # 2026-07-26 反轉先前的判讀(原 test_assess_running_or_cancelled_not_a_problem
    # 把 cancelled 當「還在動」)。理由:job 層 `timeout-minutes` 逾時被殺，
    # conclusion 是 cancelled 而不是 failure。一條每 10 分鐘跑的下載佇列卡到逾時，
    # 對 owner 就是「這輪沒做完工作」，跟失敗同級 —— 而看門狗原本會回報「沒事」。
    # 這正是 media-sorter 管線 2026-07-02～07-26 靜默死 24 天那類事故的同款盲區，
    # 只是這次出現在監控器自己身上。
    a = fd._assess("bot", "frequent", _run("cancelled", 3), _NOW)
    assert a["kind"] == "fail"
    # 措辭要讓「人為按停」和「跑失敗」分得出來，否則 owner 無法判斷該不該動手。
    assert "中止" in a["detail"]


def test_monitored_covers_media_sorter_download_queue():
    # 名單缺口的迴歸釘子:本 repo 曾只監控 media-sorter 的 ytdlp 週檢，沒監控真正的
    # 下載佇列 collector.yml，於是那條管線死 24 天而 digest 全綠。
    entries = {(repo, wf) for repo, wf, _name, _cadence in fd.MONITORED}
    assert ("media-sorter", "collector.yml") in entries, (
        "media-sorter 的下載佇列不在監控名單 —— 它死掉時 digest 會報全綠"
    )


def test_monitored_covers_collector_core_bump():
    # 迴歸釘子(2026-07-31):core-bump 是 reusable caller,被呼叫端 timeout 逾時
    # = cancelled、if:failure() 通知免疫;它同時又不在本名單 → 兩層盲區疊加,
    # 「這輪沒開 bump PR」可以無聲無息。caller 端已補 notify-cancelled(collector
    # #91),本名單是第二層,兩層都要在。
    entries = {(repo, wf) for repo, wf, _name, _cadence in fd.MONITORED}
    assert ("collector", "core-bump.yml") in entries, (
        "collector core-bump 不在監控名單 —— 它被中止時 digest 會報全綠"
    )


def test_monitored_covers_ig_token_refresh():
    # 迴歸釘子(2026-07-31):token 續期斷掉不當場痛,60 天後整條線才死,
    # 缺席訊號等於沒有訊號。
    entries = {(repo, wf) for repo, wf, _name, _cadence in fd.MONITORED}
    assert ("ig-insights-sync", "token-refresh.yml") in entries, (
        "ig token 續期不在監控名單 —— 靜默斷掉會在 60 天後以斷線形式引爆"
    )


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
    monkeypatch.setattr(
        fd, "_latest_run", lambda r, w, t: (_recent("success", 10), None)
    )
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
            return (_recent("failure", 10, url="https://gh/run/BAD"), None)
        return (_recent("success", 10), None)

    monkeypatch.setattr(fd, "_latest_run", fake_latest)
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]
    assert "要你看一下" in text
    assert "th-ops remind" in text
    assert "https://gh/run/BAD" in text
    # the other N-1 are summarized, not listed
    assert f"其他 {len(fd.MONITORED) - 1} 個都正常" in text


def test_main_degraded_reads_do_not_fake_all_green(monkeypatch):
    # (a) honesty: if most reads fail on auth, say so — never imply health.
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(fd, "_latest_run", lambda r, w, t: (None, "auth"))
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]
    assert "一切正常" not in text
    assert "報不準" in text and "FLEET_READ_TOKEN" in text
    # history must NOT be written on a degraded run
    assert not os.path.exists(fd._STATE_FILE)


def test_main_streak_suffix_from_history(monkeypatch):
    # (b) memory: a cron failing again shows "(連 N 天)".
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(
        fd, "_load_history", lambda: {"th-ops/remind.yml": ["fail", "fail"]}
    )

    def fake_latest(repo, wf, token):
        if repo == "th-ops" and wf == "remind.yml":
            return (_recent("failure", 10), None)
        return (_recent("success", 10), None)

    monkeypatch.setattr(fd, "_latest_run", fake_latest)
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "連 3 天" in box["text"]  # 2 prior + today


def test_main_green_streak_shown_when_long(monkeypatch):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(
        fd, "_load_history", lambda: {"_fleet": ["green", "green", "green"]}
    )
    monkeypatch.setattr(
        fd, "_latest_run", lambda r, w, t: (_recent("success", 10), None)
    )
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "連續 4 天全綠" in box["text"]  # 3 prior + today


def test_main_history_is_idempotent_within_a_day(monkeypatch):
    # A second same-day run (agent test / workflow_dispatch) must still SEND but
    # must NOT append another sample — else streak/flapping memory inflates.
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(
        fd, "_latest_run", lambda r, w, t: (_recent("success", 10), None)
    )
    _capture_notify(monkeypatch)
    key = f"{fd.MONITORED[0][0]}/{fd.MONITORED[0][1]}"

    assert fd.main() == 0
    assert len(fd._load_history()[key]) == 1  # recorded once
    assert fd.main() == 0  # same-day re-run
    hist = fd._load_history()
    assert len(hist[key]) == 1  # not doubled
    assert hist["_last_date"]  # stamped for the idempotency guard


def test_main_degraded_when_no_pat(monkeypatch):
    monkeypatch.delenv("FLEET_READ_TOKEN", raising=False)
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "看不到各 repo" in box["text"]


# ── _transitions / _is_flapping / _recovered ─────────────────────────────────
def test_transitions_counts_health_flips():
    assert fd._transitions(["ok", "ok", "ok"]) == 0
    assert fd._transitions(["ok", "fail", "ok"]) == 2
    assert fd._transitions(["ok", "fail", "ok", "fail"]) == 3
    # 'unknown' is neutral — a blind day neither starts nor breaks a run.
    assert fd._transitions(["ok", "unknown", "ok"]) == 0
    assert fd._transitions(["ok", "unknown", "fail"]) == 1


def test_is_flapping_needs_real_oscillation():
    # one clean incident (down then back up) is NOT flapping
    assert fd._is_flapping(["ok", "ok", "fail"], "ok") is False
    # genuine oscillation over >=4 samples IS flapping
    assert fd._is_flapping(["ok", "fail", "ok"], "fail") is True
    # too little history can't false-positive
    assert fd._is_flapping(["fail"], "ok") is False


def test_recovered_only_from_real_breakage():
    assert fd._recovered(["fail"], "ok") is True
    assert fd._recovered(["stale"], "ok") is True
    # readable-again after a blind day is not a recovery
    assert fd._recovered(["unknown"], "ok") is False
    # still broken today is not a recovery
    assert fd._recovered(["fail"], "fail") is False
    # no history -> nothing to recover from
    assert fd._recovered([], "ok") is False


def test_main_reports_recovery_even_when_all_green(monkeypatch):
    # yesterday th-ops failed; today it's green -> a recovery line, not silence.
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(
        fd, "_load_history", lambda: {"th-ops/remind.yml": ["fail", "fail"]}
    )
    monkeypatch.setattr(
        fd, "_latest_run", lambda r, w, t: (_recent("success", 10), None)
    )
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]
    assert "恢復了" in text
    assert "th-ops remind" in text
    assert "一切正常" not in text  # a recovery is news, not a plain all-green


def test_main_flags_flapping_cron_even_when_ok_today(monkeypatch):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    # oscillating history ending on ok yesterday too -> not a recovery, purely
    # the flapping signal.
    monkeypatch.setattr(
        fd,
        "_load_history",
        lambda: {"benchmark-radar/radar-watchdog.yml": ["fail", "ok", "fail", "ok"]},
    )
    monkeypatch.setattr(
        fd, "_latest_run", lambda r, w, t: (_recent("success", 10), None)
    )
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "反覆" in box["text"]
    assert "radar watchdog" in box["text"]
    assert "恢復了" not in box["text"]  # ok yesterday too -> not a recovery


def test_main_tags_flapping_on_a_current_problem(monkeypatch):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(
        fd,
        "_load_history",
        lambda: {"th-ops/remind.yml": ["fail", "ok", "fail", "ok"]},
    )

    def fake_latest(repo, wf, token):
        if repo == "th-ops" and wf == "remind.yml":
            return (_recent("failure", 10), None)
        return (_recent("success", 10), None)

    monkeypatch.setattr(fd, "_latest_run", fake_latest)
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "時好時壞" in box["text"]
