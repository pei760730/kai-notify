"""Contract tests for the shared GitHub Action surface."""

from __future__ import annotations

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
_ACTION_YML = os.path.join(_ROOT, "action.yml")
_ACTION_SEND = os.path.join(_ROOT, "scripts", "action_send.py")

EXPECTED_INPUTS = {
    "text",
    "title",
    "items",
    "label",
    "value",
    "floor",
    "unit",
    "bot-token",
    "chat-id",
}

ACTION_ENV_KEYS = {
    "KN_TEXT",
    "KN_TITLE",
    "KN_ITEMS",
    "KN_LABEL",
    "KN_VALUE",
    "KN_FLOOR",
    "KN_UNIT",
}


def _load_action_send():
    spec = importlib.util.spec_from_file_location("action_send", _ACTION_SEND)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


action_send = _load_action_send()


def _action_input_keys():
    keys = set()
    in_inputs = False
    with open(_ACTION_YML, encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line == "inputs:\n":
                in_inputs = True
                continue
            if in_inputs and not line.startswith(" "):
                break
            if in_inputs and line.startswith("  ") and not line.startswith("    "):
                keys.add(line.strip().removesuffix(":"))
    return keys


def test_action_inputs_are_pinned():
    actual = _action_input_keys()
    added = actual - EXPECTED_INPUTS
    removed = EXPECTED_INPUTS - actual
    assert actual == EXPECTED_INPUTS, (
        f"action.yml inputs changed; added={sorted(added)}, removed={sorted(removed)}"
    )


def _clear_action_env(monkeypatch):
    for key in ACTION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _capture_senders(monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_send, "notify", lambda text: calls.append(("notify", text)) or True
    )
    monkeypatch.setattr(
        action_send,
        "notify_digest",
        lambda title, items: calls.append(("notify_digest", title, items)) or True,
    )
    monkeypatch.setattr(
        action_send,
        "notify_metric",
        lambda label, value, floor=1, unit="": (
            calls.append(("notify_metric", label, value, floor, unit)) or True
        ),
    )
    return calls


def test_action_send_routes_text_to_notify(monkeypatch):
    _clear_action_env(monkeypatch)
    calls = _capture_senders(monkeypatch)
    monkeypatch.setenv("KN_TEXT", "hello")

    assert action_send.main() == 0
    assert calls == [("notify", "hello")]


def test_action_send_routes_digest_to_notify_digest(monkeypatch):
    _clear_action_env(monkeypatch)
    calls = _capture_senders(monkeypatch)
    monkeypatch.setenv("KN_TITLE", "Daily")
    monkeypatch.setenv("KN_ITEMS", "one\ntwo\n")

    assert action_send.main() == 0
    assert calls == [("notify_digest", "Daily", ["one", "two"])]


def test_action_send_routes_metric_to_notify_metric(monkeypatch):
    _clear_action_env(monkeypatch)
    calls = _capture_senders(monkeypatch)
    monkeypatch.setenv("KN_LABEL", "rows")
    monkeypatch.setenv("KN_VALUE", "0")

    assert action_send.main() == 0
    assert calls == [("notify_metric", "rows", "0", 1.0, "")]


def test_action_send_passes_unit_to_notify_metric(monkeypatch):
    _clear_action_env(monkeypatch)
    calls = _capture_senders(monkeypatch)
    monkeypatch.setenv("KN_LABEL", "voc daily")
    monkeypatch.setenv("KN_VALUE", "0")
    monkeypatch.setenv("KN_UNIT", "篇")

    assert action_send.main() == 0
    assert calls == [("notify_metric", "voc daily", "0", 1.0, "篇")]


# The action.yml env: block wires each input into the KN_* var action_send reads.
# The tests above prove KN_* -> notify* routing; this proves inputs -> KN_* — the
# unguarded seam. A typo like `KN_TEXT: ${{ inputs.txt }}` leaves every consumer
# silently sending blanks: CI green, ~12 repos muted.
EXPECTED_ENV_GLUE = (
    "KN_TEXT: ${{ inputs.text }}",
    "KN_TITLE: ${{ inputs.title }}",
    "KN_ITEMS: ${{ inputs.items }}",
    "KN_LABEL: ${{ inputs.label }}",
    "KN_VALUE: ${{ inputs.value }}",
    "KN_FLOOR: ${{ inputs.floor }}",
    "KN_UNIT: ${{ inputs.unit }}",
)


def test_action_yml_wires_inputs_to_kn_env():
    with open(_ACTION_YML, encoding="utf-8") as f:
        src = f.read()
    for mapping in EXPECTED_ENV_GLUE:
        assert mapping in src, f"action.yml input->env glue broken/renamed: {mapping!r}"


# --- 空輸入不能等於靜默(2026-07-26~30 ig-insights-sync 事件) ---
#
# 那次:通知器組訊息的步驟被跳過 → text 是空字串 → action 靜默 return 0 →
# workflow 綠燈。cron 連紅五天,owner 收到零則。呼叫端補保底只修一個消費端;
# fleet 有 53 個 kai-notify step、9 個 text 是動態值且無保底,所以補在 hub 這層。


def _run_action_send(monkeypatch, env: dict, sent: list):
    for key in ("KN_TEXT", "KN_TITLE", "KN_ITEMS", "KN_LABEL", "KN_VALUE", "KN_UNIT", "KN_FLOOR"):
        monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = _load_action_send()
    monkeypatch.setattr(mod, "notify", lambda text: sent.append(text) or True)
    assert mod.main() == 0
    return sent


def test_empty_input_sends_self_diagnostic_instead_of_silence(monkeypatch):
    sent: list[str] = []
    _run_action_send(
        monkeypatch,
        {
            "GITHUB_REPOSITORY": "pei760730/ig-insights-sync",
            "GITHUB_WORKFLOW": "ig-insights-sync",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "123456",
        },
        sent,
    )
    assert len(sent) == 1, "什麼都沒給的時候必須出聲,不能靜默綠燈"
    body = sent[0]
    # owner 是 chat-only:訊息本身要指得出是誰壞了,不能只說「有東西壞了」
    assert "pei760730/ig-insights-sync" in body
    assert "ig-insights-sync" in body
    assert "actions/runs/123456" in body


def test_empty_input_alert_survives_missing_github_env(monkeypatch):
    # 本機 / 非 Actions 環境呼叫時不能因為缺 env 就炸掉(fail-soft 是地基)
    for key in ("GITHUB_REPOSITORY", "GITHUB_WORKFLOW", "GITHUB_SERVER_URL", "GITHUB_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    sent: list[str] = []
    _run_action_send(monkeypatch, {}, sent)
    assert len(sent) == 1


def test_healthy_metric_stays_silent(monkeypatch):
    """metric 路徑刻意的靜默不能被這次改動波及。

    label+value 且高於 floor = 產出正常,設計上就是不出聲。若這條紅了,
    代表 empty-input 分支吃到了 metric 的流量,整個 fleet 會被健康訊息洗版。
    """
    mod = _load_action_send()
    for key in ("KN_TEXT", "KN_TITLE", "KN_ITEMS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KN_LABEL", "rows")
    monkeypatch.setenv("KN_VALUE", "42")
    monkeypatch.setenv("KN_FLOOR", "1")
    sent: list[str] = []
    monkeypatch.setattr(mod, "notify", lambda text: sent.append(text) or True)
    monkeypatch.setattr(mod, "notify_metric", lambda *a, **k: False)  # 高於 floor → 不告警
    assert mod.main() == 0
    assert sent == [], "健康的 metric 必須維持靜默,不可被 empty-input 分支攔截"
