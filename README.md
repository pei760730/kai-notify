# kai-notify

The **outbound notification organ** for Kai's AI backend.

The analysis / cron side of the backend (benchmark-radar, th-ops, style-superman,
voc, OS, ig-insights-sync) produces results into places Kai never opens — git
commits, report files, Actions artifacts, GitHub issues. kai-notify is the one
shared core that lets **any repo, in one line, speak to Kai in Telegram**.

- **fail-soft** — missing token or a Telegram outage logs and skips. It will
  **never** crash the task it reports on.
- **fail-closed** — sends only to the single configured chat id. Token and chat
  id come from env/secret, never hardwired. Repo is public; no secrets in code.
- **plain text** — no MarkdownV2 (its escaping silently drops messages).
- **no silent loss** — messages over Telegram's 4096-char limit are clipped (by
  code point) instead of being rejected and swallowed; failures log Telegram's
  own `description` + a root-cause hint (e.g. "bot was never /start-ed").
- **two calls** — `notify(text)` and `notify_digest(title, items)`.

It speaks through a **dedicated notify bot** (separate from the collector bots),
so notifications live in their own Telegram thread instead of mixing with inbound
collection.

---

## One-line hookup

Every consumer needs two values in its environment/secrets:

| name | value |
|---|---|
| `KAI_NOTIFY_BOT_TOKEN` | the bot token (same bot as the collectors) |
| `KAI_NOTIFY_CHAT_ID`   | the owner chat id |

Set them once per repo (use Bash, **not** PowerShell — PowerShell mangles tokens
with a BOM):

```bash
printf '%s' "<bot-token>" | gh secret set KAI_NOTIFY_BOT_TOKEN -R pei760730/<repo>
printf '%s' "<chat-id>"   | gh secret set KAI_NOTIFY_CHAT_ID   -R pei760730/<repo>
```

### A) Any GitHub Actions workflow (Python / Node / pure-yml) — the universal one-liner

```yaml
- uses: pei760730/kai-notify@main
  env:
    KAI_NOTIFY_BOT_TOKEN: ${{ secrets.KAI_NOTIFY_BOT_TOKEN }}
    KAI_NOTIFY_CHAT_ID: ${{ secrets.KAI_NOTIFY_CHAT_ID }}
  with:
    text: "th-ops daily run done"
```

Digest form:

```yaml
- uses: pei760730/kai-notify@main
  env:
    KAI_NOTIFY_BOT_TOKEN: ${{ secrets.KAI_NOTIFY_BOT_TOKEN }}
    KAI_NOTIFY_CHAT_ID: ${{ secrets.KAI_NOTIFY_CHAT_ID }}
  with:
    title: "Today's ledger"
    items: |
      MP +1.2%
      PLTR -0.4%
```

Because every cron consumer is an Actions job, this single step serves Python,
Node, and yml-only repos alike — even if you never import a library.

### B) In-process Python (e.g. local Task Scheduler, mid-run)

Install (stdlib-only, zero deps), or just vendor `python/kai_notify/__init__.py`:

```bash
pip install "git+https://github.com/pei760730/kai-notify@main#subdirectory=python"
```

```python
from kai_notify import notify, notify_digest
notify("radar run ok")
notify_digest("Today's ledger", ["MP +1.2%", "PLTR -0.4%"])
```

Both return `true` on success, `false` when skipped/failed — and never throw.

> Node / TypeScript consumers: use surface **A** (the composite action) from your
> workflow. There is no separate npm package — a parallel TS implementation was
> removed because nothing imported it; every Node cron is an Actions job that the
> action already serves with one step.

---

## Live consumers

Across Kai's backend, every scheduled cron reports through this one action:

- **report crons** (voc, TeaBus-VOC, th-ops, style-superman, media-sorter,
  GOLD, KaiOS, benchmark-radar, ig-insights-sync) push a one-line status —
  wired `if: failure()`, so a healthy run stays silent and only real failures
  ping (with a run link).
- **collector bots** (short-video-bot, clip-collector, feed-collector) route
  their drain-failure alerts here too, replacing per-repo hardcoded curls.

## Heartbeat — the watcher's watcher (`fleet-digest`)

Because the action is **fail-soft**, a broken notifier is *silent*: if the bot
token is revoked, Telegram breaks, or this action regresses, every consumer just
skips and Kai never learns notifications died. `fleet-digest` closes that blind
spot.

`.github/workflows/fleet-digest.yml` runs daily (07:00 Asia/Taipei) and
`scripts/fleet_digest.py` reads each monitored cron's latest run, then sends
**one** message:

- all-green → a single "後端一切正常" line (no status dump),
- otherwise → only the exceptions (failed / stale / unreadable), in plain
  language with a run link.

Its mere arrival **is** the heartbeat: no message one morning → the pipe is dead.
Needs a `FLEET_READ_TOKEN` secret (fine-grained PAT, **Actions: Read-only**
across the fleet) plus this repo's own `KAI_NOTIFY_BOT_TOKEN` / `KAI_NOTIFY_CHAT_ID`.
Absent the PAT it sends a degraded heartbeat that says so.

## Layout

```
action.yml                    composite action (uses: pei760730/kai-notify@main)
scripts/action_send.py        action entry — imports the python core (one source of truth)
scripts/fleet_digest.py       daily fleet health digest + heartbeat
python/kai_notify/            pip-installable / vendorable Python helper
python/tests/                 pytest suite (core + fleet-digest)
.github/workflows/ci.yml      pytest + action self-test
.github/workflows/fleet-digest.yml  the daily heartbeat cron
```

## Develop

```bash
cd python && pip install pytest && pytest -q
```

## Releasing

Consumers pin **`pei760730/kai-notify@v1`** — a moving major tag, not `@main`.
So a merge to `main` does **not** reach the fleet until you move the tag:

```bash
git tag -f v1        # point v1 at the new main
git push -f origin v1
```

Cut a new immutable point release for anything notable (`git tag -a v1.1.0 … &&
git push origin v1.1.0` + `gh release create`). Only bump the *major* (`v2`) on a
breaking change, and migrate consumers deliberately — the whole point of the pin
is that `main` can move without silently changing 14 repos' behavior.
