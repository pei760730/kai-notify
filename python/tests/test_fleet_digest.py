"""Tests for scripts/fleet_digest.py — the daily fleet health digest / heartbeat.

The script lives in ../scripts (not in the installed package), so we load it by
path. No real network: _latest_run and notify are monkeypatched.
"""

from __future__ import annotations

import importlib.util
import io
import json
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

# The autouse fixture below stubs fd._workflow_states for every test; grab the
# real one now so the two tests that exercise it directly aren't testing the stub.
_REAL_WORKFLOW_STATES = fd._workflow_states


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Never touch the repo's real state/ file during tests."""
    monkeypatch.setattr(fd, "_STATE_FILE", str(tmp_path / "fleet_history.json"))


@pytest.fixture(autouse=True)
def _no_network_workflow_states(monkeypatch):
    """main() also asks GitHub for each workflow's on/off state and whether the
    repo is archived. Default both to "couldn't read" so every existing test stays
    offline and behaves exactly as it did before those lookups existed; tests that
    care override them.

    ⚠ 2026-09-07:這裡必須是 None 而不是 {}。改判讀之後 {} 的意思變成「我成功讀到
    清單了,而且裡面一支都沒有」= 每條監控的檔案都不存在,整份 digest 會被判成 off。
    None 才是「沒讀到」,判讀退回 run-based —— 也就是這些測試原本要驗的路徑。"""
    monkeypatch.setattr(fd, "_workflow_states", lambda repo, token: None)
    monkeypatch.setattr(fd, "_repo_archived", lambda repo, token: None)


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


def test_humanize_exact_unit_boundaries():
    assert fd._humanize(timedelta(minutes=60)) == "1h"
    assert fd._humanize(timedelta(hours=48)) == "2d"


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


