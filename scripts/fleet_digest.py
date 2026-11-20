"""fleet-digest — Kai 後端每日一則「全 fleet cron 健康摘要」+ heartbeat。

為什麼要它:kai-notify 是 fail-soft 的 —— 通知器自己壞掉(token 被撤、Telegram
出事、action 被改壞)會**默默 skip**,Owner 收不到也不知道通知瞎了。這是「誰監視
監視者」的盲區。這支每天固定發一則摘要:

  - 有訊息 = 通知管線活著(heartbeat);哪天沒收到 = 管線死了,自己去查。
  - 內容 = 目前預設分支各 cron 的自然排程首輪結果。重跑或非排程補跑是補充，
    不會蓋掉自然狀態；GitHub 執行結果也不證明真的有產出或下游通知。

設計原則(對齊 kai_notify):
- fail-soft:讀不到某 repo 的 run 就標「?」,不中斷整份摘要,永不 raise。
- 純 stdlib:零依賴,直接 dogfood kai_notify.notify 送出。
- 誠實(a):PAT 失效/被限流讀不到一大片時,明說「只讀到 X/N」,絕不假裝全綠
  —— 監視器的信用全靠它從不假性安心。
- 有記憶(b):把每天各條的判讀存進 repo 內 state/fleet_history.json(git 是既有
  據點),讓摘要報「連 N 天失敗 / 今天第一次 / 連續 N 天全綠」,而非無記憶快照。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from typing import NamedTuple

# 讓本檔能 import 同 repo 的 kai_notify 核心(和 action_send.py 同款路徑注入)。
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
)
from kai_notify import notify

OWNER = "pei760730"
_API = "https://api.github.com/repos/{owner}/{repo}/actions/workflows/{wf}/runs"
_API_ATTEMPT = (
    "https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/1"
)
# 一次列一個 repo 的所有 workflow(拿 state 欄),比每條各問一次省呼叫 —— 監測名單
# 裡好幾個 repo 有多條 cron。
_WORKFLOW_PAGE_SIZE = 100
_API_WORKFLOWS = (
    "https://api.github.com/repos/{owner}/{repo}/actions/workflows"
    f"?per_page={_WORKFLOW_PAGE_SIZE}"
)
_API_REPO = "https://api.github.com/repos/{owner}/{repo}"
_TIMEOUT = 15
# Internal polling budget: leave 120s of the 10-minute job for notification
# (its own retry budget is 25s), setup and persistence. This only bounds query
# admission: socket timeouts and time spent before this script are not an
# absolute OS/job deadline guarantee. Keep the existing per-request timeout.
_POLL_BUDGET = 480.0

# 監測名單:(repo, workflow 檔, 顯示名, 節奏)。節奏決定「多久沒跑算 stale」。
# frequent 仍有 2h 新鮮度守門;collector 的高頻 cron 實際配給另用專屬節奏。
MONITORED = [
    ("benchmark-radar", "radar-watchdog.yml", "radar watchdog", "daily"),
    # 2026-09-07 移除 ig-insights-sync 的兩筆(sync.yml / token-refresh.yml):
    # 該 repo 已 archived(實測 `archived:true`,最後 push 2026-09-04)。archived repo
    # 是唯讀的、GitHub 停止派送排程,而且**不會把 workflow 的 state 改成 disabled_***
    # —— token-refresh.yml 至今仍回報 `active`。它的最後一次 run 是 08-30T06:06Z,
    # weekly 門檻 8 天在 09-07T06:06Z 越線,當天的 digest 剛好 00:49 跑、差 6 小時
    # 擦身而過,隔天早上就會開始每天喊一次假 ⏰。監控一個結構上不可能再跑的排程
    # 沒有意義。同批加上 _repo_archived 判讀,讓下一個被封存的 repo 是「被報出來」
    # 而不是「靜靜地變成假警報」。
    ("voc", "daily.yml", "voc daily", "daily"),
    ("TeaBus-VOC", "daily.yml", "TeaBus-VOC daily", "daily"),
    ("th-ops", "remind.yml", "th-ops remind", "daily"),
    ("th-ops", "expo-pull.yml", "th-ops expo-pull", "monthly"),
    # 兩條月度 cron 補進名單(2026-07-11):蓋掉「紅了沒人看見」實測盲區 ——
    # gdrive-organizer 7/1 排程紅了 9 天無人知;th-customs-scan 6/25 崩掉整月沒發現。
    ("th-customs-scan", "scan.yml", "th-customs 月掃", "monthly"),
    # 2026-08-23 更正節奏:這條 cron 是 `0 1 1 1,3,5,7,9,11 *`(單數月 1 號)= 雙月,
    # 不是每月;PR #26 在 gdrive 端改頻率時這裡沒跟著改。用 monthly 的 32 天門檻判它,
    # 每個 62 天週期會有 30 天被誤判成 ⏰ stale —— 最近一次 run 是 7/25(手動 dispatch),
    # 7/25+32=8/26 就會開始喊,而下次 cron 是 9/1。檔名 monthly-drive-audit.yml 是
    # 刻意不改的歷史名(避免斷引用),別拿檔名當節奏依據。
    ("gdrive-organizer", "monthly-drive-audit.yml", "gdrive 雙月審", "bi-monthly"),
    ("style-superman", "health.yml", "style-superman health", "weekly"),
    # 2026-09-30 移除 GOLD / KaiOS 的 adoption-metrics.yml 兩筆:兩個 repo 的預設分支
    # 都已沒有這支檔案(API `workflows/adoption-metrics.yml` 回 state=`deleted`,
    # workflow 清單只剩 engine-version-check / rules-lint 等),名單指著不存在的檔案,
    # digest 只能每天報一行「名單過期」的 off(fleet_history 已連 7 天 off)。
    # 只移除「確認已不存在」的引用;沒有新增任何監控(是否改盯 engine-version-check
    # 是 owner 的 ownership 決定,不在此處夾帶)。
    ("KaiOS-ContentSystem", "ig-sheet-sync.yml", "KaiOS ig-sync", "daily"),
    # #9 三併一(2026-07-15):short-video-bot 改名 collector,clip-collector/feed-collector
    # 併入後 archive。三條收集線=同一條 collect.yml 的三個 matrix job(fail-fast:false,
    # 任一 job 紅→run 紅),一筆監控即全覆蓋;舊三筆會對 archived repo 報假 stale。
    ("collector", "collect.yml", "collector collect(voc/tbvoc/of)", "collector-drain"),
    # 2026-07-26 補：本 repo 原本只監控 media-sorter 的 ytdlp 週檢，沒監控真正的
    # 下載佇列 collector.yml。結果 2026-07-02～07-26 那條管線因 OAuth token 失效
    # 靜默死 24 天，而看門狗「全綠」——它從一開始就沒被指派去看那裡。這是本次
    # 名單缺口的直接證據，不是泛化的「多監控一點比較好」。
    #
    # 2026-09-07 換靶不撤哨：`collector.yml` 與 `ytdlp-weekly-check.yml` 都已從
    # media-sorter 刪除(下載改走 GAS webhook relay,最後一次 collector run 是
    # 08-29),實測該 repo 現存 workflow 只剩 backlog-watch / botinfo / ci。名單還
    # 指著兩個不存在的檔案 → digest 把「檔案不存在」報成 ⏰ 該跑沒跑,連 12～21 天,
    # 「後端一切正常」整整 13 天不可能出現。**但上面那個 24 天靜默死的教訓沒有過期**
    # ——所以不是刪掉了事,是把哨兵改指向現在真正在看佇列健康的 backlog-watch.yml
    # (cron `23 */6 * * *`,每 6 小時;用 daily 的 26 小時門檻當上限)。
    ("media-sorter", "backlog-watch.yml", "media-sorter 佇列看門狗", "daily"),
    # daily-brief 是 owner 每天早上真的會讀的東西；斷了只靠「今天沒收到」的缺席
    # 訊號察覺，而缺席訊號要人記得自己沒收到，太弱。
    ("last30days", "daily-brief.yml", "last30days 早報", "daily"),
    # 2026-07-31 補兩條(fleet 掃描實證的盲區,不是泛化加監控):
    # core-bump 是 reusable caller,那端 bump job timeout 30 分,逾時=cancelled 而非
    # failure → 被呼叫端的 if:failure() 通知免疫;caller 端已加 notify-cancelled
    # (collector PR #91),這裡是第二層——它先前正好也不在本名單,兩層盲區疊好疊滿。
    ("collector", "core-bump.yml", "collector core-bump(依賴升版)", "daily"),
    # 2026-09-02 補(帶具體事件重開封版,非泛化加監控):fitbit 健康金庫的 freshness.yml
    # 是該 repo 唯一的雲端看門狗(攝入跑本機排程、雲端只判新鮮度),它紅了只停在 Actions
    # 頁 —— 09-01 就紅過兩次(缺 8 月月報),owner 收到零則。該 repo 沒放通知 secret
    # (健康數據 repo 刻意少放憑證),走本名單不需要任何 secret:digest 只拿 run 的
    # conclusion + URL、不讀內容,健康數據不出信任圈。private repo:FLEET_READ_TOKEN
    # 讀不到會印 ?、不會假綠(誠實(a))。
    ("fitbit", "freshness.yml", "fitbit 新鮮度", "daily"),
    # 2026-09-03 補(帶具體事件重開封版):AV 的 health.yml 是該 repo 第一顆感測器 ——
    # 之前四層憲法的迴圈只在對話開著時轉,臉表 artifact 陳舊三個月、離過期 6 天沒人知道。
    # 該 repo 只有 NOTION_API_TOKEN、刻意不放通知 secret(不裝 fail-soft 的假通知線),
    # 走本名單讀 run 結論補最後一哩;cadence 貼著它的週一排程。
    ("AV", "health.yml", "AV health", "weekly"),
]

# 各節奏的「該多久內要有一次 run」上限;超過視為 stale(cron 沒排到/壞了)。
_STALE_AFTER = {
    "frequent": timedelta(hours=2),
    # collector/collect.yml 已記錄 p99 9.9h、最長 11.2h 的實際排程間隔。
    # 對齊 heartbeat.yml 的 18h 人工告警線,不把正常配給延遲報成故障。
    "collector-drain": timedelta(hours=18),
    "daily": timedelta(hours=26),
    "weekly": timedelta(days=8),
    "monthly": timedelta(days=32),
    # 單數月 1 號那種雙月 cron,最長一段是 7/1→9/1 的 62 天(含 31+31);63 給一天餘裕。
    "bi-monthly": timedelta(days=63),
}


# 歷史:每條 cron 存最近 N 天的判讀(oldest..newest),讓 digest 報「連 N 天失敗」
# 而非無記憶快照。存成 repo 內一個 committed JSON(git 是既有據點,不新增平台)。
_HISTORY_LEN = 7
_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "state",
    "fleet_history.json",
)


def _load_history() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_history(hist: dict) -> None:
    # fail-soft:存不了歷史不影響今天已送出的通知。
    try:
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        with open(_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError:
        pass


def _streak_suffix(prior: list, today_kind: str) -> str:
    """prior=該條過去幾天的 kind(oldest..newest,不含今天)。回『(連 N 天)』/
    『(今天第一次)』/空,讓 Kai 一眼分辨『慢性病 vs 新問題』。"""
    if not prior:
        return ""
    if prior[-1] == "pending":
        previous = _before_pending(prior)
        if previous == today_kind:
            return "(上次確認也是這樣)"
        return ""
    if prior[-1] != today_kind:
        return "(今天第一次)"
    n = 1
    for k in reversed(prior):
        if k == today_kind:
            n += 1
        else:
            break
    return f"(連 {n} 天)" if n >= 2 else ""


# 判讀趨勢(反覆 / 連 N 天)時要忽略的 kind:它們都不是「這條在健康與壞掉之間擺盪」
# 的證據 —— unknown 是我讀不到,off 是它根本沒在跑,pending 是結果尚未確認。
_NEUTRAL_KINDS = frozenset({"unknown", "off", "pending"})


def _transitions(kinds: list) -> int:
    """Count healthy<->not-healthy flips across a kind sequence (oldest..newest).
    'unknown', 'off' and 'pending' are neutral —
    neither starts nor breaks a flip run, so a spell of unreadable days, or a
    stretch where the workflow was switched off, doesn't masquerade as
    instability."""
    flips = 0
    prev_ok: bool | None = None
    for k in kinds:
        if k in _NEUTRAL_KINDS:
            continue
        is_ok = k == "ok"
        if prev_ok is not None and is_ok != prev_ok:
            flips += 1
        prev_ok = is_ok
    return flips


def _is_flapping(prior: list, today_kind: str) -> bool:
    """Flapping = genuine oscillation over the recent window (prior + today):
    >= 3 healthy<->broken flips. One clean incident (ok..fail..ok = 2 flips)
    is NOT flapping; ok/fail/ok/fail (3 flips) is. Needs >= 4 real samples so a
    short history can't false-positive — this is the chronic-instability signal
    the consecutive-streak suffix structurally can't see."""
    window = [k for k in (prior + [today_kind]) if k not in _NEUTRAL_KINDS]
    return len(window) >= 4 and _transitions(window) >= 3


