# RFC-03：Prompt、Tool、Resource

## 6. Prompt RFC

在本規範中，Prompt 指 Skill 的 **Body** 與 **Card**——它們是進入模型 context
的文字。Server 不提供 MCP `prompts/*` 能力。

**RFC-060** Server MUST NOT 揭露 MCP prompt 能力。所有給模型的指示 MUST
透過 Skill Body 傳遞。

**理由**：兩套指示機制會產生優先權衝突，且 prompt 不受 Progressive
Disclosure 的成本模型約束。

### 6.1 Prompt 語法

Body 是純 CommonMark。**沒有樣板引擎、沒有變數替換、沒有繼承。**

```ebnf
body        = { block } ;
block       = heading | paragraph | code-block | list | table ;
heading     = "#" , { "#" } , WSP , text , LF ;
code-block  = "```" , [ lang ] , LF , { line } , "```" , LF ;
```

**RFC-061** Body MUST NOT 包含由 Server 求值的樣板語法。

**理由**：樣板求值是注入面。Body 的內容來自檔案，而檔案可能被外部流程寫入；
若 Server 對其求值，等於執行了未經審查的表達式。變動的部分屬於 Script 的
參數，不屬於指示文字。

**RFC-062** Body 中若出現形似樣板的字元序列（`{{ }}`、`${ }`），Server
MUST 原樣輸出。

### 6.2 小節擷取

**RFC-063** Server MUST 支援以標題名稱擷取 Body 的單一小節。

```ebnf
section-ref  = heading-text ;
match        = case-insensitive , prefix ;
extent       = from matched heading , until heading of same-or-higher level ;
```

| 呼叫 | 結果 |
|---|---|
| `load_skill("x", section="用法")` | `## 用法` 到下一個 `##` 或 `#` 之前 |
| `load_skill("x", section="用")` | 同上（前綴比對） |
| `load_skill("x", section="不存在")` | 錯誤，**MUST** 附上可用標題清單 |

**RFC-064** 小節不存在時，錯誤訊息 MUST 包含最多 20 個可用標題。

**理由**：讓模型能自我修正，而不是卡住或猜測。

### 6.3 Card 組成

**RFC-065** Card MUST 只包含下列欄位，MUST NOT 包含其他任何內容：

| 欄位 | 條件 |
|---|---|
| `name` | 一律 |
| `description` | 一律 |
| `tags` | 有宣告時 |
| `scripts` | 有 Script 時，值為 `{路徑: {timeout, about}}` |
| `allowed_tools` | 有宣告時 |

**RFC-066** Card 的總和 MUST 受 Context Budget 約束。超過時 Server MUST
以下列兩種檢視之一回應——**逐筆截斷**（`list`）或**領域總覽**
（`overview`）；哪一種適用、各自的形狀，見 7.3.2。無論哪一種，回應 MUST
明確標示被省略的數量（或範圍）與縮小範圍的方法。

**理由（PERF-001）**：500 個 Skill 的完整目錄實測約 23,467 tokens，佔
30 K context 的 **78%**——在使用者說任何話之前。套用預算後降至 2,646 tokens
（9%）。這是 Progressive Disclosure 第 1 層唯一可能失控的地方。

### 6.4 衝突解析

**RFC-067** 兩個 Skill 的 `description` 語意重疊時，Server MUST NOT 嘗試
消歧義。消歧義屬於 `description` 的撰寫品質，由 LINT-002 與人工審查處理。

### 6.5 本地化

**RFC-068** `description` 與 Body MAY 為任何語言。`name` MUST 為 ASCII
（RFC-031）。

**RFC-069** Server MUST NOT 提供翻譯或語言協商。需要多語言時，MUST 以
不同的 Skill 提供，並在 `description` 中標明語言。

---

## 7. Tool RFC

### 7.1 必要工具集

**RFC-070** 符合本規範的 Server MUST 揭露且僅揭露下列工具：

| 工具 | 層 | 唯讀 | 用途 |
|---|---|---|---|
| `list_skills` | 1 | ✓ | 回傳 Card |
| `load_skill` | 2 | ✓ | 回傳 Body 或小節 |
| `read_skill_file` | 3 | ✓ | 回傳 Reference |
| `run_skill_script` | 4 | ✗ | 執行 Script |
| `skill_server_stats` | — | ✓ | 索引、行程、延遲統計 |
| `reload_skills` | — | ✗ | 立即重新掃描 |

**RFC-071** Server MUST NOT 新增未經本規範定義的工具。組織擴充 MUST 透過
新增 Skill 而非新增工具。

**理由**：工具清單是每次對話的固定成本，且是 Client 的相容性介面。新增
工具會使所有 Client 必須更新。新增 Skill 不會。

