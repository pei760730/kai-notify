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

## Live consumer

`benchmark-radar`'s daily watchdog cron pushes a one-line runner status via this
action — its own code noted it "self-records but never self-reports"; kai-notify
is that reporter. See `.github/workflows/radar-watchdog.yml` in that repo.

## Layout

```
action.yml              composite action (uses: pei760730/kai-notify@main)
scripts/action_send.py  action entry — imports the python core (one source of truth)
python/kai_notify/      pip-installable / vendorable Python helper
.github/workflows/ci.yml  pytest + action self-test
```

## Develop

```bash
cd python && pip install pytest && pytest -q
```
