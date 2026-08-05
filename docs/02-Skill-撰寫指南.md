# 02 Skill 撰寫指南

格式與 Claude Code 相同，現有的 skill 可以直接搬過來用。

## 目錄結構

```
skills/
└── order-lookup/            ← 目錄名與 name 用 kebab-case
    ├── SKILL.md             ← 必要
    ├── scripts/             ← 可執行程式，只有這個目錄能執行
    │   └── query.py
    ├── references/          ← 需要時才讀的參考文件
    │   └── error-codes.md
    └── hooks/               ← 選用的檢查機制
        ├── pre.py
        └── post.py
```

## 命名規則（與 Claude Code 相同，且會強制檢查）

`name` 只能是**小寫英文字母、數字、連字號**，最多 64 字元：

```
order-lookup      ✓
csv-profile       ✓
Order-Lookup      ✗  大寫
order_lookup      ✗  底線
訂單查詢           ✗  非 ASCII
```

不合規的 skill **不會被載入**，並且會出現在 `skill_server_stats()` 的
`index.rejected` 裡，附上原因。這是強制而非建議，因為 `name` 同時是查詢鍵，
也（淨化後）會變成狀態目錄的名稱——名稱裡有 `../` 會讓狀態寫到根目錄外面。

`description` 上限 1024 字元（Claude Code 的限制）。超過會被拒絕。

**中文寫在 `description` 和內文裡，`name` 用英文。** description 是給模型讀的，
中文完全沒問題。

也支援多一層命名空間，方便分團隊：

```
skills/
└── finance/
    └── reconciliation/
        └── SKILL.md
```

新增 skill 只要建目錄、寫 `SKILL.md`。**不用註冊、不用重啟**，5 秒內自動生效。

## SKILL.md 最小範例

```markdown
---
name: order-lookup
description: 查詢內部訂單系統的狀態與明細。當使用者問到訂單編號、出貨狀態或退款進度時使用。
---

# 訂單查詢

## 用法

...
```

只有 `name` 和 `description` 會在索引階段被讀取。

## description 是最重要的一行

`description` 是**唯一**會出現在每次對話裡的文字（`list_skills` 只回傳這個）。
模型靠它決定要不要載入這個 skill。

把它當成**路由判斷**來寫，不是標題：

```yaml
# 不好 —— 這是標題，模型無法據此決定
description: 訂單工具

# 好 —— 說明「做什麼」和「什麼時候用」
description: 查詢內部訂單系統的狀態與明細。當使用者問到訂單編號、出貨狀態或退款進度時使用。
```

寫的時候問自己：**模型只看到這一行，能不能正確判斷該不該用？**

## 完整 frontmatter

```yaml
---
name: internal-api
description: 呼叫內部工作 API。submit.py 送出長任務並回傳 uuid；report.py 等待同步結果。
version: 1.0.0
tags: [api, internal, jobs]
execution:
  default:
    timeout: 60
  scripts/submit.py:
    timeout: 15
    stall_timeout: 10
    description: 送出任務，API 立刻回 uuid 後自己跑完
  scripts/report.py:
    timeout: 300
    stall_timeout: 30
    description: 等 API 回傳完整答案，過程中會印心跳
---
```

| 欄位 | 必要 | 說明 |
|---|---|---|
| `name` | 否 | 省略時用資料夾名稱。**必須 kebab-case** |
| `description` | **是** | 模型的路由依據 |
| `version` | 否 | 純記錄用 |
| `tags` | 否 | 供 `list_skills(tags=[...])` 過濾 |
| `execution` | 否 | 執行模式宣告，見下節 |
| `allowed-tools` | 否 | Claude Code 欄位。本服務透傳給客戶端，不自行強制 |
| `license` | 否 | 純記錄用 |

## execution：宣告 timeout（重要）

`execution` 只宣告一件事：**每支 script 允許跑多久**。這是唯一無法從輸出得知的
資訊，因為它必須在執行前決定。

```yaml
execution:
  default:
    timeout: 60
  scripts/submit.py:
    timeout: 15
    stall_timeout: 10
    description: 送出任務，API 立刻回 uuid 後自己跑完；把 uuid 交給呼叫端即可
  scripts/report.py:
    timeout: 300
    stall_timeout: 30
    description: 等 API 回傳完整答案，過程中會印心跳
```