### 7.2 命名規範

```ebnf
tool-name = lower , { lower | digit | "_" } ;
lower     = "a".."z" ;
```

**RFC-072** 工具名稱 MUST 為 `snake_case`，MUST NOT 超過 32 字元。

### 7.3 `list_skills`

```json
{
  "name": "list_skills",
  "readOnlyHint": true,
  "idempotentHint": true,
  "inputSchema": {
    "type": "object",
    "properties": {
      "query": { "type": ["string", "null"], "maxLength": 256 },
      "tags":  { "type": ["array", "null"], "items": { "type": "string" } },
      "limit": { "type": "integer", "minimum": 1, "maximum": 500, "default": 50 }
    },
    "additionalProperties": false
  }
}
```

回應：

```json
{
  "type": "object",
  "required": ["count", "total", "view", "skills"],
  "properties": {
    "count": { "type": "integer" },
    "total": { "type": "integer", "description": "索引中的總數，可能大於 count" },
    "view": { "enum": ["list", "overview"], "description": "RFC-066，見 7.3.2" },
    "skills": { "type": "array" },
    "truncated": {
      "type": "object",
      "required": ["omitted", "hint"],
      "description": "RFC-066a。僅 view=list 時可能出現：因預算被逐筆裁掉時必須存在。"
    },
    "facets": {
      "type": "object",
      "additionalProperties": { "type": "integer" },
      "description": "RFC-066b。僅 view=overview 時存在：領域 -> 該領域的 Skill 數。"
    },
    "hint": {
      "type": "string",
      "description": "RFC-066b。僅 view=overview 時存在：如何用 tags/query 縮小到單一領域。"
    }
  }
}
```

**RFC-073** `total` MUST 反映索引中的真實總數，即使 `skills` 被裁切。

**理由**：模型必須能分辨「只有 3 個 Skill」與「有 300 個但只看得到 3 個」。

#### 7.3.1 檢索：切詞與排序

`query` 不是子字串比對，而是排序過的檢索。這件事本身是規範性的
（RFC-099a），因為中文沒有空白分詞，樸素的 `str.split()` + 子字串比對對
中文近乎失效——實測 13 個真實中文查詢只命中 1 個（**8%**），詳見
[附錄 A](A-事故紀錄.md) 事故 A-14。

**RFC-099a** `query` 的比對 MUST 對中日韓文字（CJK）使用**至少 bigram**
粒度的切詞，MUST NOT 只用空白分詞或全字串子字串比對。

**理由**：中文詞之間沒有空白，「查訂單」不是「查詢內部訂單系統的狀態與
明細」的子字串；bigram（把連續兩個字元當一個 token）是在不引入詞典型
分詞器（啟動成本高，見 RFC-004）的前提下，能同時捕捉「訂單」≠「單訂」
詞序資訊的最小方案。

**RFC-099b** 除了 bigram，切詞 SHOULD 額外保留 CJK **unigram**
（單一字元），且其權重 SHOULD 低於 bigram。拉丁字母／數字／底線構成的
詞 SHOULD 以詞為單位切分（不套用 CJK 規則）。

**理由**：unigram 補救 bigram 抓不到的情況——詞序顛倒（「貨到」查不到
「到貨」）與單字查詢（查「貨」）。純 bigram 對長度 1 的 CJK 輸入完全沒有
token 可切，會直接查無結果。

**RFC-099c** 排序 SHOULD 使用具 IDF（逆文件頻率）特性的演算法，讓到處
出現的字（「的」「與」）自然趨近零權重，而不需要維護一份停用詞清單。

**RFC-099d** 欄位比對 SHOULD 依可信度加權：`name` 命中的可信度 SHOULD
高於 `tags`，`tags` SHOULD 高於 `description`。

**MUST 與 SHOULD 的分界**：RFC-099a（CJK 至少 bigram）是 MUST，因為它
決定「查不查得到」——不遵守的實作會重演 A-14 的 8% 命中率，這是
Progressive Disclosure 第 1 層直接失效。RFC-099b–d 是排序品質的調校，
只影響「先看到誰」，不影響「找不找得到」，因此是 SHOULD：不同實作 MAY
採用不同的 IDF 公式或欄位權重，只要不退化成純子字串比對。

參考實作的實際參數（供互通參考，數值本身不是規範）：

| 項目 | 值 | 說明 |
|---|---|---|
| 排序演算法 | BM25 | `k1=1.2`（詞頻飽和）、`b=0.5`（長度正規化，偏低——文件是 name+description+tags，長 description 不該只因字多被懲罰） |
| unigram 權重 | `0.3` | 相對 bigram 的權重 `1.0` |
| 欄位權重 | `name=3.0`、`tags=2.0`、`description=1.0` | 名稱命中最可信 |

