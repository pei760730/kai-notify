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
        print("kai-notify: no text/title/items/metric given; nothing to send.")
        return 0

    print("kai-notify: sent." if ok else "kai-notify: skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
