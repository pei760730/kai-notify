"""Entry point for the composite GitHub Action.

Reads the action inputs from env and dispatches to the kai_notify core, so the
action and Python consumers share one implementation. Always exits 0: the core
is fail-soft, and a notifier must never fail the workflow it reports on.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
)

from kai_notify import notify, notify_digest, notify_metric



def _empty_input_alert() -> str:
    """Message for "the caller invoked me with nothing".

    Self-identifying on purpose: the owner is chat-only, so a bare "something is
    wrong" costs him a hunt. Everything here comes from GitHub's default env,
    which is present for composite-action steps.
    """
    repo = os.environ.get("GITHUB_REPOSITORY") or "(unknown repo)"
    workflow = os.environ.get("GITHUB_WORKFLOW") or "(unknown workflow)"
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    run_id = os.environ.get("GITHUB_RUN_ID") or ""
    where = f"{server}/{repo}/actions/runs/{run_id}" if run_id else "(no run id)"
    return (
        f"⚠️ {repo} 的「{workflow}」呼叫了通知器,但沒有給任何內容 —— "
        f"這輪該說的話沒說出口。通常是組訊息那一步被跳過或炸掉(例如相依還沒裝好),"
        f"不是「這輪沒事」。去看:{where}"
    )

def main() -> int:
    text = (os.environ.get("KN_TEXT") or "").strip()
    title = (os.environ.get("KN_TITLE") or "").strip()
    items = [
        line for line in (os.environ.get("KN_ITEMS") or "").splitlines() if line.strip()
    ]
    label = (os.environ.get("KN_LABEL") or "").strip()
    value = (os.environ.get("KN_VALUE") or "").strip()
    unit = (os.environ.get("KN_UNIT") or "").strip()

    if text:
        ok = notify(text)
    elif title or items:
        ok = notify_digest(title, items)
    elif label and value != "":
        # Product-quantity report: alerts only when below the floor, otherwise
        # stays silent — so "healthy" here is a no-send, not a failure.
        try:
            floor = float((os.environ.get("KN_FLOOR") or "").strip() or "1")
        except ValueError:
            floor = 1.0
        alerted = notify_metric(label, value, floor=floor, unit=unit)
        print(
            "kai-notify: metric below floor -> alert sent."
            if alerted
            else "kai-notify: metric ok (silent)."
        )
        return 0
    else:
        # 沒有任何內容 = 呼叫端壞了,不是「這輪沒事」。
        #
        # 真實事件(2026-07-26~30,ig-insights-sync):`Build notify message` 帶
        # always() 但排在 `Install runtime deps` 之後,checkout 一失敗就被跳過 →
        # 通知器 ModuleNotFoundError → 輸出空字串 → 這裡靜默 return 0 → workflow
        # 綠燈。cron 連紅五天,owner 收到零則。呼叫端已補 || 保底,但那只修了一個
        # 消費端;fleet 有 53 個 kai-notify step、其中 9 個的 text 是動態值且無保底。
        #
        # 所以在 hub 這一層補:什麼都沒拿到就發一則「你的通知器壞了」,把 repo /
        # workflow / run 指出來。這不違反 fail-soft(notify 本身仍不 raise),
        # 也不動任何 input 契約。**metric 路徑刻意的靜默不受影響**——那條在上面
        # 就 return 了,健康時不出聲是它的設計。
        ok = notify(_empty_input_alert())
        print(
            "kai-notify: EMPTY INPUT — caller sent nothing; "
            + ("self-diagnostic alert sent." if ok else "alert also failed (fail-soft).")
        )
        return 0

    print("kai-notify: sent." if ok else "kai-notify: skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