def _before_pending(prior: list) -> str | None:
    return next((kind for kind in reversed(prior) if kind != "pending"), None)


def _recovered(prior: list, today_kind: str) -> bool:
    """A pending sample is not recovery; look past it once success is confirmed.

    Unknown or switched-off history still does not imply a confirmed recovery.
    """
    return today_kind == "ok" and _before_pending(prior) in ("fail", "stale")


def _read_json(url: str, token: str) -> tuple[dict | None, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data, None) if isinstance(data, dict) else (None, "invalid")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            if exc.headers and exc.headers.get("x-ratelimit-remaining") == "0":
                return (None, "ratelimit")
            return (None, "auth")
        if exc.code == 429:
            return (None, "ratelimit")
        return (None, "http")
    except (OSError, ValueError):
        return (None, "network")


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _matches_run(run: object, event: str, branch: str) -> bool:
    return (
        isinstance(run, dict)
        and run.get("event") == event
        and run.get("head_branch") == branch
        and _positive_int(run.get("id"))
        and _positive_int(run.get("run_attempt"))
    )


def _latest_run(
    repo: str, wf: str, token: str, event: str, branch: str
) -> tuple[dict | None, str | None]:
    """Fetch only the newest run for an explicit event and current default branch."""
    query = urllib.parse.urlencode({"per_page": 1, "event": event, "branch": branch})
    url = _API.format(owner=OWNER, repo=repo, wf=wf)
    data, err = _read_json(f"{url}?{query}", token)
    if err is not None or data is None:
        return (None, err)
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        return (None, "invalid")
    if not runs:
        return (None, None)
    run = runs[0]
    return (run, None) if _matches_run(run, event, branch) else (None, "invalid")