**參考實作與 LINT-040 的一致性**：`spec/validate.py` 的 `LINT-040`（可
區分性檢查）使用與此處相同的 bigram 切詞比較兩個 `description` 的
Jaccard 相似度，這樣量到的相似度才是模型在 `list_skills` 實際會遇到的
混淆程度（見 RFC-05 §10.4）。

### 7.3.2 `list` 與 `overview`：兩種合法的檢視

`list_skills` 的完整目錄可能放不進 Context Budget。直接按名稱字母序
截斷會讓排在後面的整個領域對模型隱形——實測 315 個 Skill、11 個領域時，
模型只看得到前 6 個領域，另外 5 個完全不知道存在，而且**沒有辦法發現
自己漏了什麼**（見附錄 A-10）。因此規範定義兩種回應形狀，依情境擇一。

**RFC-066a** `view` MUST 依下表擇一，MUST NOT 依呼叫順序或其他狀態改變
同一個請求的結果：

| `view` | 何時 | `skills` 的內容 |
|---|---|---|
| `list` | 有 `query` 或 `tags`；或未篩選但**完整目錄**在預算內 | 逐筆 Card |
| `overview` | 未帶 `query` 也未帶 `tags`，且**完整目錄**超過預算 | 每個領域最多若干筆代表 Card |

**RFC-066b** `view=list` 因預算捨棄任何 Card 時，回應 MUST 包含
`truncated: {omitted, hint}`。

**RFC-066c** `view=overview` 時，回應 MUST 包含：

| 欄位 | 內容 | 約束 |
|---|---|---|
| `facets` | 領域 → 該領域的 Skill 數 | MUST 涵蓋**所有**領域，MUST NOT 只列前 N 個 |
| `skills` | 每個領域的代表樣本 | 數量 MAY 受預算限制，但涵蓋的**領域**不受限 |
| `hint` | 如何用 `tags`／`query` 取得單一領域的完整清單 | MUST 可行動 |

**理由**：`overview` 用跟 `list` 差不多的 token 數換取「涵蓋所有領域」而
非「涵蓋所有 Skill」——模型至少要能知道**有什麼**，再自己決定要鑽進
哪個領域，而不是連領域存在都不知道。

### 7.4 `run_skill_script`

```json
{
  "name": "run_skill_script",
  "inputSchema": {
    "type": "object",
    "required": ["name", "script"],
    "properties": {
      "name":   { "$ref": "common.schema.json#/$defs/skillName" },
      "script": { "$ref": "common.schema.json#/$defs/scriptPath" },
      "args":   { "type": ["array", "null"], "items": { "type": "string" },
                  "maxItems": 64 },
      "stdin":  { "type": ["string", "null"], "maxLength": 4194304 },
      "timeout": { "$ref": "common.schema.json#/$defs/timeoutSeconds" },
      "stall_timeout": { "$ref": "common.schema.json#/$defs/stallTimeoutSeconds" },
      "env": { "type": ["object", "null"],
               "additionalProperties": { "type": "string" } },
      "max_output_tokens": { "type": ["integer", "null"],
                             "minimum": 200, "maximum": 100000 }
    },
    "additionalProperties": false
  }
}
```

回應 schema 見 [`schemas/script-result.schema.json`](schemas/script-result.schema.json)。

#### 7.4.1 執行契約

**RFC-074** Server MUST 原樣回傳 stdout，MUST NOT 解析、擷取或重新詮釋其
內容。

**RFC-075** Server MUST 以 `status` 而非 `exit_code` 表達結果分類。

| `status` | 意義 | 呼叫端應對 |
|---|---|---|
| `ok` | 離開碼 0 | 使用 stdout |
| `failed` | 離開碼非 0 | 讀 stderr；**不是**伺服器錯誤 |
| `stalled` | 靜默超過 `stall_timeout` | **阻塞**，不是慢。不要只調大 timeout |
| `timeout` | 到達上限時仍有輸出 | 真的慢 |

**RFC-076** 非零離開碼 MUST 以資料回報，MUST NOT 拋出協定層錯誤。

**RFC-077** `status` 為 `timeout` 或 `stalled` 時，回應 MUST 包含
`hint`，且 `hint` MUST 說明可行動的下一步。

**RFC-078** `hint` 的內容 MUST 依 `timeout` 的量級區分建議：

| 條件 | 診斷方向 |
|---|---|
| ceiling ≤ 30 秒且逾時 | 端點在回覆前完成工作；async 邊界切錯邊。**調大 timeout 會掩蓋而非修正** |
| ceiling > 30 秒且逾時 | 真的慢；調大 timeout 或改為分頁 |
| 停滯 | 阻塞在無 timeout 的網路呼叫；檢查 proxy 變數 |

