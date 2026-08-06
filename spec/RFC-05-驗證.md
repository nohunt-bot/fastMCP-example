# RFC-05：驗證規則總表

每條規則都在 `spec/validate.py` 中實作。表格的「偵測」欄描述演算法，
「自動修正」欄標示是否可機器修正。

執行：

```bash
uv run python -m spec.validate <路徑> [--recursive] [--format=json] [--level=L2]
```

離開碼：`0` 通過、`1` 有 error（L2 以上含 warning）、`2` 驗證器本身出錯。

## 10.1 嚴重度定義

| 嚴重度 | 意義 | 對載入的影響 | L1 | L2 | L3 |
|---|---|---|---|---|---|
| `error` | 違反 MUST | **Skill 不被載入** | 阻擋 | 阻擋 | 阻擋 |
| `warning` | 違反 SHOULD | 仍載入 | 通過 | 阻擋 | 阻擋 |
| `info` | 提示 | 仍載入 | 通過 | 通過 | 通過 |

## 10.2 結構驗證（VAL-0xx）

| 規則 | 嚴重度 | 描述 | 偵測 | 自動修正 | RFC |
|---|---|---|---|---|---|
| VAL-000 | error | 目標下找不到任何 SKILL.md | 遞迴搜尋為空 | ✗ | RFC-020 |
| VAL-001 | error | Skill 目錄缺少 SKILL.md | `(dir/"SKILL.md").is_file()` | ✗ | RFC-021 |
| VAL-002 | error | 檔案含 UTF-8 BOM | 前三位元組為 `EF BB BF` | ✓ | RFC-021 |
| VAL-003 | error | 缺少 frontmatter | 開頭非 `---` | ✗ | RFC-029 |
| VAL-004 | error | frontmatter 未結束或超過 16 KB | 找不到收尾 `\n---` | ✗ | RFC-029 |
| VAL-005 | error | frontmatter 非合法 YAML | `yaml.safe_load` 拋錯 | ✗ | RFC-030 |
| VAL-006 | error | frontmatter 非對映 | 解析結果非 dict | ✗ | RFC-030 |
| VAL-010 | error | name 不符命名規則 | 不符 `^[a-z0-9]+(-[a-z0-9]+)*$` | ✗ | RFC-031 |
| VAL-011 | error | name 超過 64 字元 | `len(name) > 64` | ✗ | RFC-031 |
| VAL-012 | error | 缺少 description | 不存在或空白 | ✗ | RFC-034 |
| VAL-013 | error | description 超過 1024 字元 | `len > 1024` | ✗ | RFC-034 |
| VAL-014 | error | tags 非陣列 | 型別檢查 | ✗ | RFC-030 |
| VAL-020 | error | execution 非對映 | 型別檢查 | ✗ | RFC-036 |
| VAL-021 | error | execution 鍵不是 default 或 scripts/ 路徑 | 前綴比對 | ✗ | RFC-036 |
| VAL-022 | error | execution 指向不存在的 script | 與實際檔案集合比對 | ✗ | RFC-043 |
| VAL-023 | error | execution policy 非對映 | 型別檢查 | ✗ | RFC-036 |
| VAL-024 | error | execution 含 mode/key_field | 鍵存在性檢查 | ✓ | RFC-036 |
| VAL-025 | error | timeout 非正數 | 型別與範圍 | ✗ | RFC-037 |
| VAL-026 | error | stall_timeout 為負 | 範圍檢查 | ✗ | RFC-036 |
| VAL-030 | error | scripts/ 下有不可執行的副檔名 | 副檔名白名單 | ✗ | RFC-023 |
| VAL-031 | error | script 路徑含子目錄 | 路徑 regex | ✗ | RFC-022 |
| VAL-040 | error | script 使用可寫狀態目錄 | 原始碼比對 | ✗ | RFC-041 |
| VAL-050 | error | Body 指向不存在的 reference | 反向連結檢查 | ✗ | RFC-025 |

## 10.3 安全驗證（SEC-0xx）

| 規則 | 嚴重度 | 描述 | 偵測 | RFC |
|---|---|---|---|---|
| SEC-001 | error | 符號連結指向 Bundle 之外 | `resolve()` 後檢查包含性 | RFC-028 |
| SEC-002 | error | 可執行副檔名在 scripts/ 之外 | 路徑 + 副檔名 | RFC-022 |
| SEC-010 | error | requests 呼叫缺少 timeout | 原始碼比對（去註解後） | RFC-050 |
| SEC-011 | error | urlopen 缺少 timeout | 同上 | RFC-050 |
| SEC-012 | error | curl 缺少 --max-time | 檔案層級比對 | RFC-050 |
| SEC-013 | warning | script 覆寫 PATH 等危險變數 | 賦值比對 | RFC-052 |

### 執行期安全（無法靜態偵測，MUST 以測試驗證）

| 規則 | 描述 | 驗證方式 |
|---|---|---|
| SEC-020 | 呼叫端不得設定 PATH / LD_* / PYTHON* | 單元測試，每個變數一個案例 |
| SEC-021 | 路徑穿越必須被拒絕 | 參數化測試（`../`、絕對路徑、混合） |
| SEC-022 | 伺服器環境的機密不得被繼承 | 設一個變數後檢查子行程看不到 |
| SEC-023 | stdin 必須有上限 | 送出超量後預期被拒絕 |
| SEC-024 | Hook 的 symlink 逃逸必須被拒絕 | 建立指向外部的 hook 連結 |