def _first_attempt(repo: str, run: dict, token: str, branch: str) -> dict | None:
    run_id = run["id"]
    url = _API_ATTEMPT.format(owner=OWNER, repo=repo, run_id=run_id)
    first, _ = _read_json(url, token)
    if (
        first is not None
        and _matches_run(first, "schedule", branch)
        and first["id"] == run_id
        and first["run_attempt"] == 1
    ):
        return first
    return None


class _WorkflowListing(NamedTuple):
    states: dict[str, str]
    complete: bool

    def observation(self, name: str) -> tuple[bool | None, str | None]:
        if name in self.states:
            return True, self.states[name]
        return (False if self.complete else None), None


def _workflow_states(repo: str, token: str) -> _WorkflowListing | None:
    """Keep known states; only a complete, verified page proves absence.

    Keep one bounded request. A truncated/malformed list is not evidence that
    a missing workflow was deleted; let its default-branch run lookup decide.
    """
    data, _ = _read_json(_API_WORKFLOWS.format(owner=OWNER, repo=repo), token)
    if data is None or not isinstance(data.get("workflows"), list):
        return None
    items = data["workflows"]
    states: dict[str, str] = {}
    duplicates = set()
    for wf in items:
        if not isinstance(wf, dict):
            continue
        path, state = wf.get("path"), wf.get("state")
        if not isinstance(path, str) or not isinstance(state, str) or not state:
            continue
        prefix = ".github/workflows/"
        if not path.startswith(prefix):
            continue
        name = path[len(prefix) :]
        if not name or "/" in name:
            continue
        if name in states:
            states.pop(name)
            duplicates.add(name)
        elif name not in duplicates:
            states[name] = state
    count = data.get("total_count")
    complete = (
        type(count) is int
        and count == len(items) == len(states)
        and count <= _WORKFLOW_PAGE_SIZE
    )
    return _WorkflowListing(states, complete)


