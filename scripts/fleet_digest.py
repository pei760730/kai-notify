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
from kai_notify import notify  # noqa: E402

OWNER = "pei760730"
_API = (
    "https://api.github.com/repos/{owner}/{repo}/actions/workflows/{wf}/runs?per_page=1"
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
    ("style-superman", "health.yml", "style-superman health", "weekly"),
    ("media-sorter", "ytdlp-weekly-check.yml", "media-sorter ytdlp", "weekly"),
    ("GOLD-ContentSystem", "adoption-metrics.yml", "GOLD adoption", "weekly"),
    ("KaiOS-ContentSystem", "adoption-metrics.yml", "KaiOS adoption", "weekly"),
    ("KaiOS-ContentSystem", "ig-sheet-sync.yml", "KaiOS ig-sync", "daily"),
    ("short-video-bot", "collect.yml", "short-video-bot collect", "frequent"),
    ("clip-collector", "collect.yml", "clip-collector collect", "frequent"),
    ("feed-collector", "collect.yml", "feed-collector collect", "frequent"),
]

# 各節奏的「該多久內要有一次 run」上限;超過視為 stale(cron 沒排到/壞了)。
_STALE_AFTER = {
    "frequent": timedelta(hours=2),
    "daily": timedelta(hours=26),
    "weekly": timedelta(days=8),
    "monthly": timedelta(days=32),
}


def _latest_run(repo: str, wf: str, token: str) -> dict | None:
    """回傳該 workflow 最近一次 run 的 {conclusion, created_at}，讀不到回 None。"""
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
        return runs[0] if runs else None
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


_CADENCE_HUMAN = {
    "frequent": "本來就該一直在跑",
    "daily": "平常每天要跑一次",
    "weekly": "平常每週跑一次",
    "monthly": "平常每月跑一次",
}


def _assess(name: str, cadence: str, run: dict | None, now: datetime) -> dict:
    """判讀一個 cron 的最近狀態，回白話結果。
    kind: ok(正常) / fail(跑失敗) / stale(該跑沒跑) / unknown(讀不到)。
    只有 fail/stale/unknown 算「要人看」；running/cancelled 視為沒事(它在動)。"""
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

    stale = age is not None and age > _STALE_AFTER.get(cadence, timedelta(hours=26))
    if stale:
        return {
            "kind": "stale",
            "name": name,
            "detail": f"已經 {ago} 沒動靜了（{_CADENCE_HUMAN.get(cadence, '')}），排程可能卡住",
            "url": url,
        }

    # success / 進行中 / cancelled 都當「沒事」——它有在動就好。
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
    assessed = [
        _assess(name, cadence, _latest_run(repo, wf, token), now)
        for repo, wf, name, cadence in MONITORED
    ]
    problems = [a for a in assessed if a["kind"] != "ok"]

    if not problems:
        # 最常見的情況:全好。就一句話讓他放心,不要丟一堆流水帳。
        msg = (
            f"🌅 早安 Kai — 後端一切正常 😌\n\n"
            f"{len(assessed)} 個排程昨天都乖乖跑過、沒半個出事,今天不用你動手。"
        )
    else:
        blocks = []
        for a in problems:
            block = f"{_ICON.get(a['kind'], '⚠️')} {a['name']} — {a['detail']}"
            if a["url"]:
                block += f"\n   👉 {a['url']}"
            blocks.append(block)
        ok_n = len(assessed) - len(problems)
        msg = (
            f"🌅 早安 Kai — 有 {len(problems)} 個要你看一下 👇\n\n"
            + "\n\n".join(blocks)
            + f"\n\n其他 {ok_n} 個都正常 ✅"
        )

    # heartbeat 提醒:低調一行,但要在——這則的意義就是「沒收到=通知線斷了」。
    msg += "\n\n— 每天早上這一則;哪天沒來,就是通知管線自己掛了 🫥"

    sent = notify(msg)
    print("fleet-digest:", "sent." if sent else "skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
