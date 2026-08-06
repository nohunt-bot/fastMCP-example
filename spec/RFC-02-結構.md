# RFC-02：檔案系統、Manifest、Skill 規格

## 3. 檔案系統標準

### 3.1 標準目錄結構

```
<skill-root>/
└── <skill-name>/                 REQUIRED  目錄名 SHOULD 等於 manifest.name
    ├── SKILL.md                  REQUIRED  唯一必要檔案
    ├── scripts/                  OPTIONAL  唯一可執行位置
    │   ├── <name>.sh
    │   ├── <name>.py
    │   └── <name>.js
    ├── references/               OPTIONAL  按需讀取的內容
    │   └── <name>.md
    ├── hooks/                    OPTIONAL  強制檢查
    │   ├── pre.py
    │   └── post.py
    └── assets/                   OPTIONAL  非文字資源
```

支援一層命名空間：

```
<skill-root>/
└── <team>/
    └── <skill-name>/
        └── SKILL.md
```

**RFC-020** Server MUST 掃描 Skill Root 底下一層與兩層的 `SKILL.md`。
Server MUST NOT 掃描更深的層級。

**理由**：無界的遞迴會讓一次誤設的路徑掃描整個檔案系統。

### 3.2 各路徑規格

#### 3.2.1 `SKILL.md`

| 項目 | 規格 |
|---|---|
| 用途 | 定義 Skill 的 Manifest 與 Body |
| 位置 | Skill 目錄的根，**MUST NOT** 在子目錄 |
| 檔名 | 精確為 `SKILL.md`，**大小寫敏感** |
| 編碼 | UTF-8，**MUST NOT** 有 BOM |
| 換行 | LF 或 CRLF（皆 MUST 被接受） |
| 大小 | 無硬性上限；> 32 KB SHOULD 產生 `LINT-011` 警告 |

**RFC-021** `SKILL.md` MUST 以 UTF-8 編碼且 MUST NOT 含 BOM。

**理由**：BOM 會使 frontmatter 的起始 `---` 不在第 0 位元組，導致整份
frontmatter 被忽略——症狀是「Skill 存在但 description 空白」，難以診斷。

#### 3.2.2 `scripts/`

| 項目 | 規格 |
|---|---|
| 用途 | 可被 `run_skill_script` 執行的檔案 |
| 位置 | **唯一**可執行位置 |
| 副檔名 | MUST 在直譯器白名單內 |
| 執行位元 | **無意義**。白名單才決定可否執行 |

**RFC-022** Runtime MUST 拒絕執行 `scripts/` 以外的任何檔案。

**RFC-023** Runtime MUST 以副檔名決定直譯器，MUST NOT 依賴執行位元或
shebang。

**理由**：執行位元由檔案系統與 git 決定，不是刻意的授權決策。白名單是。

| 副檔名 | 直譯器 | 備註 |
|---|---|---|
| `.sh` | `/bin/bash` | 啟動成本最低 |
| `.py` | `<python> -I` | `-I` 隔離模式，忽略 `PYTHON*` 與 cwd |
| `.js` | `node` | 僅在 node 存在時註冊 |

**RFC-024** Python Script MUST 以隔離模式（`-I`）執行。

#### 3.2.3 `references/`

| 項目 | 規格 |
|---|---|
| 用途 | 不需每次載入的細節 |
| 讀取 | 僅透過 `read_skill_file` |
| 格式 | 任意；文字內容 SHOULD 為 Markdown |

**RFC-025** `read_skill_file` 的回應 MUST 受 Context Budget 約束。

**理由**：一份 400 KB 的 Reference 約 114,285 tokens。對 30 K context 的
模型而言不可用，而且**外觀是「讀取成功」而非錯誤**，難以察覺。

#### 3.2.4 `hooks/`

| 項目 | 規格 |
|---|---|
| 檔名 | 精確為 `pre.<ext>` / `post.<ext>` |
| 位置 | Skill 目錄下的 `hooks/` |
| 可見性 | **MUST NOT** 出現在 Card 或 `files` 清單 |

**RFC-026** Hook 檔案 MUST NOT 被列為模型可讀的內容。

**理由**：Hook 是機制不是內容，列出只是消耗 token。

**RFC-027** Hook 的解析路徑 MUST 通過與 Script 相同的 jail 檢查，jail
根目錄為其所屬目錄（Skill Hook 為 Skill 目錄，全域 Hook 為全域 Hook 目錄）。

**理由**：`hooks/pre.py` 若為指向外部的符號連結，將在**每一次呼叫**時執行
任意程式。此攻擊實際可行過。

### 3.3 禁止的模式

