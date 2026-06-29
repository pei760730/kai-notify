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

from kai_notify import notify, notify_digest  # noqa: E402


def main() -> int:
    text = (os.environ.get("KN_TEXT") or "").strip()
    title = (os.environ.get("KN_TITLE") or "").strip()
    items = [
        line for line in (os.environ.get("KN_ITEMS") or "").splitlines() if line.strip()
    ]

    if text:
        ok = notify(text)
    elif title or items:
        ok = notify_digest(title, items)
    else:
        print("kai-notify: no text/title/items given; nothing to send.")
        return 0

    print("kai-notify: sent." if ok else "kai-notify: skipped/failed (fail-soft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