**理由**：短 ceiling 是作者的斷言（「這應該立刻回」）。把兩種情況給同樣的
建議會把人導向錯誤方向。

#### 7.4.2 逾時、停滯與取消

**RFC-079** Server MUST 對 Script 施加 wall-clock 上限，並 MUST 在超過時
終止**整個行程群組**（`start_new_session` + `killpg`）。

**理由**：只終止直接子行程會留下孤兒。

**RFC-080** Server MUST 偵測停滯：Script 在 `stall_timeout` 內未產生任何
輸出時 MUST 提前終止。

**RFC-081** Server 等待的 MUST 是**行程結束**，而非 stdout 關閉。

**RFC-082** 行程結束後的輸出收尾等待 MUST 受剩餘 timeout 預算約束，
MUST NOT 為固定值。

**理由（實測）**：Script 自身 0.05 秒結束，但其 spawn 的孫行程繼承了
stdout，pipe 因此未 EOF。固定等待 5 秒使 `timeout=2` 的呼叫實際花費 5.05
秒——違反了對呼叫端的承諾。修正後為 2.25 秒。

#### 7.4.3 串流

**RFC-083** Server MUST 在 Script 執行期間串流其 **stderr**，MUST NOT
串流 stdout。

**理由**：stdout 是結果，本來就會完整回傳；重複串流會把整份 payload 灌進
Client 的日誌。因此約定為：**進度走 stderr，結果走 stdout**。

#### 7.4.4 冪等性

**RFC-084** `run_skill_script` MUST NOT 宣稱冪等。

**RFC-085** 需要冪等的操作，其冪等性 MUST 由被呼叫的後端 API 提供
（見 RFC-042）。

### 7.5 分頁

**RFC-086** Server MUST NOT 在工具層實作游標式分頁。輸出縮減 MUST 透過
Context Budget（RFC-066）與 Script 自身的過濾參數達成。

**理由**：游標需要伺服器端狀態，與 RFC-041 衝突。

### 7.6 認證與授權

**RFC-087** Server MUST NOT 實作自有的認證機制。

**RFC-088** 部署 MUST 在傳輸層或閘道層實施存取控制。

**理由**：內部服務的身分應由既有的網路與閘道基礎設施管理；在應用層重造
會產生第二套需要維護的信任來源。

**RFC-089** 傳給 Script 的機密 MUST 透過 `env` 參數傳遞，MUST NOT 透過
`args`。

**理由**：argv 出現在行程列表與日誌中。

### 7.7 工具版本

**RFC-090** 工具的輸入 schema MUST 只以新增選用屬性的方式演進。

**RFC-091** 移除工具、移除屬性、或收緊既有屬性的約束 MUST 視為 breaking
change（見 RFC-08）。

---

## 8. Resource RFC

### 8.1 URI 語法

```ebnf
skill-uri    = "skill://" , skill-name , [ "/files/" , rel-path ] ;
skill-name   = lower-seg , { "-" , lower-seg } ;
lower-seg    = ( "a".."z" | "0".."9" ) , { "a".."z" | "0".."9" } ;
rel-path     = seg , { "/" , seg } ;
seg          = pchar , { pchar } ;
pchar        = ALPHA | DIGIT | "-" | "_" | "." ;
```

**RFC-092** Server MUST 只揭露 `skill://` 協定。

**RFC-093** `rel-path` MUST NOT 包含 `..`，MUST NOT 以 `/` 開頭。

**RFC-094** 所有 Resource 讀取 MUST 通過與 `read_skill_file` 相同的
路徑 jail。

| URI | 結果 |
|---|---|
| `skill://order-lookup` | Body |
| `skill://order-lookup/files/references/x.md` | Reference |
| `skill://order-lookup/files/../../etc/passwd` | **拒絕** |
| `file:///etc/passwd` | **拒絕**（協定不支援） |

### 8.2 內容協商與快取

**RFC-095** Server MUST NOT 實作內容協商。`skill://` 的 MIME type 由
副檔名決定。

**RFC-096** Server MUST NOT 在 Resource 回應中宣告快取指示。

**理由**：Skill 可在任意時刻熱重載，Server 無法保證快取有效期。

### 8.3 壓縮

**RFC-097** 壓縮 MUST 由傳輸層處理，MUST NOT 在應用層實作。

### 8.4 權限

**RFC-098** Resource 讀取 MUST 與工具呼叫套用相同的授權。Resource
MUST NOT 成為繞過工具層檢查的旁路。