| 模式 | 規則 | 理由 |
|---|---|---|
| Skill 目錄外的符號連結 | **RFC-028** MUST 拒絕 | 繞過路徑檢查的標準手法 |
| `scripts/` 外的可執行檔 | **RFC-022** MUST 拒絕 | 參考文件不應被當程式執行 |
| Server 寫入任何檔案 | **RFC-041** MUST NOT | 唯讀根檔案系統 |
| 名稱含路徑分隔字元 | **RFC-031** MUST 拒絕 | 路徑穿越 |
| `SKILL.md` 大小寫變體 | **RFC-021** MUST NOT 接受 | 跨平台不一致 |

**RFC-028** 所有路徑解析 MUST 先 `resolve()`（跟隨符號連結）再檢查是否
包含於 jail 根目錄內。

```
# 正確順序
target = (base / relpath).resolve()
if not target.is_relative_to(base.resolve()):
    reject()
```

### 3.4 檔案系統驗證範例

**通過：**

```
skills/order-lookup/SKILL.md
skills/order-lookup/scripts/query.sh
skills/order-lookup/references/error-codes.md
skills/finance/reconciliation/SKILL.md
```

**失敗：**

| 路徑 | 違反 |
|---|---|
| `skills/order-lookup/skill.md` | RFC-021（大小寫） |
| `skills/order-lookup/query.sh` | RFC-022（不在 `scripts/`） |
| `skills/a/b/c/SKILL.md` | RFC-020（超過兩層） |
| `skills/x/scripts/run.rb` | RFC-023（副檔名不在白名單） |

---

## 4. Manifest RFC

Manifest 是 `SKILL.md` 開頭以 `---` 圍住的 YAML。

### 4.1 格式

**RFC-029** Manifest MUST 以 `---` 作為第一行開始，以 `---` 單獨一行結束。

**RFC-030** Manifest MUST 為合法 YAML 1.2，MUST 以 `SafeLoader` 等價的
方式解析（MUST NOT 允許任意物件建構）。

**RFC-030a** Manifest 解析失敗時，Server MUST NOT 中止；MUST 記錄警告並
將該 Skill 視為無 Manifest。

### 4.2 屬性總表

| 屬性 | 型別 | 必要 | 預設 | 相容性影響 |
|---|---|---|---|---|
| `name` | string | RECOMMENDED | 目錄名 | 變更即為 breaking |
| `description` | string | **REQUIRED** | — | 變更不影響 API |
| `version` | string | OPTIONAL | 無 | 僅記錄 |
| `tags` | string[] | OPTIONAL | `[]` | 移除為 breaking（過濾行為改變） |
| `license` | string | OPTIONAL | 無 | 無 |
| `allowed-tools` | string[] | OPTIONAL | `[]` | 透傳，不強制 |
| `execution` | object | OPTIONAL | `{}` | 改變 timeout 行為 |

### 4.3 `name`

| 項目 | 規格 |
|---|---|
| 用途 | Skill 的查詢鍵，也是識別碼 |
| 型別 | string |
| 必要 | RECOMMENDED（省略時使用目錄名） |
| 允許值 | 見下方 EBNF |
| 長度 | 1–64 字元 |

```ebnf
name        = segment , { "-" , segment } ;
segment     = alnum , { alnum } ;
alnum       = "a".."z" | "0".."9" ;
```

**RFC-031** `name` MUST 符合 `^[a-z0-9]+(-[a-z0-9]+)*$` 且長度 MUST NOT
超過 64。

**RFC-032** 不符合 RFC-031 的 Skill MUST NOT 被載入，且 MUST 出現在可查詢
的 rejected 清單中，附帶可行動的原因。

**理由**：強制而非建議，因為 `name` 同時是查詢鍵，也可能被用於構成路徑。
名為 `../../etc` 的 Skill 曾能使狀態寫出根目錄之外。

**RFC-033** 名稱重複時，Server MUST 保留其中一個並記錄警告。Server
SHOULD 以確定性順序（如路徑字典序）決定保留者。

| 值 | 結果 |
|---|---|
| `order-lookup` | ✓ |
| `csv-profile` | ✓ |
| `a1` | ✓ |
| `Order-Lookup` | ✗ 大寫 |
| `order_lookup` | ✗ 底線 |
| `訂單查詢` | ✗ 非 ASCII |
| `order lookup` | ✗ 空白 |
| `-lead` / `trail-` | ✗ 連字號在端點 |
| `../../etc` | ✗ 路徑字元 |
| 65 字元 | ✗ 超長 |

### 4.4 `description`

| 項目 | 規格 |
|---|---|
| 用途 | **模型的路由依據**。唯一每次對話都載入的文字 |
| 型別 | string |
| 必要 | **REQUIRED** |
| 長度 | 1–1024 字元 |
| 語言 | 任意（MAY 為非 ASCII） |

**RFC-034** `description` MUST 存在且 MUST NOT 超過 1024 字元。超過者
MUST NOT 被載入。

**RFC-035** `description` SHOULD 同時說明「做什麼」與「什麼時候使用」。

