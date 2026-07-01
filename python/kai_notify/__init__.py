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
import random
import time
import urllib.error
import urllib.request
from typing import Iterable

__all__ = ["notify", "notify_digest"]

_log = logging.getLogger("kai_notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10  # seconds; a slow Telegram must not stall the caller's cron
# Transient sends (429 rate-limit, 5xx, network blips) are retried — for this
# fleet the dropped message (a failure alert, a collector drain) IS the product,
# so a swallowed 429 means Kai never learns a cron broke. Bounded and budgeted
# so a down Telegram can never stall the caller's cron (the load-bearing rule).
_MAX_ATTEMPTS = 3
_RETRY_BUDGET = 25.0  # hard cap on total send wall-clock across all attempts
_BACKOFF = (1.0, 3.0)  # base seconds before attempts 2 and 3 (jitter added)
# Telegram rejects a sendMessage over 4096 chars with 400 — which fail-soft
# would swallow, silently dropping the whole message. Truncate instead.
_TG_MAX = 4096
_TRUNC_MARK = "…(truncated)"


def _clip(text: str) -> str:
    """Clip to Telegram's hard limit, by code point (never splits a CJK char
    or emoji), leaving room for a visible truncation marker."""
    if len(text) <= _TG_MAX:
        return text
    return text[: _TG_MAX - len(_TRUNC_MARK)].rstrip() + _TRUNC_MARK


def _failure_hint(code: int, description: str) -> str:
    """Turn a Telegram error into something an AI/operator can act on from a
    log line — the #1 silent-mute causes across many fail-closed repos."""
    d = description.lower()
    if code == 400 and "chat not found" in d:
        return " — the bot was never /start-ed by this chat, or the chat id is wrong"
    if code in (401, 403, 404):
        return " — bot token invalid/revoked, or the bot was blocked"
    return ""


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


def _retry_after(exc: urllib.error.HTTPError, raw: bytes) -> float | None:
    """Seconds Telegram asks us to wait on a 429, from the JSON body's
    parameters.retry_after (preferred) or the Retry-After header. None if absent."""
    try:
        params = json.loads(raw.decode("utf-8")).get("parameters") or {}
        if params.get("retry_after") is not None:
            return float(params["retry_after"])
    except Exception:  # noqa: BLE001
        pass
    try:
        header = exc.headers.get("Retry-After")
        return float(header) if header is not None else None
    except Exception:  # noqa: BLE001
        return None


def _send_once(req: urllib.request.Request, timeout: float) -> tuple[str, object]:
    """One attempt. Returns:
    ("ok", bool)     — definitive (True sent, False ok:false); do NOT retry.
    ("retry", wait)  — transient (429/5xx/network); wait is seconds or None.
    ("fail", None)   — permanent (400/401/403/404); already logged, do NOT retry.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        # A 200 can still carry {"ok": false} — don't report that as sent.
        try:
            ok = bool(json.loads(body.decode("utf-8")).get("ok"))
        except Exception:  # noqa: BLE001 - non-JSON 200 is unexpected; trust it
            ok = True
        if not ok:
            _log.warning("kai_notify: telegram returned ok=false (fail-soft)")
        return ("ok", ok)
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            pass
        # 429 (rate limit) and 5xx are transient → retry.
        if exc.code == 429 or exc.code >= 500:
            return ("retry", _retry_after(exc, raw))
        # 400/401/403/404 etc. won't get better by retrying — surface + give up.
        try:
            description = json.loads(raw.decode("utf-8")).get("description", "")
        except Exception:  # noqa: BLE001
            description = exc.reason or ""
        _log.warning(
            "kai_notify: send failed (fail-soft): HTTP %s %s%s",
            exc.code,
            description,
            _failure_hint(exc.code, description),
        )
        return ("fail", None)
    except Exception as exc:  # noqa: BLE001 - network/timeout: transient, retry
        _log.warning("kai_notify: send attempt failed (%s); retrying if budget", exc)
        return ("retry", None)


def _send(text: str) -> bool:
    creds = _credentials()
    if creds is None:
        return False
    token, chat_id = creds
    payload = json.dumps(
        {"chat_id": chat_id, "text": _clip(text), "disable_web_page_preview": True}
    ).encode("utf-8")

    deadline = time.monotonic() + _RETRY_BUDGET
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        req = urllib.request.Request(
            _API.format(token=token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        outcome, info = _send_once(req, timeout=min(_TIMEOUT, remaining))
        if outcome == "ok":
            return bool(info)
        if outcome == "fail":
            return False
        # outcome == "retry": back off (honoring Telegram's retry_after), but
        # never sleep past the budget — a down Telegram must not stall the caller.
        if attempt == _MAX_ATTEMPTS:
            break
        wait = info if info is not None else _BACKOFF[attempt - 1]
        wait += random.uniform(0, 0.5 * wait if wait else 0.5)
        if time.monotonic() + wait >= deadline:
            break
        time.sleep(wait)
    _log.warning(
        "kai_notify: send failed after %d attempt(s) (fail-soft)", _MAX_ATTEMPTS
    )
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
