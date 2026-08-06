# MCP Skill Service 規範（RFC-SKILL-1）

本組織所有 MCP Skill 服務的單一事實來源。涵蓋架構、檔案格式、驗證、
安全、測試、CI 與相容性。

## 這份規範的來源

規範中的每一條效能與可靠性規則都來自**實測**，不是推論。參考實作
（`skill_server/`）在受限容器（0.1 core / 512 MB / 唯讀根檔案系統）中
逐條驗證過，實測數字直接寫進規則的理由欄位。

多數規則的存在是因為它的反面曾經真實發生過。這些案例記錄在
[附錄 A：事故紀錄](A-事故紀錄.md)，每一條都對應一條規範與一個回歸測試。

## 章節

| # | 文件 | 內容 |
|---|---|---|
| 1–2 | [核心](RFC-01-核心.md) | 術語、整體架構、生命週期 |
| 3–5 | [結構](RFC-02-結構.md) | 檔案系統、Manifest、Skill 規格 |
| 6–8 | [介面](RFC-03-介面.md) | Prompt、Tool、Resource |
| 9 | [型別系統](RFC-04-型別.md) | 型別定義與 JSON Schema 對應 |
| 10 | [驗證規則](RFC-05-驗證.md) | 每條規則的 ID、嚴重度、偵測邏輯 |
| 11 | [JSON Schema](schemas/) | Draft 2020-12，可直接使用 |
| 12 | [錯誤模型](RFC-06-錯誤.md) | 通用錯誤規格 |
| 13 | [安全](RFC-07-安全.md) | 威脅模型與可測試的要求 |
| 14 | [相容性](RFC-08-相容性.md) | 版本、棄用、遷移 |
| 15–16 | [文件與測試](RFC-09-文件測試.md) | 最低要求結構與涵蓋率 |
| 17 | [Lint 規範](RFC-10-lint.md) | 可執行的 lint 規則 |
| 18 | [CI 管線](RFC-11-ci.md) | 驗證階段與報告格式 |
| 19 | [審查清單](RFC-12-審查清單.md) | 每項對應一條驗證規則 |
| 20 | [參考實作](RFC-13-參考實作.md) | 逐項說明如何滿足規範 |
| A | [事故紀錄](A-事故紀錄.md) | 規則的實證來源 |

## 規範編號

| 前綴 | 用途 | 範例 |
|---|---|---|
| `RFC-nnn` | 規範性要求 | RFC-041 服務 MUST NOT 寫入任何檔案 |
| `VAL-nnn` | 驗證規則 | VAL-012 manifest.name 必須符合命名規則 |
| `LINT-nnn` | Lint 規則 | LINT-003 description 不得超過 1024 字元 |
| `ERR-nnn` | 錯誤碼 | ERR-404 skill 不存在 |
| `SEC-nnn` | 安全要求 | SEC-007 呼叫端不得設定 PATH |
| `PERF-nnn` | 效能要求 | PERF-002 目錄回應必須符合 context 預算 |

編號一經指派**永不重用**。棄用的編號標記為 `DEPRECATED` 並保留。

## 符合性等級

| 等級 | 要求 |
|---|---|
| **L1 基本** | 所有 `MUST` 規則 |
| **L2 標準** | L1 + 所有 `SHOULD` 規則 + 通過完整 lint |
| **L3 完整** | L2 + 效能規則 + 安全測試 + 相容性測試 |

宣稱符合的服務 MUST 在 README 標示等級，並附上驗證報告。

## 立即驗證

```bash
# 驗證單一 skill 是否符合規範
uv run python -m spec.validate skills/api-call

# 驗證整個 skill 目錄樹
uv run python -m spec.validate skills/ --recursive

# 輸出機器可讀的報告
uv run python -m spec.validate skills/ --format=json
```

## RFC 2119 規範語言

本文件中的 MUST、MUST NOT、REQUIRED、SHALL、SHALL NOT、SHOULD、
SHOULD NOT、MAY 依 RFC 2119 解釋。

**未標記規範關鍵字的敘述是說明性的，不具強制力。**