**理由**：`description` 是每次對話的固定成本，乘以 Skill 數量。它同時是
模型唯一的路由依據——只寫標題（如「訂單工具」）無法支撐選擇決策。

### 4.5 `execution`

```yaml
execution:
  default:
    timeout: 60
    stall_timeout: 20
  scripts/submit.sh:
    timeout: 15
    description: 送出任務，API 立刻回 uuid 後自己跑完
```

| 子屬性 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `timeout` | number > 0 | Server 預設 | wall-clock 上限（秒） |
| `stall_timeout` | number ≥ 0 | Server 預設 | 靜默判定秒數，`0` 停用 |
| `description` | string | `""` | 顯示於 Card 的自然語言說明 |

**RFC-036** `execution` MUST NOT 宣告任何描述輸出「語意」的欄位（例如
執行模式、job key 欄位名）。

**理由**：Server 對「回傳 handle」與「回傳結果」的處理完全相同——原樣回傳
stdout。模式旗標不改變任何行為，只使 Server 去猜測它已經持有的東西，而
猜測會出錯（見 D-2）。

**RFC-037** Server MUST 將 `timeout` 鉗制在 Server 設定的上限內。

**RFC-038** Card MUST 揭露每支 Script 的 `timeout` 與 `description`。

**理由**：模型需要在執行前知道預期的耗時規模與 Script 的用途。

### 4.6 `allowed-tools`

**RFC-039** Server MUST 解析並透傳 `allowed-tools`，MUST NOT 嘗試強制執行。

**理由**：Server 不擁有 Client 的工具清單，沒有能力強制。若 Client 未實作，
此欄位僅為註解——本規範明確標示此為能力邊界。

### 4.7 Manifest JSON Schema

見 [`schemas/manifest.schema.json`](schemas/manifest.schema.json)。

### 4.8 完整範例

```markdown
---
name: order-lookup
description: 查詢內部訂單系統的狀態與明細。當使用者問到訂單編號、出貨狀態或退款進度時使用。
version: 1.2.0
tags: [order, internal, query]
license: proprietary
allowed-tools: [Read, Grep]
execution:
  default:
    timeout: 30
  scripts/query.sh:
    timeout: 15
    description: 依訂單編號查詢，回傳單筆明細
---

# 訂單查詢
...
```

---

## 5. Skill 規格 RFC

### 5.1 Body

**RFC-040** Body MUST 為 CommonMark 0.30 相容的 Markdown。

**RFC-040a** `load_skill` 的回應 MUST 受 Context Budget 約束。

**RFC-040b** Server MUST 支援以標題擷取 Body 的單一小節。小節比對
MUST 為大小寫不敏感且 MUST 支援前綴比對。

**理由**：要求模型逐字重現標題會產生不必要的失敗。

### 5.2 Body 的最低結構

**RFC-040c** Body SHOULD 包含以下小節：

| 小節 | 內容 |
|---|---|
| 用法 | 可直接照抄的呼叫範例 |
| 解讀結果 | 欄位意義與邊界情況 |
| 不適用的情況 | 避免模型硬套 |

### 5.3 無狀態要求

**RFC-041** Server MUST NOT 建立、寫入或修改任何檔案。Server MUST NOT
向 Script 提供可寫目錄。

**可驗證條件**：在 `readOnlyRootFilesystem: true` 的容器中執行完整工具
呼叫循環後，`docker diff` MUST 回傳零列。

**理由**：曾預設建立狀態目錄，在唯讀容器中導致**每一支** Script 失敗。
無狀態使得不需 volume、pod 可任意丟棄、副本間無狀態同步。

**RFC-042** 需要跨呼叫存續的識別碼（例如非同步任務的 handle）MUST 由
呼叫端保管，其可靠性 MUST 由後端 API 的 idempotency 機制提供。

**規範性說明**：送出請求 SHALL 接受由呼叫端產生的 idempotency key；以
相同 key 重送 SHALL 回傳相同的識別碼而非建立第二個任務。只有後端 API
知道什麼構成「同一個任務」，此保證 MUST NOT 在 MCP 層實作。

### 5.4 繼承與覆寫

**RFC-043** `execution` 的解析順序 MUST 為：

```
呼叫端明確指定  >  execution.<script>  >  execution.default  >  Server 預設
```

**RFC-044** Skill 之間 MUST NOT 有繼承關係。每個 Skill MUST 自我完備。

**理由**：Skill 繼承會使 Card 無法在不讀取其他 Skill 的情況下計算，破壞
漸進式揭露的成本模型。

### 5.5 擴充規則

**RFC-045** Manifest 中未知的頂層屬性 MUST 被忽略且 MUST NOT 導致拒絕。

**RFC-046** 組織自訂的擴充屬性 MUST 以 `x-` 為前綴。

**理由**：使本規範的未來版本可新增屬性而不與既有擴充衝突。

### 5.6 Skill JSON Schema

見 [`schemas/skill.schema.json`](schemas/skill.schema.json)。