| 欄位 | 說明 |
|---|---|
| `timeout` | 硬性上限，超過就砍掉 |
| `stall_timeout` | 靜默多久判定卡住（`0` 關閉） |
| `description` | 給模型看的自然語言說明，會出現在 `list_skills` 卡片上 |

## 為什麼沒有 background / sync 模式

因為**沒有必要**。script 印什麼，那就是答案：

```
送出型 script 印 {"key": "7f3a-uuid"}   → 呼叫端拿到 uuid，交給前端，停手
等待型 script 印 {"rows": 842}          → 呼叫端拿到答案
```

服務對這兩種情況做的事**完全相同**：原樣回傳 stdout。加一個模式旗標只會讓服務
去猜它已經拿到的東西是什麼意思——而猜測會出錯：

| script 輸出 | 自動猜測會提取的「任務代號」 |
|---|---|
| `{"key": "7f3a-uuid", "status": "accepted"}` | `7f3a-uuid` ✓ |
| `{"id": 123, "order_no": "SO-001"}` | `123` ✗ 那是訂單 id |
| `{"id": "u-88", "name": "Amy"}` | `u-88` ✗ 那是使用者 id |

一個錯的任務代號比沒有更糟，因為它看起來是對的。所以服務不猜——**script 印出
的內容就是最終答案**。

要讓模型知道某支 script 是送出型，用 `description` 說明就好，那比 enum 更精確
也更靈活。

## timeout 怎麼設

| 情況 | `timeout` | 理由 |
|---|---|---|
| 送出後立刻回 uuid | **10–20 秒** | 應該幾百毫秒就回，設短才抓得到問題 |
| 等待 API 回完整結果 | 60–300 秒 | 看實際需要 |
| 純本機運算 | 30–60 秒 | 沒有網路變數 |

短 `timeout` 不只是保護，它是**斷言**：你在說「這應該馬上回」。所以超時的時候，
服務的診斷會直接指出端點在回覆前就把工作做完了、async 邊界切錯邊，而不是叫你
調大 timeout：

> 這支 script 的 15 秒上限代表它應該立即回應 —— 通常是送出工作後回傳一個代號。
> 超時通常表示端點在回覆前就把工作做完了，async 邊界切錯邊。調大 timeout 只會
> 掩蓋它，不會修好它。

長 `timeout` 超時則是相反的建議：真的慢，調大 timeout 才對。

## 內文怎麼寫

內文是模型呼叫 `load_skill` 之後才會看到的。寫給模型看，不是寫給人看的說明書。

好的內文包含：

1. **怎麼呼叫** —— 具體的參數範例，可以直接照抄
2. **怎麼解讀結果** —— 欄位意義、邊界情況
3. **什麼時候不該用** —— 避免模型硬套

```markdown
# 訂單查詢

## 用法

run_skill_script("order-lookup", "scripts/query.py", ["--order-no", "SO-2026-000123"])

## 解讀結果

- `status` 為 `shipped` 時，`tracking_no` 才有值
- `amount` 是未稅金額
- 查無資料回傳 `{"found": false}`，不是錯誤

## 不適用的情況

跨年度的歷史訂單不在這個系統，要查 2024 年以前的請用「歷史訂單查詢」。
```

### 用小節切開

內文很長時分小節，模型可以只載入需要的部分：

```
load_skill("order-lookup", section="用法")
```

會只回傳那個標題底下的內容，省 token。

## references/ 放什麼

放**不是每次都需要**的細節：欄位對照表、錯誤碼、範例輸出、邊界情況說明。

在內文裡指路：

```markdown
完整的錯誤碼對照見 `references/錯誤碼.md`。
```

模型需要時才會呼叫 `read_skill_file` 讀取。這是第三層揭露。

## 檢查清單

新增 skill 之後：

- [ ] `description` 寫的是「做什麼 + 什麼時候用」，不是標題
- [ ] 會呼叫 API 的 script 在 `execution` 宣告了 timeout
- [ ] 送出型的 script 設了短 timeout（10-20 秒）
- [ ] 等待型的 script 會輸出心跳
- [ ] 內文有可直接照抄的呼叫範例
- [ ] 不常用的細節放在 `references/`
- [ ] `name` 是 kebab-case（`order-lookup`，不是 `訂單查詢` 或 `order_lookup`）
- [ ] 執行 `uv run python acceptance.py --group 索引` 確認能被掃到
- [ ] 執行 `uv run python acceptance.py --group 規格對齊` 確認符合 Claude Code 規格