def test_monitored_covers_media_sorter_queue_health():
    # 名單缺口的迴歸釘子:本 repo 曾只監控 media-sorter 的 ytdlp 週檢，沒監控真正的
    # 下載佇列，於是那條管線死 24 天而 digest 全綠。
    #
    # 2026-09-07 換靶不撤哨:collector.yml / ytdlp-weekly-check.yml 都已從該 repo
    # 刪除(下載改走 GAS webhook relay)，哨兵改指向現在真正看佇列健康的
    # backlog-watch.yml。**上面那個教訓沒有過期，所以這支測試不是刪掉而是改指**。
    entries = {(repo, wf) for repo, wf, _name, _cadence in fd.MONITORED}
    assert ("media-sorter", "backlog-watch.yml") in entries, (
        "media-sorter 的佇列健康不在監控名單 —— 它死掉時 digest 會報全綠"
    )
    # 名單指著不存在的檔案 = digest 把「檔案不見了」報成「該跑沒跑」，連喊 21 天。
    for dead in ("collector.yml", "ytdlp-weekly-check.yml"):
        assert ("media-sorter", dead) not in entries, (
            f"{dead} 已從 media-sorter 刪除，留在名單裡只會每天產生一則假 ⏰"
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


def test_archived_repo_is_not_monitored():
    # 原本這裡釘的是「ig token 續期必須在名單裡」(2026-07-31)。2026-09-07 該 repo
    # 已 archived —— archived repo 唯讀、GitHub 停止派送排程，卻**不會**把 workflow
    # 的 state 改成 disabled_*(實測 token-refresh.yml 仍回報 active)，所以它只會
    # 靜靜走進 stale 門檻然後每天喊一次假 ⏰。監控一個結構上不可能再跑的排程沒有意義。
    #
    # 這條是弱釘子(釘值)；真正的防線是下面 _assess 的 archived / exists 行為測試 ——
    # 那組保證下一個被封存或被刪掉的目標會「被報出來」而不是「變成假警報」。
    repos = {repo for repo, _wf, _name, _cadence in fd.MONITORED}
    assert "ig-insights-sync" not in repos, (
        "ig-insights-sync 已 archived，留在名單裡每天都會產生假 ⏰"
    )


def test_monitored_covers_fitbit_freshness():
    # 迴歸釘子(2026-09-02):fitbit 的 freshness.yml 是該 repo 唯一雲端看門狗,
    # 紅了只停在 Actions 頁 —— 09-01 紅過兩次、owner 收到零則。該 repo 刻意不放
    # 通知 secret,由本名單讀 run 結論補上最後一哩;cadence 貼著它的每日排程。
    by_key = {(repo, wf): cadence for repo, wf, _name, cadence in fd.MONITORED}
    assert ("fitbit", "freshness.yml") in by_key, (
        "fitbit 新鮮度不在監控名單 —— 攝入停了 owner 不會知道"
    )
    assert by_key[("fitbit", "freshness.yml")] == "daily"


def test_gdrive_audit_registered_as_bi_monthly():
    # 迴歸釘子(2026-08-23):gdrive 的 cron 是 `0 1 1 1,3,5,7,9,11 *`(單數月 1 號),
    # 最長一段 7/1→9/1 = 62 天。先前登記成 monthly(32 天門檻),於是每個週期會有
    # 約 30 天被誤判成 ⏰ stale —— 唯一每天會叫的通道被訓練成可以忽略。
    by_key = {(repo, wf): cadence for repo, wf, _name, cadence in fd.MONITORED}
    assert by_key[("gdrive-organizer", "monthly-drive-audit.yml")] == "bi-monthly", (
        "gdrive 雙月審被登記成別的節奏 —— 節奏比 cron 密就會每輪喊假 stale"
    )
    # 62 天的真實間隔必須還算新鮮,63 天才過期。
    assert (
        fd._assess("gdrive", "bi-monthly", _run("success", 62 * 24 * 60), _NOW)["kind"]
        == "ok"
    )
    assert (
        fd._assess("gdrive", "bi-monthly", _run("success", 64 * 24 * 60), _NOW)["kind"]
        == "stale"
    )


def test_every_monitored_cadence_is_defined():
    # 節奏打錯字會靜靜退回 daily 的 26 小時門檻(_STALE_AFTER.get 的預設值),
    # 對任何比 daily 稀疏的 cron 都等於每天喊假 stale。載入期就該叫,不留到執行期。
    for repo, wf, _name, cadence in fd.MONITORED:
        assert cadence in fd._STALE_AFTER, (
            f"{repo}/{wf} 的節奏 {cadence!r} 沒有 stale 門檻"
        )
        assert cadence in fd._CADENCE_HUMAN, (
            f"{repo}/{wf} 的節奏 {cadence!r} 沒有白話說明"
        )


def test_assess_frequent_stale_when_idle_too_long():
    # A 5-min collector silent for 3h is past its 2h bound.
    a = fd._assess("collector", "frequent", _run("success", 180), _NOW)
    assert a["kind"] == "stale"


def test_assess_exact_stale_boundary_is_still_fresh():
    a = fd._assess("voc daily", "daily", _run("success", 26 * 60), _NOW)
    assert a["kind"] == "ok"


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


def test_main_degrades_at_exactly_half_blind_reads(monkeypatch):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    calls = {"n": 0}

    def half_blind(repo, wf, token):
        calls["n"] += 1
        if calls["n"] <= len(fd.MONITORED) // 2:
            return (None, "auth")
        return (_recent("success", 10), None)

    monkeypatch.setattr(fd, "_latest_run", half_blind)
    monkeypatch.setattr(
        fd,
        "_save_history",
        lambda hist: (_ for _ in ()).throw(
            AssertionError("history must stay untouched")
        ),
    )
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    assert "報不準" in box["text"]


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


def test_streak_suffix_reports_exactly_two_days():
    assert fd._streak_suffix(["fail"], "fail") == "(連 2 天)"


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


# ── workflow 開關狀態(active / disabled_manually / disabled_inactivity)─────────
#
# 真事故:2026-08-01 owner 裁示停掉 th-customs-scan/scan.yml(MOC open-data API
# 對外關閉,每月只是燒 runner 到逾時)。停用的 workflow 不會再產生新 run,所以它
# 最後一次 run 的結論(07-25 cancelled)被永遠凍結,digest 於是每天回報
# 「❌ th-customs 月掃 — 被中止(連 N 天)」,天數還一直長。它沒壞,是被關掉了。
def test_assess_manually_disabled_is_not_a_failure():
    a = fd._assess(
        "th-customs 月掃",
        "monthly",
        _run("cancelled", 8 * 24 * 60),
        _NOW,
        "disabled_manually",
    )
    assert a["kind"] == "off"
    assert "停用" in a["detail"]


def test_assess_disabled_beats_a_frozen_bad_run():
    """開關狀態要先於 run 判讀 —— 否則凍結的舊 run 會蓋過「它已經關了」。"""
    stale_fail = _run("failure", 400 * 24 * 60)  # 又老又紅
    assert (
        fd._assess("x", "daily", stale_fail, _NOW, "disabled_manually")["kind"] == "off"
    )
    # 沒有 state 資訊時(讀不到 workflow 清單)行為完全不變 —— 這是 fail-soft 的底線
    assert fd._assess("x", "daily", stale_fail, _NOW, None)["kind"] == "fail"


# ── 名單腐爛:目標被刪掉 / repo 被封存 ────────────────────────────────────────
#
# 真事故(2026-09-07 實測):media-sorter 的 collector.yml 與 ytdlp-weekly-check.yml
# 都已從 repo 刪除(下載改走 GAS webhook relay,最後一次 collector run 是 08-29),
# 但 MONITORED 還指著它們。digest 於是把「這個檔案不存在」報成 ⏰ 該跑沒跑,
# 連續 12～21 天,「後端一切正常」整整 13 天不可能出現。
# 同期 ig-insights-sync 被 archived,GitHub 停止派送排程卻不改 workflow 的 state
# (token-refresh.yml 至今仍是 active),所以 disabled_* 那層完全接不住。
def test_assess_deleted_workflow_is_not_a_failure():
    """名單指著一個已經不存在的檔案 —— 那是名單過期,不是 cron 壞掉。"""
    stale = _run(
        "success", 400 * 24 * 60
    )  # 又老(GitHub 會保留已刪 workflow 的 run 歷史)
    a = fd._assess(
        "media-sorter collector", "frequent", stale, _NOW, None, exists=False
    )
    assert a["kind"] == "off", "檔案不存在被報成該跑沒跑 = 每天一則假警報"
    assert "不在 repo 裡" in a["detail"]


def test_assess_unreadable_workflow_list_is_not_deleted():
    """**本組最重要的一條**:exists=None(沒讀到清單)絕不可以被當成 exists=False
    (讀到了、裡面沒有它)。把自己的失明報成全世界的死亡,是看門狗最不能犯的錯 ——
    一次網路抖動就會讓整份 digest 變成「所有 cron 都消失了」。"""
    stale = _run("success", 400 * 24 * 60)
    a = fd._assess("x", "daily", stale, _NOW, None, exists=None)
    assert a["kind"] == "stale", "讀不到清單時判讀必須原樣退回 run-based"


def test_assess_archived_repo_is_not_a_failure():
    """archived repo 唯讀、排程不再派送,但 workflow 的 state 仍是 active ——
    這是 disabled_* 接不住、只能靠 repo 層判斷的一類。"""
    stale = _run("success", 30 * 24 * 60)
    a = fd._assess("ig token 續期", "weekly", stale, _NOW, "active", archived=True)
    assert a["kind"] == "off"
    assert "封存" in a["detail"]


def test_assess_archived_beats_everything_else():
    """repo 層的封存要先於 workflow 層的 state 與 run —— 否則凍結的舊 run 會蓋過它。"""
    stale_fail = _run("failure", 400 * 24 * 60)
    assert (
        fd._assess("x", "daily", stale_fail, _NOW, "active", archived=True)["kind"]
        == "off"
    )
    # 反向:沒封存(False)或讀不到(None)時,行為完全不變
    for archived in (False, None):
        assert (
            fd._assess("x", "daily", stale_fail, _NOW, "active", archived=archived)[
                "kind"
            ]
            == "fail"
        ), f"archived={archived} 不該改變判讀"


def test_deleted_and_archived_still_get_a_line_every_day():
    """honesty 不變式:可以不喊狼,不可以假裝那條 cron 不存在。
    兩者都判 off —— off 不進「要你看一下」的計數,但每天照列在「另外」區塊。"""
    stale = _run("success", 400 * 24 * 60)
    gone = fd._assess("a", "daily", stale, _NOW, None, exists=False)
    arch = fd._assess("b", "daily", stale, _NOW, "active", archived=True)
    for a in (gone, arch):
        assert a["kind"] == "off"
        assert a["kind"] in fd._NEUTRAL_KINDS, "off 必須是中性 kind,不計入健康趨勢"


def test_assess_inactivity_disabled_is_loud():
    """GitHub 會把「repo 太久沒動靜」的排程自動關掉,而且不通知任何人 ——
    這是真的靜默死亡,跟人為停用必須分開報,不可以一起被靜音。"""
    a = fd._assess("某條月排程", "monthly", None, _NOW, "disabled_inactivity")
    assert a["kind"] == "fail"
    assert "自動停用" in a["detail"]


def test_neutral_kinds_do_not_look_like_instability():
    """被關掉的那幾天不算「時好時壞」—— 它根本沒在跑,沒有擺盪可言。"""
    assert fd._is_flapping(["ok", "off", "off", "off"], "ok") is False
    # 真的擺盪還是要抓到(對照組,證明上面不是因為函式壞了才回 False)
    assert fd._is_flapping(["ok", "fail", "ok", "fail"], "ok") is True


def _all_success_except_disabled(monkeypatch, disabled_repo, disabled_wf, state):
    monkeypatch.setenv("FLEET_READ_TOKEN", "x")
    monkeypatch.setattr(
        fd,
        "_latest_run",
        lambda r, w, t: (
            (_recent("cancelled", 10), None)
            if (r, w) == (disabled_repo, disabled_wf)
            else (_recent("success", 10), None)
        ),
    )

    def _states(repo, token):
        # 從 MONITORED 推導,不手寫清單:2026-09-07 起「讀到了、裡面沒有它」
        # 就是「這支被刪了」,所以少列一支等於把它判成消失。
        out = {wf: "active" for r, wf, _n, _c in fd.MONITORED if r == repo}
        if repo == disabled_repo:
            out[disabled_wf] = state
        return out

    monkeypatch.setattr(
        fd,
        "_workflow_states",
        _states,
    )


def test_main_disabled_workflow_is_shown_but_not_counted_as_a_problem(monkeypatch):
    repo, wf = "th-customs-scan", "scan.yml"
    assert (repo, wf) in [(r, w) for r, w, _n, _c in fd.MONITORED], "監測名單前提變了"
    _all_success_except_disabled(monkeypatch, repo, wf, "disabled_manually")
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]

    # 不喊狼:它不該出現在「要你看一下」的清單裡
    assert "要你看一下" not in text
    # 但也不准消失 —— 每天照列一行,owner 才不會忘了有這條 cron 被關著
    assert "th-customs 月掃" in text and "🔕" in text
    # 而且不可以被算進「都正常」的頭數
    assert f"其餘 {len(fd.MONITORED) - 1} 個都正常" in text


