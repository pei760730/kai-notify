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
- action inputs:`text` | `title`+`items` | `label`+`value`(+`floor`+`unit`,green-but-empty
  告警) | `bot-token`/`chat-id`(覆蓋 env)。**改 input 名 = 破契約、12 repo 靜默失聲。**
  input 集合由 `test_action_contract.py` 釘死(含 inputs→KN_* 膠水),改動要同步更新。
- 消費端 pin **`@v1`**(移動式 major tag),不是 `@main`;放版要
  `git tag -f v1 && git push -f origin v1` —— main 動不等於 fleet 動。

## Decisions

- **砍 TS 平行實作**(e2ed2d8):零 import、雙核維護是純負債。Node 消費端走 action。
- **fleet-digest = 看門狗的看門狗**(PR #1/#7):action fail-soft ⇒ 壞掉是靜默的;
  每天 07:00 輪詢 16 條 cron 狀態(2026-07-11 補 gdrive 月審 + th-customs 月掃)、一天一則(全綠一行 / 只列出事的),history 存
  `state/fleet_history.json`(git 當 store,不引新平台)。⚠ **殘留盲點**:它用同一隻
  bot 發,bot 死了它也發不出「我死了」;目前靠「訊息沒來 = 管線死」的缺席訊號
  mitigate,刻意不加 out-of-band 通道(避免過度工程)。
- **cancelled 算失敗 + 補監控名單**(2026-07-26,帶具體事件重開封版):
  (a) `_assess` 原本把 `cancelled` 併進「沒事」,但 job 層 `timeout-minutes` 逾時被殺
  的 conclusion 就是 cancelled 而非 failure —— 卡到逾時對 owner 等於「這輪沒做完」。
  (b) 名單原本只有 `media-sorter/ytdlp-weekly-check.yml`,沒有真正的下載佇列
  `collector.yml`;該管線 2026-07-02～07-26 因 OAuth 失效靜默死 24 天,而 digest 全綠 ——
  看門狗沒瞎,是沒被指派去看那裡。同批補上 `last30days/daily-brief.yml`。
  兩者都有迴歸測試釘住。**這是本檔「出現真實事件才重開」條款的一次正當觸發**,
  不是「讓它更好」的泛想。⚠ 未動 fail-soft、未改 action inputs、未加 out-of-band 通道
  (後者仍維持刻意不做的決定)。
- **digest 看得見 workflow 的開關狀態**(2026-08-02,帶具體事件重開封版):
  `_assess` 原本只看「最近一次 run 的結論」,看不到 workflow 本身是 active 還是
  disabled。**停用的 workflow 不會再產生新 run**,所以它最後一次 run 的結論被永遠
  凍結 —— 2026-08-01 owner 裁示停掉 `th-customs-scan/scan.yml`(MOC open-data API
  對外關閉,每月只是燒 runner 到逾時),那條的最後一次 run 是 07-25 的 `cancelled`,
  於是 digest 從此每天回報「❌ th-customs 月掃 — 被中止(連 N 天)」,天數還一直長。
  它沒有壞,是被關掉了。實測(對真 fleet dry-run):修前「有 2 個要你看一下」,
  修後「有 1 個」+ 一行 🔕。
  - `disabled_manually` → kind `off`:不計入「要你看一下」,但**每天照列一行**,
    絕不消失(honesty 不變式:可以不喊狼,不可以假裝那條 cron 不存在)。
  - `disabled_inactivity` → 仍算 `fail` 且措辭不同:GitHub 對「repo 60 天沒動靜」
    的排程會自動關掉**且不通知任何人**,那是真的靜默死亡。兩者共用同一 code path、
    只差 state 字串,已用一對正反測試釘死,擋「乾脆把所有 disabled_* 都靜音」。
  - ⚠ 未動 fail-soft(`_workflow_states` 讀不到就回 `{}`,判讀原樣退回)、未改 action
    inputs、未加 out-of-band 通道。
  - ⚠ **這不是降噪功能**。本檔 Lessons 明令「想在 digest 加降噪 / 分級先問痛不痛」,
    其失效條件(訊量暴增到洗版)並未成立 —— 本次改的是**判讀正確性**:把「被關掉」
    講成「壞掉」是錯的判讀,不是吵。原本考慮做的「已知問題暫緩清單」已據該條否決。
- **notify_metric**(PR #8):跑成功但產出 0(green-but-empty)是內容管線的真失敗。
- **補 fitbit freshness 進監控名單**(2026-09-02,帶具體事件重開封版):fitbit 健康金庫
  的雲端看門狗紅了只停在 Actions 頁(09-01 紅兩次、owner 零則);該 repo 刻意不放通知
  secret,由名單讀 run 結論補最後一哩。只加一筆 + 迴歸釘子;未動 fail-soft、未改 action
  inputs、未加 out-of-band 通道。

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
- **觸發:又想對本 repo 開一輪「深挖 / 讓它更好 / 更自我進化」→ 先問「自上次審查後有無
  真實事件或新證據」;沒有就回 [維持現狀 / 封版],不再開審。本 hub 已封版收斂。**
  - 理由:核心不變式齊、契約有測試守(#9 + glue)、治理在(本檔)、fleet-digest 自監控且
    idempotency 已驗。對穩定 hub 反覆深挖 = 燒 Actions 額度 + 加表面 + 撞車風險,報酬遞減
    (協議:複雜度/規則成長本身是警訊,不是成果)。
  - 證據:2026-07-05~06 一週內三輪深度審(兩次多代理 deep-dig + 一次 Fable-5),第三輪
    fleet_history 全綠、velocity 只剩每日 heartbeat chore、零活 bug —— 已無根因級問題可挖。
  - 失效:出現真實事件(某 cron 靜默死 / 契約被破 / Telegram 或 token 故障 / 新增一類消費端)
    才重開,且帶那個具體事件進來,不是「讓它更好」的泛想。