def _repo_info(repo: str, token: str) -> tuple[bool | None, str | None, str | None]:
    """Get archive status and current default branch with the same metadata read."""
    data, err = _read_json(_API_REPO.format(owner=OWNER, repo=repo), token)
    if data is None:
        return (None, None, err)
    archived = data.get("archived")
    branch = data.get("default_branch")
    return (
        archived if isinstance(archived, bool) else None,
        branch if isinstance(branch, str) and branch.strip() else None,
        err,
    )


_CADENCE_HUMAN = {
    "frequent": "本來就該一直在跑",
    "collector-drain": "批次收集;6h 看守補觸發,18h 人工告警線",
    "daily": "平常每天要跑一次",
    "weekly": "平常每週跑一次",
    "monthly": "平常每月跑一次",
    "bi-monthly": "平常每兩個月跑一次",
}

# 節奏打錯字 / 用了沒定義的節奏 → 以前會靜靜退回 daily 的 26 小時門檻,對一條雙月
# cron 就是每輪喊 30 天假 stale。名單是人手維護的,所以在載入時就叫,不留給執行期。
_unknown_cadences = sorted(
    {c for _, _, _, c in MONITORED} - (_STALE_AFTER.keys() & _CADENCE_HUMAN.keys())
)
if _unknown_cadences:
    raise RuntimeError(
        f"MONITORED 用了未定義的節奏:{_unknown_cadences};"
        f"請在 _STALE_AFTER 與 _CADENCE_HUMAN 同時補上"
    )


