# AGENTS.md — kai-notify(Codex 行為規則)

> 雙 AI 艦隊檔:Codex 進場先讀這份 + **`CLAUDE.md`(in-repo 憲法,必讀)**。

## 這個 repo 是什麼

後端艦隊**唯一對外通知出口**:任何 repo 一行 `uses: pei760730/kai-notify@v1` 把訊息推到 owner 的 Telegram。composite action + Python 套件 `kai_notify`(stdlib 零依賴)共用同一核心。另含 `fleet-digest`(每天 07:00 台北輪詢全艦隊 cron 狀態)與 `drill.yml`(通知鏈實彈演習)。

## 紅線(違反即停)

- **fail-soft**:token 缺 / Telegram 掛 → log 後略過,**絕不弄死呼叫端 cron**。
- **fail-closed**:只推 env 設定的單一 chat id;純文字(不用 MarkdownV2);secret 不進 code。
- **action input 契約已被測試釘死**(改 input 名 → CI 紅):消費端 17+ repo 全 pin `@v1`,破壞性改動=艦隊級事故。
- 發版=`git tag -f v1` 推 tag;v1 tag 保護議題已結案(2026-07-25 Kai 拍板維持現狀),勿重提。
- **動工前 `git fetch` + `gh pr list`**:本 repo 有多 session 平行開發前科,同時段一條活動線。
- 本 repo 自身需要 `KAI_NOTIFY_BOT_TOKEN`/`KAI_NOTIFY_CHAT_ID` secrets(fleet-digest 用),**別刪**。

## 驗證

```bash
python -m pytest -q && python -m ruff check .
```

## Codex 通用紀律

分支 `codex/*`;絕不自 merge(merge 一律 Kai 說了算);宣稱完成前先跑驗證看到綠;只動被要求的部分。
