# RFC-04：型別系統

所有跨越 Server／Client 邊界的資料 MUST 可用 JSON Schema Draft 2020-12
表達。本章定義型別到 JSON Schema 的對應。

## 9.1 基本型別

| 型別 | JSON Schema | 約束 |
|---|---|---|
| `string` | `{"type":"string"}` | MUST 宣告 `maxLength` |
| `integer` | `{"type":"integer"}` | MUST 宣告 `minimum`／`maximum` |
| `number` | `{"type":"number"}` | 同上 |
| `boolean` | `{"type":"boolean"}` | — |
| `null` | `{"type":"null"}` | 僅用於聯集 |

**RFC-110** 所有字串型別 MUST 宣告 `maxLength`。

**RFC-111** 所有數值型別 MUST 宣告上下界。

**理由**：無界的型別是 DoS 面，且使呼叫端無法預先驗證。

## 9.2 選用與可空

本規範**區分**兩者：

| 語意 | 表達 | 意義 |
|---|---|---|
| 選用 | 不在 `required` | 屬性可以不存在 |
| 可空 | `{"type":["string","null"]}` | 屬性存在但值為 null |
| 兩者皆可 | 不在 `required` + 型別含 `null` | 常見於工具參數 |

**RFC-112** 工具的選用參數 MUST 同時允許「缺席」與 `null`。

**理由**：不同 MCP Client 對未提供的參數處理不一致，有的省略、有的送
`null`。兩者都必須被接受。

## 9.3 複合型別

### 陣列

```json
{ "type": "array", "items": {...}, "maxItems": 64, "uniqueItems": true }
```

**RFC-113** 所有陣列 MUST 宣告 `maxItems`。

### 對映

```json
{ "type": "object", "additionalProperties": { "type": "string" },
  "propertyNames": { "pattern": "^[A-Za-z_][A-Za-z0-9_]*$" } }
```

**RFC-114** 開放式對映 MUST 以 `propertyNames` 約束鍵的格式。

### 列舉

**RFC-115** 列舉 MUST 使用 `enum`，MUST NOT 使用自由字串加文件說明。

**RFC-116** 新增列舉值為**向後相容**；移除或重新命名為 **breaking**。

## 9.4 判別聯集

**RFC-117** 判別聯集 MUST 使用 `oneOf` 搭配 `if/then` 的判別欄位，
MUST NOT 依賴結構推斷。

範例（`script-result` 的診斷要求）：

```json
{
  "if":   { "properties": { "status": { "enum": ["timeout", "stalled"] } } },
  "then": { "required": ["hint", "silent_for_s"] }
}
```

## 9.5 遞迴型別

**RFC-118** 遞迴型別 MUST 宣告深度上限。

**理由**：無界遞迴在序列化與驗證兩端都是 DoS 面。參考實作的輸出結構
描述（`_outline`）在深度 3 截斷。

## 9.6 引用與重用

**RFC-119** 共用定義 MUST 置於 `$defs` 並以 `$ref` 引用，MUST NOT 重複
定義。

**RFC-120** 跨檔案引用 MUST 使用相對 `$id`，MUST NOT 依賴網路取得
schema。

**理由**：CI 與離線環境必須能完成驗證。

## 9.7 型別與 Schema 的對應總表

| 概念 | Schema 位置 |
|---|---|
| Skill 名稱 | `common#/$defs/skillName` |
| 描述 | `common#/$defs/description` |
| 版本 | `common#/$defs/semver` |
| Script 路徑 | `common#/$defs/scriptPath` |
| 相對路徑 | `common#/$defs/relativePath` |
| 逾時 | `common#/$defs/timeoutSeconds` |
| 執行狀態 | `common#/$defs/scriptStatus` |
| Manifest | `manifest.schema.json` |
| Script 結果 | `script-result.schema.json` |
| 錯誤 | `error.schema.json` |
| 驗證報告 | `validation-report.schema.json` |