def _assess(
    name: str,
    cadence: str,
    run: dict | None,
    now: datetime,
    state: str | None = None,
    *,
    exists: bool | None = None,
    archived: bool | None = None,
) -> dict:
    """判讀一個 cron 的最近狀態，回白話結果。
    kind: ok(正常) / fail(跑失敗) / stale(該跑沒跑) / unknown(讀不到) /
          off(被人為停用，不算出事但一定要看得見)。
    只有 fail/stale/unknown 算「要人看」；running 視為沒事(它在動)。
    cancelled 算 fail：job 層 timeout-minutes 逾時被殺就落在 cancelled，不是 failure。

    `state` 是 workflow 本身的開關(見 _workflow_states)，必須先於 run 判讀 ——
    停用的 workflow 不會再產生新 run，所以它「最後一次 run」的結論會被永遠凍結：
    2026-08-01 owner 裁示停掉 th-customs-scan/scan.yml(MOC API 對外關閉、每月燒
    runner 到逾時)，那條的最後一次 run 是 07-25 的 cancelled，於是本 digest 從此
    每天回報「❌ th-customs 月掃 — 被中止(連 N 天)」，天數還會一直長。它沒有壞，
    是被關掉了；把「關掉」講成「壞掉」的看門狗，等於每天都在喊一次狼來了。

    `archived` / `exists` 是同一個道理再往外推兩層(2026-09-07，帶具體事件重開封版)：
    真事故 —— media-sorter 的 `collector.yml` 與 `ytdlp-weekly-check.yml` 已從 repo
    刪除(改走 GAS webhook relay)，但本名單還指著它們，於是 digest 連 12～21 天把
    「這個檔案不存在」報成 ⏰ 該跑沒跑，「後端一切正常」整整 13 天不可能出現。
    同期 ig-insights-sync 被 archived，GitHub 停止派送它的排程卻**不改 workflow 的
    state**(實測 token-refresh.yml 仍是 `active`)，所以 disabled_* 那層接不住它。
    兩者都不是「壞掉」，是「不該再被監控」——判成 off，不進「要你看一下」，但依
    honesty 不變式每天照列一行，逼人去修名單。

    ⚠ `exists=None` 與 `exists=False` 是相反的兩件事：None＝我沒讀到清單(退回
    run 判讀)，False＝我讀到了、裡面沒有它。把讀取失敗當成「檔案不見了」會讓一次
    網路抖動把全 fleet 報成消失。"""
    if archived is True:
        return {
            "kind": "off",
            "name": name,
            "detail": (
                "這個 repo 已封存(archived)——GitHub 不會再派送它的排程，"
                "而且不會把 workflow 改成停用狀態。它不是壞掉，是不該再被監控："
                "請把它從 kai-notify 的 MONITORED 名單移掉"
            ),
            "url": "",
        }
    if exists is False:
        return {
            "kind": "off",
            "name": name,
            "detail": (
                "這條 workflow 已經不在 repo 裡了(被刪掉或改名)——"
                "監控名單指著一個不存在的檔案。它不是壞掉，是名單過期了："
                "請更新 kai-notify 的 MONITORED"
            ),
            "url": "",
        }
    if state == "disabled_manually":
        return {
            "kind": "off",
            "name": name,
            "detail": "這條被停用中（不是壞掉），重新啟用才會再跑",
            "url": "",
        }
    if state == "disabled_inactivity":
        # GitHub 對「repo 60 天沒動靜」的排程會自動關掉,而且不會通知任何人 ——
        # 這是真的靜默死亡,跟人為停用是相反的兩件事,不可以併在一起報。
        return {
            "kind": "fail",
            "name": name,
            "detail": (
                "被 GitHub 自動停用了（repo 太久沒動靜的排程會被自動關掉，"
                "而且不會通知）——要手動重新啟用它才會再跑"
            ),
            "url": "",
        }
    if run is None:
        return {
            "kind": "unknown",
            "name": name,
            "detail": "讀不到它的狀態（權限或 workflow 名字對不上？）",
            "url": "",
        }
    concl = run.get("conclusion")  # success/failure/cancelled/timed_out/None(進行中)
    url = _run_link(run)
    age = _run_age(run, now)
    ago = _humanize(age) if age is not None else "?"

    if concl == "failure":
        return {
            "kind": "fail",
            "name": name,
            "detail": f"{ago}前那次跑失敗了",
            "url": url,
        }

    # cancelled 不是「沒事」。job 層 timeout-minutes 逾時被殺，conclusion 就是
    # cancelled 而不是 failure —— 一條每 10 分鐘跑的下載佇列卡住整整逾時，跟它
    # 跑失敗對 owner 是同一件事：這輪沒做完工作。把 cancelled 併進「沒事」的原始
    # 寫法，正是 media-sorter 管線 2026-07-02～07-26 靜默死 24 天那類事故的同款盲區
    # （job 層 timeout → cancelled → 監控說沒事）。措辭刻意區分成「被中止」而非
    # 「失敗」，因為人為按停也會落在這裡，owner 要能一眼分辨。
    if concl == "cancelled":
        return {
            "kind": "fail",
            "name": name,
            "detail": f"{ago}前那次被中止（逾時或人為按停），這輪的工作沒做完",
            "url": url,
        }

    # GitHub 的明確 timed_out 結論也代表這輪沒做完,不能落入 fresh → ok。
    if concl == "timed_out":
        return {
            "kind": "fail",
            "name": name,
            "detail": f"{ago}前那次執行逾時，這輪的工作沒做完",
            "url": url,
        }

    if concl == "action_required":
        return {
            "kind": "fail",
            "name": name,
            "detail": f"{ago}前那次需要人工處理，請打開執行紀錄查看下一步",
            "url": url,
        }

    stale = age is not None and age > _STALE_AFTER.get(cadence, timedelta(hours=26))
    if stale:
        return {
            "kind": "stale",
            "name": name,
            "detail": f"已經 {ago} 沒動靜了（{_CADENCE_HUMAN.get(cadence, '')}），排程可能卡住",
            "url": url,
        }

    # success / 進行中 當「沒事」——它有在動就好。cancelled / timed_out 已在上面攔掉。
    return {"kind": "ok", "name": name, "detail": f"{ago}前跑過", "url": url}


