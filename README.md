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
- **three calls** — `notify(text)`, `notify_digest(title, items)`, and
  `notify_metric(label, value, floor)` (the last flags a *green-but-empty* run —
  see below).

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

All return `true` on success, `false` when skipped/failed — and never throw.

### C) Guard a "green but empty" run — `notify_metric`

A cron can **succeed and still produce nothing** (scraped 0 rows, synced 0
files). That never shows up in the run's *conclusion*, so the fleet-digest —
which only reads conclusions — is structurally blind to it. Report what the run
actually produced, and kai-notify pings **only** when it comes up short:

```python
from kai_notify import notify_metric
notify_metric("voc daily", rows_written, floor=1, unit="篇")
```

Below `floor` → an immediate "跑綠但產出 0" alert (visible the moment it happens,
not a day later). At/above the floor → silent, like every other success here.
The same is available to yml-only workflows via the action:

```yaml
- uses: pei760730/kai-notify@v1
  env:
    KAI_NOTIFY_BOT_TOKEN: ${{ secrets.KAI_NOTIFY_BOT_TOKEN }}
    KAI_NOTIFY_CHAT_ID: ${{ secrets.KAI_NOTIFY_CHAT_ID }}
  with:
    label: "voc daily"
    value: ${{ steps.run.outputs.rows }}
    floor: "1"
```

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

- all-green → a single "後端一切正常" line (no status dump), with a "連續 N 天全綠"
  streak once it's been green a while,
- otherwise → only the exceptions (failed / stale / unreadable), plain-language
  with a run link and a "連 N 天 / 今天第一次" tag so chronic reads differently
  from new.

It is **honest about its own blind spots**: if the PAT dies or gets rate-limited
and most reads fail, it says "只讀到 X/N …先別當全綠" instead of implying health —
a monitor's whole worth is that it never falsely reassures. And it is
**history-aware**: each day's verdicts are persisted to `state/fleet_history.json`
(git is the store — no new platform), so it reports deterioration, not a stateless
snapshot. This is why the digest job needs `contents: write` (to commit history).

It reasons over that history, not just today's boolean:

- **recovery** — a cron that failed yesterday and is green today gets its own
  "✅ 之前在鬧,今天恢復了" line instead of vanishing into "其他都正常". A fix
  confirming itself is worth one line.
- **flapping** — a cron that oscillates (ok/fail/ok/fail) is flagged 🔀時好時壞
  even on a green day. The consecutive-streak suffix can't see intermittent
  instability — it reads every other day as "今天第一次"; this catches the
  chronic-but-not-constant failure the streak logic hides.

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