def test_main_inactivity_disabled_still_demands_attention(monkeypatch):
    """反向:GitHub 自動關掉的排程必須留在「要你看一下」裡。
    這條和上一條共用同一個 code path,只差 state 字串 —— 一起釘才擋得住
    「乾脆把所有 disabled_* 都靜音」這種看似合理的簡化。"""
    repo, wf = "th-customs-scan", "scan.yml"
    _all_success_except_disabled(monkeypatch, repo, wf, "disabled_inactivity")
    box = _capture_notify(monkeypatch)
    assert fd.main() == 0
    text = box["text"]
    assert "要你看一下" in text
    assert "th-customs 月掃" in text and "自動停用" in text


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_workflow_states_maps_basename_to_state(monkeypatch):
    """回傳要以檔名為鍵(MONITORED 存的是檔名,API 給的是 .github/workflows/x.yml)。"""
    captured = {}
    payload = {
        "workflows": [
            {"path": ".github/workflows/scan.yml", "state": "disabled_manually"},
            {"path": ".github/workflows/ci.yml", "state": "active"},
            {"path": "", "state": "active"},  # 殘缺條目要被跳過
            {"path": ".github/workflows/x.yml"},  # 沒 state 也跳過
        ]
    }

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeResp(payload)

    monkeypatch.setattr(fd.urllib.request, "urlopen", fake_urlopen)
    out = _REAL_WORKFLOW_STATES("th-customs-scan", "tok")
    assert out == {"scan.yml": "disabled_manually", "ci.yml": "active"}
    assert "th-customs-scan" in captured["url"]


