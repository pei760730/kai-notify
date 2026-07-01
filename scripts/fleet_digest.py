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
_API = "https://api.github.com/repos/{owner}/{repo}/actions/workflows/{wf}/runs?per_page=1"
_TIMEOUT = 15
_TPE = timezone(timedelta(hours=8))  # Asia/Taipei

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


def _status_line(name: str, cadence: str, run: dict | None, now: datetime) -> tuple[str, bool]:
    """回傳 (顯示行, 是否有問題)。有問題 = 失敗 / stale / 讀不到。"""
    if run is None:
        return f"❓ {name}:讀不到 run", True
    concl = run.get("conclusion")  # success/failure/cancelled/None(進行中)
    created = run.get("created_at", "")
    try:
        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        age = now - ts
    except ValueError:
        ts, age = None, None

    stale = age is not None and age > _STALE_AFTER.get(cadence, timedelta(hours=26))
    ago = _humanize(age) if age is not None else "?"

    if concl == "success" and not stale:
        return f"✅ {name} · {ago} 前", False
    if concl == "failure":
        return f"❌ {name} · {ago} 前失敗", True
    if stale:
        return f"⚠️ {name} · 已 {ago} 沒跑(cadence={cadence})", True
    # 進行中 / cancelled / 其他:非綠、但不一定是災難,標出來讓人看一眼。
    return f"⚠️ {name} · {concl or '進行中'} · {ago} 前", True


def _humanize(delta: timedelta) -> str:
    m = int(delta.total_seconds() // 60)
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h"
    return f"{h // 24}d"


def main() -> int:
    token = (os.environ.get("FLEET_READ_TOKEN") or "").strip()
    if not token:
        # 沒 PAT 就沒有跨 repo 讀取權;送一則明說,免得「digest 靜悄悄」被誤當管線活著。
        notify(
            "📋 fleet-digest 未設 FLEET_READ_TOKEN,無法讀跨 repo 狀態(heartbeat 僅代表 digest cron 本身活著)。"
        )
        print("fleet-digest: no FLEET_READ_TOKEN; sent degraded heartbeat.")
        return 0

    now = datetime.now(timezone.utc)
    lines: list[str] = []
    problems = 0
    for repo, wf, name, cadence in MONITORED:
        run = _latest_run(repo, wf, token)
        line, bad = _status_line(name, cadence, run, now)
        lines.append(line)
        problems += 1 if bad else 0

    date_tpe = now.astimezone(_TPE).strftime("%Y-%m-%d")
    ok = len(MONITORED) - problems
    header = (
        f"📋 fleet daily · {date_tpe} · {ok}/{len(MONITORED)} ok"
        + (f" · ⚠️{problems} 要看" if problems else " · 全綠")
    )
    # 問題行排前面,一眼看到該處理的。
    lines.sort(key=lambda s: 0 if s[0] in "❌⚠️❓" else 1)
    sent = notify(header + "\n" + "\n".join(lines))
    print("fleet-digest:", "sent." if sent else "skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
