"""fleet-digest — Kai 後端每日一則「全 fleet cron 健康摘要」+ heartbeat。

為什麼要它:kai-notify 是 fail-soft 的 —— 通知器自己壞掉(token 被撤、Telegram
出事、action 被改壞)會**默默 skip**,Owner 收不到也不知道通知瞎了。這是「誰監視
監視者」的盲區。這支每天固定發一則摘要:

  - 有訊息 = 通知管線活著(heartbeat);哪天沒收到 = 管線死了,自己去查。
  - 內容 = 各 report cron 最近一次跑的結論,把原本每天 9 則「· success」噪音
    收斂成一則有用摘要,失敗仍由各 cron 即時(if: failure())響、不靠這裡。

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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# 讓本檔能 import 同 repo 的 kai_notify 核心(和 action_send.py 同款路徑注入)。
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
)
from kai_notify import notify

OWNER = "pei760730"
_API = (
    "https://api.github.com/repos/{owner}/{repo}/actions/workflows/{wf}/runs?per_page=1"
)
# 一次列一個 repo 的所有 workflow(拿 state 欄),比每條各問一次省呼叫 —— 監測名單
# 裡好幾個 repo 有多條 cron。
_API_WORKFLOWS = (
    "https://api.github.com/repos/{owner}/{repo}/actions/workflows?per_page=100"
)
_TIMEOUT = 15

# 監測名單:(repo, workflow 檔, 顯示名, 節奏)。節奏決定「多久沒跑算 stale」。
# frequent(每 5 分收集 bot)只看最近一次結論、不判 stale(隨時都在跑)。
MONITORED = [
    ("benchmark-radar", "radar-watchdog.yml", "radar watchdog", "daily"),
    ("ig-insights-sync", "sync.yml", "ig-insights sync", "daily"),
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
    ("media-sorter", "ytdlp-weekly-check.yml", "media-sorter ytdlp", "weekly"),
    ("GOLD-ContentSystem", "adoption-metrics.yml", "GOLD adoption", "weekly"),
    ("KaiOS-ContentSystem", "adoption-metrics.yml", "KaiOS adoption", "weekly"),
    ("KaiOS-ContentSystem", "ig-sheet-sync.yml", "KaiOS ig-sync", "daily"),
    # #9 三併一(2026-07-15):short-video-bot 改名 collector,clip-collector/feed-collector
    # 併入後 archive。三條收集線=同一條 collect.yml 的三個 matrix job(fail-fast:false,
    # 任一 job 紅→run 紅),一筆監控即全覆蓋;舊三筆會對 archived repo 報假 stale。
    ("collector", "collect.yml", "collector collect(voc/tbvoc/of)", "frequent"),
    # 2026-07-26 補：本 repo 原本只監控 media-sorter 的 ytdlp 週檢，沒監控真正的
    # 下載佇列 collector.yml。結果 2026-07-02～07-26 那條管線因 OAuth token 失效
    # 靜默死 24 天，而看門狗「全綠」——它從一開始就沒被指派去看那裡。這是本次
    # 名單缺口的直接證據，不是泛化的「多監控一點比較好」。
    ("media-sorter", "collector.yml", "media-sorter collector(下載佇列)", "frequent"),
    # daily-brief 是 owner 每天早上真的會讀的東西；斷了只靠「今天沒收到」的缺席
    # 訊號察覺，而缺席訊號要人記得自己沒收到，太弱。
    ("last30days", "daily-brief.yml", "last30days 早報", "daily"),
    # 2026-07-31 補兩條(fleet 掃描實證的盲區,不是泛化加監控):
    # core-bump 是 reusable caller,那端 bump job timeout 30 分,逾時=cancelled 而非
    # failure → 被呼叫端的 if:failure() 通知免疫;caller 端已加 notify-cancelled
    # (collector PR #91),這裡是第二層——它先前正好也不在本名單,兩層盲區疊好疊滿。
    ("collector", "core-bump.yml", "collector core-bump(依賴升版)", "daily"),
    # token 續期一週一次;斷掉不會當場痛,60 天後 token 到期整條 ig-insights 線才死,
    # 到時候誰都想不起來是哪一週的續期沒跑。cadence=weekly 讓 stale 判斷貼著排程。
    ("ig-insights-sync", "token-refresh.yml", "ig token 續期", "weekly"),
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
# 的證據 —— unknown 是我讀不到,off 是它根本沒在跑。
_NEUTRAL_KINDS = frozenset({"unknown", "off"})


def _transitions(kinds: list) -> int:
    """Count healthy<->not-healthy flips across a kind sequence (oldest..newest).
    'unknown' (couldn't read) and 'off' (deliberately disabled) are neutral —
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


def _recovered(prior: list, today_kind: str) -> bool:
    """True when today is healthy but yesterday it was actually broken
    (fail/stale) — a recovery worth one line. 'unknown -> ok' is just 'readable
    again', not a recovery, so it doesn't count."""
    return today_kind == "ok" and bool(prior) and prior[-1] in ("fail", "stale")


def _latest_run(repo: str, wf: str, token: str) -> tuple[dict | None, str | None]:
    """回傳 (最近一次 run 或 None, 錯誤種類 或 None)。
    err: None=讀到了; 'auth'=PAT 失效(401/403); 'ratelimit'=被限流;
    'http'/'network'=其他讀取失敗。分辨這個,digest 才不會把『我根本讀不到』
    誤當『這條沒跑』而假裝全綠。"""
    req = urllib.request.Request(
        _API.format(owner=OWNER, repo=repo, wf=wf),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        runs = data.get("workflow_runs") or []
        return (runs[0] if runs else None, None)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # GitHub 用 403 + x-ratelimit-remaining:0 表示限流,和純授權失敗分開。
            if exc.headers.get("x-ratelimit-remaining") == "0":
                return (None, "ratelimit")
            return (None, "auth")
        if exc.code == 429:
            return (None, "ratelimit")
        return (None, "http")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return (None, "network")


def _workflow_states(repo: str, token: str) -> dict:
    """{workflow 檔名: state} —— state 是 GitHub 對這條 workflow 本身的開關狀態
    (`active` / `disabled_manually` / `disabled_inactivity`),跟「最近一次 run 的
    結論」是兩回事。

    讀不到就回空 dict:這層是補充判讀,失敗只能讓判讀退回原本的行為,
    絕不能讓整份摘要死掉(fail-soft 是本 repo 地基)。"""
    req = urllib.request.Request(
        _API_WORKFLOWS.format(owner=OWNER, repo=repo),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {}
    out = {}
    for wf in data.get("workflows") or []:
        path = wf.get("path") or ""
        state = wf.get("state")
        if path and state:
            out[path.rsplit("/", 1)[-1]] = state
    return out


_CADENCE_HUMAN = {
    "frequent": "本來就該一直在跑",
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
    是被關掉了；把「關掉」講成「壞掉」的看門狗，等於每天都在喊一次狼來了。"""
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
    concl = run.get("conclusion")  # success/failure/cancelled/None(進行中)
    url = run.get("html_url", "")
    try:
        ts = datetime.fromisoformat(run.get("created_at", "").replace("Z", "+00:00"))
        age = now - ts
        ago = _humanize(age)
    except ValueError:
        age, ago = None, "?"

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

    stale = age is not None and age > _STALE_AFTER.get(cadence, timedelta(hours=26))
    if stale:
        return {
            "kind": "stale",
            "name": name,
            "detail": f"已經 {ago} 沒動靜了（{_CADENCE_HUMAN.get(cadence, '')}），排程可能卡住",
            "url": url,
        }

    # success / 進行中 當「沒事」——它有在動就好。cancelled 已在上面攔掉。
    return {"kind": "ok", "name": name, "detail": f"{ago}前跑過", "url": url}


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
        notify(
            "🌅 早安 Kai — 我暫時看不到各 repo 的狀態（FLEET_READ_TOKEN 沒設或失效）。"
            "這則至少證明通知本身還活著,但 fleet 健康我這輪報不了。"
        )
        print("fleet-digest: no FLEET_READ_TOKEN; sent degraded heartbeat.")
        return 0

    now = datetime.now(timezone.utc)
    # 每個 repo 只問一次 workflow 清單(拿開關狀態),同 repo 的多條 cron 共用。
    states: dict = {}
    for repo in dict.fromkeys(r for r, _wf, _n, _c in MONITORED):
        states[repo] = _workflow_states(repo, token)

    reads = []  # (key, name, cadence, run, err, state)
    for repo, wf, name, cadence in MONITORED:
        run, err = _latest_run(repo, wf, token)
        reads.append(
            (f"{repo}/{wf}", name, cadence, run, err, states.get(repo, {}).get(wf))
        )

    # (a) 誠實:一大片讀取因 PAT/限流失敗 = 我根本沒法驗健康,絕不能假裝全綠。
    blind = [r for r in reads if r[4] in ("auth", "ratelimit")]
    if len(blind) >= max(3, len(reads) // 2):
        reason = "PAT 可能失效" if any(r[4] == "auth" for r in blind) else "被限流"
        notify(
            f"🌅 早安 Kai — ⚠️ 這輪我只讀到 {len(reads) - len(blind)}/{len(reads)} 條"
            f"（{reason}),fleet 健康報不準,先別當全綠 —— 去看一下 kai-notify 的 "
            f"FLEET_READ_TOKEN。\n\n— 每天早上這一則;哪天沒來,就是通知管線自己掛了 🫥"
        )
        print("fleet-digest: degraded reads; sent honest warning (history untouched).")
        return 0  # 不更新歷史,別讓一次 token 中斷洗掉連續紀錄

    history = _load_history()
    assessed = []
    for key, name, cadence, run, _err, state in reads:
        a = _assess(name, cadence, run, now, state)
        a["key"] = key
        # 用「今天以前」的歷史(還沒 append 今天)判讀趨勢。
        prior = history.get(key, [])
        a["recovered"] = _recovered(prior, a["kind"])
        a["flapping"] = _is_flapping(prior, a["kind"])
        assessed.append(a)
    # 「被停用」不是出事,不進「要你看一下」的計數 —— 但也絕不消失:它在下面的
    # 「另外」區塊每天照列一行。看門狗可以不喊狼,不可以假裝那條 cron 不存在。
    problems = [a for a in assessed if a["kind"] not in ("ok", "off")]
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
        for a in switched_off:
            lines.append(f"🔕 {a['name']} — {a['detail']}")
        return lines

    if not problems:
        highlights = _highlights()
        if highlights:
            # 沒出事,但有恢復/反覆要讓他知道 —— 不是純報平安。
            msg = (
                f"🌅 早安 Kai — 後端沒出事,但有 {len(highlights)} 件想讓你知道 👇\n\n"
                + "\n".join(highlights)
                + f"\n\n其餘 {len(assessed) - len(highlights)} 個都正常 ✅"
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
            streak = f"（連續 {green} 天全綠）" if green >= 3 else ""
            msg = (
                f"🌅 早安 Kai — 後端一切正常 😌{streak}\n\n"
                f"{len(assessed)} 個排程都乖乖跑過、沒半個出事,今天不用你動手。"
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
            blocks.append(head)
        ok_n = len(assessed) - len(problems) - len(switched_off)
        msg = f"🌅 早安 Kai — 有 {len(problems)} 個要你看一下 👇\n\n" + "\n\n".join(
            blocks
        )
        highlights = _highlights()
        if highlights:
            msg += "\n\n另外(現在沒壞,但值得知道):\n" + "\n".join(highlights)
        msg += f"\n\n其他 {ok_n} 個都正常 ✅"

    # heartbeat 提醒:低調一行,但要在——這則的意義就是「沒收到=通知線斷了」。
    msg += "\n\n— 每天早上這一則;哪天沒來,就是通知管線自己掛了 🫥"
    sent = notify(msg)

    # (b) 更新歷史:append 今天每條 kind + fleet 整體,裁到最近 N 天。
    #     日期冪等(Taipei 日):同一天二次觸發(agent 測試 / workflow_dispatch)不再
    #     重灌一格,否則把 streak/flapping 記憶灌失真——而這條記憶迴圈的唯一價值就是
    #     分辨「慢性 vs 新問題」。訊息照送(上面已 notify),只有歷史 append 每天一次。
    today = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    if history.get("_last_date") != today:
        for a in assessed:
            history[a["key"]] = (history.get(a["key"], []) + [a["kind"]])[
                -_HISTORY_LEN:
            ]
        history["_fleet"] = (
            history.get("_fleet", []) + ["problem" if problems else "green"]
        )[-_HISTORY_LEN:]
        history["_last_date"] = today
        _save_history(history)

    print("fleet-digest:", "sent." if sent else "skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