def test_workflow_states_is_fail_soft(monkeypatch):
    """讀不到就回 None,讓判讀退回原本行為 —— 絕不能讓整份摘要死掉。

    ⚠ 2026-09-07 從 `{}` 改成 `None`,而且這個差別是承重的:這份清單同時是
    「這支 workflow 還在不在 repo 裡」的唯一依據。回 `{}` 的話,一次網路失敗
    就等於宣告「這個 repo 一支 workflow 都沒有」= 每條監控都被判成檔案不見了。
    看門狗把自己的失明報成全世界的死亡,比不報還糟。"""

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(fd.urllib.request, "urlopen", boom)
    assert _REAL_WORKFLOW_STATES("any", "tok") is None
    assert _REAL_WORKFLOW_STATES("any", "tok") != {}, (
        "回 {} 會被判讀成『讀到了、這個 repo 沒有任何 workflow』"
    )


def test_save_history_is_fail_soft(monkeypatch):
    monkeypatch.setattr(fd.os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(
        "builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    fd._save_history({"x": ["ok"]})


def test_latest_run_reads_workflow_runs_and_uses_first(monkeypatch):
    payload = {
        "workflow_runs": [
            {"id": "newest"},
            {"id": "older"},
        ]
    }
    monkeypatch.setattr(
        fd.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload)
    )
    run, err = fd._latest_run("repo", "ci.yml", "tok")
    assert err is None
    assert run == {"id": "newest"}


def _raise_http(code, headers=None):
    raise fd.urllib.error.HTTPError(
        "url", code, "error", headers or {}, io.BytesIO(b"{}")
    )


def test_latest_run_classifies_403_auth(monkeypatch):
    monkeypatch.setattr(fd.urllib.request, "urlopen", lambda *a, **k: _raise_http(403))
    assert fd._latest_run("repo", "ci.yml", "tok") == (None, "auth")


def test_latest_run_classifies_403_rate_limit(monkeypatch):
    monkeypatch.setattr(
        fd.urllib.request,
        "urlopen",
        lambda *a, **k: _raise_http(403, {"x-ratelimit-remaining": "0"}),
    )
    assert fd._latest_run("repo", "ci.yml", "tok") == (None, "ratelimit")


def test_latest_run_classifies_429_rate_limit(monkeypatch):
    monkeypatch.setattr(fd.urllib.request, "urlopen", lambda *a, **k: _raise_http(429))
    assert fd._latest_run("repo", "ci.yml", "tok") == (None, "ratelimit")


def test_latest_run_is_fail_soft_on_plain_oserror(monkeypatch):
    monkeypatch.setattr(
        fd.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(OSError("socket closed")),
    )
    assert fd._latest_run("repo", "ci.yml", "tok") == (None, "network")


def test_monitored_covers_av_health():
    # 迴歸釘子(2026-09-03):AV/health.yml 是該 repo 唯一的感測器,repo 刻意不放通知
    # secret,由本名單讀 run 結論補最後一哩;cadence 貼著它的週一排程。
    by_key = {(repo, wf): cadence for repo, wf, _name, cadence in fd.MONITORED}
    assert ("AV", "health.yml") in by_key, (
        "AV health 不在監控名單 —— 感測器紅了 owner 不會知道"
    )
    assert by_key[("AV", "health.yml")] == "weekly"
