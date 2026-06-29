# kai-notify (Python)

Stdlib-only Telegram push helper. See the [repo README](../README.md) for the
full picture (composite action, TS helper, secrets).

```python
from kai_notify import notify, notify_digest

notify("th-ops daily run done: 3 new suppliers")
notify_digest("Today's ledger", ["MP +1.2%", "PLTR -0.4%"])
```

Both read `KAI_NOTIFY_BOT_TOKEN` + `KAI_NOTIFY_CHAT_ID` from the environment and
fail soft (log + return `False`) if either is missing or Telegram is down.