def _run_time(run: dict, field: str = "created_at") -> datetime | None:
    try:
        ts = datetime.fromisoformat(run.get(field, ""))  # Python 3.11+ accepts Z
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo is not None else None


def _run_age(run: dict, now: datetime, field: str = "created_at") -> timedelta | None:
    ts = _run_time(run, field)
    return now - ts if ts is not None and ts <= now else None


_BUSY = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
_BAD = frozenset({"failure", "cancelled", "timed_out", "action_required"})


def _run_link(run: dict) -> str:
    value = run.get("html_url")
    return value if isinstance(value, str) else ""


def _busy_run(run: dict) -> bool:
    status = run.get("status")
    return run.get("conclusion") is None and isinstance(status, str) and status in _BUSY


def _clean_success(run: dict) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") == "success"


def _scheduled_assessment(
    a: dict, initial: dict | None, err: str | None, now: datetime, cadence: str
) -> None:
    """Translate only the natural attempt for history; do not assert work output."""
    if a["kind"] == "off" or initial is None and a["kind"] == "fail":
        return
    if initial is None:
        if err == "first-attempt":
            a["detail"] = "有自然排程重跑，但首輪結果未能核對"
        elif err:
            a["detail"] = "無法核對目前預設分支的自然排程"
        else:
            a["detail"] = "查不到目前預設分支的自然排程紀錄，不能確認有跑"
        return
    age = _run_age(initial, now)
    if a["kind"] != "ok":
        a["detail"] = f"自然排程：{a['detail']}"
        if a["kind"] == "fail" and age is not None and age > _STALE_AFTER[cadence]:
            a["detail"] += "；也已超過應到的判斷窗"
        return
    if age is None:
        a.update(kind="unknown", detail="自然排程時間無法確認")
    elif _clean_success(initial):
        a["detail"] = "自然排程首輪回報成功"
    elif _busy_run(initial):
        # A timely run is not a failure, but it is not a successful outcome.
        a.update(kind="pending", detail="自然排程已建立，仍未有完成結果")
    else:
        a.update(kind="unknown", detail="自然排程有紀錄，但無法確認成功或進行中")


def _extra_run_detail(run: dict, label: str) -> str:
    if _clean_success(run):
        return f"{label}回報成功（未驗證產出）"
    conclusion = run.get("conclusion")
    if isinstance(conclusion, str) and conclusion in _BAD:
        return f"{label}未成功"
    if _busy_run(run):
        return f"{label}已建立，結果未定"
    return f"{label}結果未能確認"


def _recent_dispatch(
    dispatch: dict, scheduled: dict | None, cadence: str, now: datetime
) -> bool:
    age = _run_age(dispatch, now)
    if age is None or age > _STALE_AFTER[cadence]:
        return False
    if scheduled is None:
        return True
    first, later = _run_time(scheduled), _run_time(dispatch)
    return first is not None and later is not None and later > first


