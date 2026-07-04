# CLAUDE.md — kai-notify

> 接手先讀。kai-notify = 後端唯一「對外通知出口器官」:任何 repo 一行
> (`uses: pei760730/kai-notify@v1`,或 in-process `from kai_notify import notify`)
> 把一則訊息推到 owner 的 Telegram(@MYNOOTIFYBot)。**它現在是 ~12 個 repo 的共用
> 相依,改壞它 = 整個後端同時失聲(而且因為 fail-soft,是靜默失聲)。**

## 載重不變式(改任何一條前先證明沒破;破了就回退)

- **fail-soft**:token 缺 / Telegram 掛 → log + 回 False,**永不 raise**。通知掛掉
  不能反過來弄死呼叫端 cron。這是本 repo 的地基,不為任何功能讓步。
- **fail-closed**:只推 env 設定的單一 chat id;token / chat id 只從 env/secret 讀,
  **永不硬寫進 code**(repo 是 PUBLIC)。
- **純文字**:不用 MarkdownV2(escaping 會靜默掉訊)。
- **stdlib-only 核心**:`python/kai_notify/` 零依賴才能被 vendored;action 直接 import
  它 = 單一真相源,**不得複製第二份實作**(TS 版已因此砍除)。
- **長度按 UTF-16 截斷**:Telegram 的 4096 是 UTF-16 單位、emoji 佔 2;按 code point
  算會漏截,超長訊息被 400 後又被 fail-soft 靜默吞掉。

## 消費端契約(改這些 = 同時改 ~12 個 repo,先數清楚再動)

- secret:`KAI_NOTIFY_BOT_TOKEN` + `KAI_NOTIFY_CHAT_ID`(每 repo 各設一份;個人帳號
  的 secret 不能跨 repo 共用)。
- action inputs:`text` | `title`+`items` | `label`+`value`(+`floor`,green-but-empty
  告警) | `bot-token`/`chat-id`(覆蓋 env)。**改 input 名 = 破契約、12 repo 靜默失聲。**
- 消費端 pin **`@v1`**(移動式 major tag),不是 `@main`;放版要
  `git tag -f v1 && git push -f origin v1` —— main 動不等於 fleet 動。

## Decisions

- **砍 TS 平行實作**(e2ed2d8):零 import、雙核維護是純負債。Node 消費端走 action。
- **fleet-digest = 看門狗的看門狗**(PR #1/#7):action fail-soft ⇒ 壞掉是靜默的;
  每天 07:00 輪詢 14 條 cron 狀態、一天一則(全綠一行 / 只列出事的),history 存
  `state/fleet_history.json`(git 當 store,不引新平台)。⚠ **殘留盲點**:它用同一隻
  bot 發,bot 死了它也發不出「我死了」;目前靠「訊息沒來 = 管線死」的缺席訊號
  mitigate,刻意不加 out-of-band 通道(避免過度工程)。
- **notify_metric**(PR #8):跑成功但產出 0(green-but-empty)是內容管線的真失敗。

## Lessons(觸發 → 教訓;理由 / 證據 / 失效條件)

- **觸發:準備在本 repo 動手 → 先 `git fetch origin` + `gh pr list`,確認沒有第二條
  活動線再改;發現有,先停手請 owner 關一邊。**
  - 理由:這是 ~12 repo 的共用 hub,又常有多個 agent session 同時進來,並行改必撞。
  - 證據:2026-07-03 五 PR 三線撞車(global memory);同日本 session push 被 reject 需 rebase。
  - 失效:若改為單一維護者 / 單線流程,可放寬。
- **觸發:想在 `_send` / digest 加「更聰明」的東西(降噪 / 分級 / 去重 / 自我健檢)
  → 先問這 fleet 是否真的痛,再問能不能用測試或規則擋,最後才寫 code。**
  - 理由:更智能 ≠ 更多;每個訊號要自己掙到位置,否則衰退成噪音(警告必配修復)。
  - 證據:預設仍是「只在出事時說話」;fleet 已 ~12 消費端(門檻已過)才做的 digest。
  - 失效:訊量暴增到洗版時,再回來做降噪。
