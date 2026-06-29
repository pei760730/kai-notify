"""kai-notify — outbound Telegram push for Kai's AI backend.

The single source of truth for "tell Kai one line in Telegram". The composite
GitHub Action imports this exact module, so Python consumers and pure-yml
workflows behave identically.

Design rules (do not relax):
- fail-soft: missing token/chat id or a Telegram outage -> log + return False,
  NEVER raise. A broken notifier must not crash the task it reports on.
- fail-closed: the recipient is the single configured KAI_NOTIFY_CHAT_ID. The
  chat id and bot token are read from env/secret only, never hardwired.
- plain text only: no MarkdownV2 (its escaping rules silently drop messages).
- stdlib only: zero install deps, so this file can also just be vendored.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Iterable

__all__ = ["notify", "notify_digest"]

_log = logging.getLogger("kai_notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10  # seconds; a slow Telegram must not stall the caller's cron


def _credentials() -> tuple[str, str] | None:
    token = (os.environ.get("KAI_NOTIFY_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("KAI_NOTIFY_CHAT_ID") or "").strip()
    if not token or not chat_id:
        # Not an error: a repo without the secret set simply stays silent.
        _log.warning(
            "kai_notify: KAI_NOTIFY_BOT_TOKEN / KAI_NOTIFY_CHAT_ID not set; "
            "skipping push (fail-soft)."
        )
        return None
    return token, chat_id


def _send(text: str) -> bool:
    creds = _credentials()
    if creds is None:
        return False
    token, chat_id = creds
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        _API.format(token=token),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read()  # drain
        return True
    except Exception as exc:  # noqa: BLE001 - fail-soft: never propagate
        _log.warning("kai_notify: send failed (fail-soft): %s", exc)
        return False


def notify(text: str) -> bool:
    """Push a single line / block of text to Kai's Telegram.

    Returns True if Telegram accepted the message, False if it was skipped
    (no credentials, empty text) or failed (never raises).
    """
    text = (text or "").strip()
    if not text:
        return False
    return _send(text)


def notify_digest(title: str, items: Iterable[object]) -> bool:
    """Push a titled bullet list. ``items`` is any iterable of stringifiable
    things; blanks are dropped. Skips (returns False) if nothing to say.
    """
    title = (title or "").strip()
    lines: list[str] = []
    if title:
        lines.append(title)
    for item in items or []:
        s = str(item).strip()
        if s:
            lines.append(f"• {s}")
    if not lines:
        return False
    return _send("\n".join(lines))