## 10.4 Lint 規則（LINT-0xx）

| 規則 | 嚴重度 | 描述 | 自動修正 |
|---|---|---|---|
| LINT-001 | warning | version 不是 SemVer | ✗ |
| LINT-002 | warning | description 過短（< 20 字元） | ✗ |
| LINT-004 | warning | tag 不是 kebab-case | ✓ |
| LINT-005 | info | 未知的 manifest 屬性 | ✗ |
| LINT-006 | warning | timeout 超過 900 秒上限 | ✓ |
| LINT-007 | warning | execution policy 有未知屬性 | ✗ |
| LINT-008 | warning | hooks/ 下有 pre/post 以外的檔案 | ✗ |
| LINT-009 | info | 檔案不在標準目錄內 | ✗ |
| LINT-011 | warning | Body 超過 32 KB | ✗ |
| LINT-012 | warning | Body 為空 | ✗ |
| LINT-020 | warning | shell script 未設定 set -e | ✓ |
| LINT-021 | warning | pipefail 搭配 \| head | ✓ |
| LINT-030 | info | 規則已被豁免（附理由） | — |
| LINT-040 | warning | description 與其他 skill 過於相似 | ✗ |

### LINT-040 是唯一需要全域視角的規則

其他規則都能單獨看一個 Bundle 判斷。可區分性不行——單看任何一個
`description` 永遠合格，問題只在它跟別的擺在一起時才出現，而**模型看到的
正是擺在一起的樣子**。

門檻：CJK bigram + 英文詞的 Jaccard 相似度 ≥ **0.35**。

校準自對照實驗：

| description 寫法 | 相似度範圍 |
|---|---|
| 照直覺寫（「查詢訂單狀態。提供訂單狀態的查詢功能。」） | 0.43 – 0.57 |
| 「做什麼 + 何時使用 + 使用者口語」 | 0.11 – 0.14 |

兩個分佈完全不重疊，0.35 取在中間偏保守處。切詞方式與服務端的檢索一致，
因此量到的就是模型實際會遇到的混淆程度。

少於 4 個 token 的 description 跳過比較，避免誤報。

**RFC-035a** 同一個 Skill Root 內，任兩個 `description` 的相似度
SHOULD NOT 達到 0.35。

**理由**：選錯 Skill 是本系統最常見的失敗模式，且上線後難以歸因——模型
不會回報「我在兩個之間猶豫」，它只會選一個然後給出錯誤的答案。這個問題
在撰寫當下可被偵測，不必等到營運階段。

## 10.5 豁免機制

**RFC-055** 驗證器 MUST 支援檔案層級的規則豁免，且豁免 MUST 附帶理由。

```bash
# spec:allow LINT-020 需要自行判斷 curl 的離開碼，用 -e 會直接中止
set -uo pipefail
```

語法：

```ebnf
suppression = "#" , WSP , "spec:allow" , WSP , rule-id , WSP , reason ;
rule-id     = ("VAL" | "LINT" | "SEC" | "PERF") , "-" , 3digit ;
reason      = ? 至少一個非空白字元，直到行尾 ? ;
```

**RFC-056** 豁免 MUST 產生一筆 `LINT-030` info 記錄，使其在報告中可見。

**RFC-057** `error` 等級的安全規則（SEC-001、SEC-002、SEC-010、SEC-011、
SEC-012、VAL-040）MUST NOT 可被豁免。

**理由**：沒有理由的豁免等於關閉規則。可見的豁免可被審查；靜默的豁免不能。
而安全規則的豁免應該透過修改規範，不是逐檔繞過。

## 10.6 效能驗證（PERF-0xx）

無法靜態偵測，MUST 以測試與量測驗證。

| 規則 | 要求 | 驗證方式 | 參考實作實測 |
|---|---|---|---|
| PERF-001 | 目錄回應 MUST ≤ Context Budget | 造 300+ Skill 後檢查回應大小 | 78% → 9% |
| PERF-002 | `list_skills` MUST NOT 做磁碟 I/O | 程式碼審查 + 延遲量測 | p50 11.5 ms |
| PERF-003 | 無變動的索引更新 MUST 為 O(skill 數) 次 stat | 500 Skill 計時 | 2.8 ms |
| PERF-004 | 冷啟動 MUST 在探針 initialDelay 內完成 | 受限容器計時 | 42 s @ 0.1 core |
| PERF-005 | Script 輸出 MUST 受預算約束 | 大輸出測試 | 59,222 → 442 tokens |

**RFC-058** 宣稱 L3 符合性的服務 MUST 在其資源限制下量測 PERF-001 至
PERF-005 並公佈數字。

**RFC-059** 效能數字 MUST 標註量測環境（CPU 限制、記憶體、是否唯讀）。
以完整 CPU 的數字推算受限環境 MUST NOT 被接受為量測。

**理由**：以完整 CPU 數字乘以比例推算受限環境，實測誤差達 **14 倍**
（推算冷啟動 3 秒，實測 42 秒）。推算漏掉冷 page cache、首次 import、
以及 CFS 將工作切成數十個週期的效應。