def _humanize(delta: timedelta) -> str:
    m = int(delta.total_seconds() // 60)
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h"
    return f"{h // 24}d"


_ICON = {"fail": "❌", "stale": "⏰", "unknown": "❓"}


def main() -> int:
    token = (os.environ.get("FLEET_READ_TOKEN") or "").strip()
    if not token:
        # 沒 PAT 就讀不到各 repo;老實說,免得「digest 靜悄悄」被誤當一切正常。
        sent = notify(
            "🌅 早安 Kai — 我暫時看不到各 repo 的狀態（FLEET_READ_TOKEN 沒設或失效）。"
            "這則至少證明通知本身還活著,但 fleet 健康我這輪報不了。"
        )
        print(
            "fleet-digest: no FLEET_READ_TOKEN;",
            "sent degraded heartbeat."
            if sent
            else "degraded heartbeat skipped/failed (fail-soft).",
        )
        return 0

    deadline = time.monotonic() + _POLL_BUDGET

    def _can_poll() -> bool:
        return deadline - time.monotonic() >= _TIMEOUT

    def _stop_polling() -> bool:
        # Reserve a full request timeout before starting another serial lookup.
        if _can_poll():
            return False
        sent = notify(
            "🌅 早安 Kai — ⚠️ 這輪掃描未完成（讀取耗時超出預算），"
            "fleet 健康報不準，先別當全綠。歷史保留上一輪，不寫入這次的部分結果。"
            "\n\n— 每天早上這一則;哪天沒來,就是通知管線自己掛了 🫥"
        )
        print(
            "fleet-digest: incomplete scan (history untouched);",
            "sent." if sent else "skipped/failed (fail-soft).",
        )
        return True

    # One workflow list and one repo metadata call per repo, as before.
    states = {}
    info = {}
    for repo in dict.fromkeys(r for r, _wf, _n, _c in MONITORED):
        if _stop_polling():
            return 0
        states[repo] = _workflow_states(repo, token)
        if _stop_polling():
            return 0
        info[repo] = _repo_info(repo, token)

    reads = []
    for repo, wf, name, cadence in MONITORED:
        repo_states = states[repo]
        exists, state = (
            (None, None) if repo_states is None else repo_states.observation(wf)
        )
        archived, branch, metadata_error = info[repo]
        if (
            archived is True
            or exists is False
            or state in ("disabled_manually", "disabled_inactivity")
        ):
            run, err = None, None
        elif branch is None:
            run, err = None, metadata_error or "branch"
        else:
            if _stop_polling():
                return 0
            run, err = _latest_run(repo, wf, token, "schedule", branch)
        reads.append(
            (repo, wf, name, cadence, err, run, state, exists, archived, branch)
        )

    # (a) 誠實:一大片讀取因 PAT/限流失敗 = 我根本沒法驗健康,絕不能假裝全綠。
    blind = [r for r in reads if r[4] in ("auth", "ratelimit")]
    if len(blind) >= max(3, len(reads) // 2):
        reason = "PAT 可能失效" if any(r[4] == "auth" for r in blind) else "被限流"
        sent = notify(
            f"🌅 早安 Kai — ⚠️ 這輪我只讀到 {len(reads) - len(blind)}/{len(reads)} 條"
            f"（{reason}),fleet 健康報不準,先別當全綠 —— 去看一下 kai-notify 的 "
            f"FLEET_READ_TOKEN。\n\n— 每天早上這一則;哪天沒來,就是通知管線自己掛了 🫥"
        )
        print(
            "fleet-digest: degraded reads;",
            "sent honest warning (history untouched)."
            if sent
            else "warning skipped/failed (fail-soft) (history untouched).",
        )
        return 0  # 不更新歷史,別讓一次 token 中斷洗掉連續紀錄

    history = _load_history()
    assessed = []
    # Gather the mandatory natural lists first; optional evidence never crowds
    # them out. Give first-attempt evidence priority over separate dispatches.
    originals = []
    for repo, wf, name, cadence, err, latest, state, exists, archived, branch in reads:
        initial = latest
        rerun = latest is not None and latest["run_attempt"] > 1
        if rerun:
            initial = (
                _first_attempt(repo, latest, token, branch) if _can_poll() else None
            )
            if initial is None:
                err = "first-attempt"
        originals.append((initial, err, rerun))

    for read, original in zip(reads, originals, strict=True):
        repo, wf, name, cadence, _, latest, state, exists, archived, branch = read
        initial, err, rerun = original
        now = datetime.now(UTC)
        a = _assess(
            name, cadence, initial, now, state, exists=exists, archived=archived
        )
        _scheduled_assessment(a, initial, err, now, cadence)
        extras = []
        if rerun and (a["kind"] != "ok" or not _clean_success(latest)):
            attempt = latest["run_attempt"]
            extras.append(
                (_extra_run_detail(latest, f"同次排程第 {attempt} 次重跑"), "")
            )
        if (
            a["kind"] in ("fail", "stale", "unknown")
            and archived is not True
            and exists is not False
            and state not in ("disabled_manually", "disabled_inactivity")
            and err not in ("auth", "ratelimit")
            and branch is not None
            and _can_poll()
        ):
            dispatch, _ = _latest_run(repo, wf, token, "workflow_dispatch", branch)
            if dispatch is not None and _recent_dispatch(
                dispatch, latest, cadence, datetime.now(UTC)
            ):
                extras.append(
                    (
                        _extra_run_detail(dispatch, "預設分支非排程補跑"),
                        _run_link(dispatch),
                    )
                )
        key = f"{repo}/{wf}"
        a.update(key=key, extras=extras)
        prior = history.get(key, [])
        a["recovered"] = _recovered(prior, a["kind"])
        a["flapping"] = _is_flapping(prior, a["kind"])
        assessed.append(a)
    now = datetime.now(UTC)
    # 「被停用」不是出事,不進「要你看一下」的計數 —— 但也絕不消失:它在下面的
    # 「另外」區塊每天照列一行。看門狗可以不喊狼,不可以假裝那條 cron 不存在。
    problems = [a for a in assessed if a["kind"] not in ("ok", "off", "pending")]
    pending = [a for a in assessed if a["kind"] == "pending"]
    switched_off = [a for a in assessed if a["kind"] == "off"]

    # 就算現在是綠的,這兩件也值得單獨講一行(否則會被「其他都正常」吞掉):
    #  · 恢復:昨天還在鬧、今天好了 —— 一句報喜,也證明問題真的解了。
    #  · 反覆:時好時壞的慢性不穩定 —— 現在剛好綠,但別當它沒事。
    recoveries = [a for a in assessed if a["kind"] == "ok" and a["recovered"]]
    flaps_ok = [
        a
        for a in assessed
        if a["kind"] == "ok" and a["flapping"] and not a["recovered"]
    ]

    def _highlights() -> list[str]:
        lines = []
        for a in recoveries:
            tail = "（不過這幾天一直反覆,還是盯著)" if a["flapping"] else ""
            lines.append(f"✅ {a['name']} — 之前在鬧,今天恢復了{tail}")
        for a in flaps_ok:
            lines.append(
                f"🔀 {a['name']} — 這幾天時好時壞、一直反覆,現在剛好是綠的但值得盯"
            )
        for a in assessed:
            if a["kind"] == "pending":
                lines.append(f"⏳ {a['name']} — {a['detail']}")
            if a["kind"] in ("ok", "pending"):
                for detail, _ in a["extras"]:
                    lines.append(f"↪ {a['name']} — {detail}")
        for a in switched_off:
            lines.append(f"🔕 {a['name']} — {a['detail']}")
        return lines

    if not problems:
        highlights = _highlights()
        if pending:
            msg = (
                f"🌅 早安 Kai — 有 {len(pending)} 個自然排程尚未完成 👇\n\n"
                + "\n".join(highlights)
            )
        elif highlights:
            # 沒出事,但有恢復/反覆要讓他知道 —— 不是純報平安。
            msg = "🌅 早安 Kai — 自然排程首輪未見異常，有幾項附註 👇\n\n" + "\n".join(
                highlights
            )
        else:
            # 全好:一句讓他放心。若連續多天全綠,順帶報個信心。
            prior_fleet = history.get("_fleet", [])
            green = 1
            for k in reversed(prior_fleet):
                if k == "green":
                    green += 1
                else:
                    break
            streak = f"（連續 {green} 天未見排程警訊）" if green >= 3 else ""
            msg = (
                f"🌅 早安 Kai — 自然排程紀錄未見異常 😌{streak}\n\n"
                f"{len(assessed)} 個自然排程首輪回報成功。"
            )
    else:
        # (b) 有記憶:每條問題帶「連 N 天 / 今天第一次」,分辨慢性 vs 新問題;
        #     再帶「時好時壞」標籤,分辨慢性穩定壞 vs 間歇性不穩。
        blocks = []
        for a in problems:
            suffix = _streak_suffix(history.get(a["key"], []), a["kind"])
            head = f"{_ICON.get(a['kind'], '⚠️')} {a['name']} — {a['detail']}"
            if suffix:
                head += f" {suffix}"
            if a["flapping"]:
                head += " 🔀時好時壞"
            if a["url"]:
                head += f"\n   👉 {a['url']}"
            for detail, link in a["extras"]:
                head += f"\n   ↪ {detail}"
                if link:
                    head += f" ({link})"
            blocks.append(head)
        ok_n = sum(a["kind"] == "ok" for a in assessed)
        msg = f"🌅 早安 Kai — 有 {len(problems)} 個要你看一下 👇\n\n" + "\n\n".join(
            blocks
        )
        highlights = _highlights()
        if highlights:
            msg += "\n\n另外：\n" + "\n".join(highlights)
        if ok_n:
            msg += f"\n\n其他 {ok_n} 個自然排程未見異常 ✅"

    # heartbeat 提醒:低調一行,但要在——這則的意義就是「沒收到=通知線斷了」。
    msg += "\n\n只判讀 GitHub 執行紀錄，未核對實際工作產出或下游通知。"
    msg += "\n\n— 每天早上這一則;哪天沒來,就是通知管線自己掛了 🫥"
    sent = notify(msg)

    # Taipei 日只由自然 digest 首輪寫入一次；手動或重跑仍可出報告，
    # 但不搶自然趨勢。pending 是一個非結論樣本，不算成功也不算失敗。
    today = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    if (
        os.environ.get("GITHUB_EVENT_NAME") == "schedule"
        and os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
        and history.get("_last_date") != today
    ):
        for a in assessed:
            history[a["key"]] = (history.get(a["key"], []) + [a["kind"]])[
                -_HISTORY_LEN:
            ]
        fleet_kind = "problem" if problems else "pending" if pending else "green"
        history["_fleet"] = (history.get("_fleet", []) + [fleet_kind])[-_HISTORY_LEN:]
        history["_last_date"] = today
        _save_history(history)

    print("fleet-digest:", "sent." if sent else "skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
