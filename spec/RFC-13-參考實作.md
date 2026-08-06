# RFC-13：參考實作

`skills/api-call/` 是符合本規範所有條文的最小完整範例。本章逐項說明每個
設計決定滿足了哪條規範。

## 20.1 完整原始碼

### `skills/api-call/SKILL.md`

```markdown
---
name: api-call
description: 呼叫內部 API 並原樣回傳回應。送出型端點會回 uuid，查詢型會回資料——兩者都直接就是答案。這是呼叫內部 API 最快的方式。
version: 1.0.0
tags: [api, internal, http]
execution:
  default:
    timeout: 30
    description: bash + curl，啟動成本最低；打完 API 印出回應就結束
---
```

| 決定 | 規範 | 為什麼 |
|---|---|---|
| `name: api-call` | RFC-031 | kebab-case。同時是查詢鍵，也可能構成路徑 |
| description 說明「做什麼＋何時用」 | RFC-035 | 唯一每次對話載入的文字，是模型的路由依據 |
| description 為中文 | RFC-068 | 使用者以中文提問；只有 `name` 需為 ASCII |
| `version` 為 SemVer | LINT-001 | 使相容性檢查可自動化 |
| `execution` 只有 `timeout` 與 `description` | RFC-036 | **不宣告輸出語意**——Server 對「回 uuid」與「回資料」處理完全相同 |
| 未宣告 `mode` / `key_field` | VAL-024 | 見事故 A-40：自動擷取會把訂單 id 誤判為 handle |

### `skills/api-call/scripts/call.sh`

```bash
#!/bin/bash
# spec:allow LINT-020 需要自行判斷 curl 的離開碼以產生結構化錯誤，用 -e 會直接中止
set -uo pipefail

json_string() {
    printf '%s' "$1" | head -c 400 \
        | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' -e 's/\r//g' \
        | awk 'BEGIN{ORS=""; printf "\""} {print (NR>1 ? "\\n" : "") $0} END{printf "\""}'
}

METHOD="${1:?用法: call.sh <METHOD> <URL> [BODY]}"
URL="${2:?缺少 URL}"
BODY="${3:-}"
CONNECT_TIMEOUT="${HTTP_CONNECT_TIMEOUT:-5}"
MAX_TIME="${HTTP_TIMEOUT:-15}"

echo "[$(date +%H:%M:%S)] $METHOD $URL (max ${MAX_TIME}s)" >&2

args=(-sS --show-error
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME"
      -X "$METHOD" -H 'Accept: application/json'
      -w '\n%{http_code}')
[ -n "${API_TOKEN:-}" ] && args+=(-H "Authorization: Bearer $API_TOKEN")
[ -n "$BODY" ] && args+=(-H 'Content-Type: application/json' -d "$BODY")

response=$(curl "${args[@]}" "$URL" 2>&1) || curl_failed=1
# ... 錯誤處理與輸出
```

| 決定 | 規範 | 為什麼 |
|---|---|---|
| bash + curl 而非 Python | PERF-004 | 實測 **102 ms vs 2,198 ms**，21 倍差距（事故 A-13） |
| `--max-time` + `--connect-timeout` | SEC-012 / RFC-050 | curl 的讀取沒有上限 |
| 進度走 `>&2` | RFC-083 | stdout 是結果且會完整回傳，重複串流會灌爆 Client 日誌 |
| 呼叫**之前**印出端點 | RFC-050 | 卡住時最後一行就指出卡在哪 |
| 回應原樣印到 stdout | RFC-074 | Server 不解讀，Script 也不該重新包裝 |
| 印完立刻結束 | RFC-081 | Server 等的是**行程結束**，不是 stdout 關閉。留尾巴會拖住整個呼叫 |
| token 從 `$API_TOKEN` 取 | RFC-089 | argv 出現在行程列表與日誌 |
| 不寫任何檔案 | RFC-041 | 唯讀根檔案系統 |
| `json_string()` 一次性轉義 | RFC-105 / A-54 | 逐行加引號會讓多行錯誤變成無效 JSON |
| 明確豁免 LINT-020 並附理由 | RFC-055 | 需自行判斷 curl 離開碼；豁免可見且可審查 |

## 20.2 逐條符合性對照

| 章 | 規範 | 如何滿足 | 驗證 |
|---|---|---|---|
| 3 | RFC-021 | UTF-8 無 BOM | VAL-002 |
| 3 | RFC-022 | 唯一可執行檔在 `scripts/` | VAL-031 |
| 3 | RFC-023 | `.sh` 在白名單 | VAL-030 |
| 4 | RFC-031 | name 為 kebab-case | VAL-010 |
| 4 | RFC-034 | description 96 字元 | VAL-013 |
| 4 | RFC-036 | execution 無語意欄位 | VAL-024 |
| 5 | RFC-040c | Body 有「用法／回傳的就是答案／為什麼快」 | [人工] |
| 5 | RFC-041 | 不寫檔案 | VAL-040 + `docker diff` |
| 7 | RFC-074 | 回應原樣輸出 | 整合測試 |
| 7 | RFC-083 | 進度 stderr、結果 stdout | 整合測試 |
| 13 | RFC-050 | 所有 timeout 明確 | SEC-012 |
| 13 | RFC-089 | 機密走 env | [人工] |

## 20.3 實測數據

唯讀容器（`--read-only --cpus=0.1 --memory=512m`）對真實 API：

| 情境 | p50 |
|---|---|
| POST 送出型（回 uuid） | **201 ms** |
| GET 查詢型（回資料） | **194 ms** |
| 連不到（快速失敗） | **199 ms** |
| 同環境的 Python 等效 script | **2,198 ms** |

容器檔案變更數：**0**（RFC-041 的可驗證條件）。

## 20.4 從零建立一個合規 Skill

```bash
mkdir -p skills/my-skill/scripts

cat > skills/my-skill/SKILL.md <<'EOF'
---
name: my-skill
description: <做什麼>。當<什麼情況>時使用。
version: 0.1.0
execution:
  default:
    timeout: 30
---

# <標題>

## 用法
run_skill_script("my-skill", "scripts/run.sh", ["<參數>"])

## 解讀結果
- <欄位>：<意義>

## 不適用的情況
- <邊界>
EOF

cp skills/api-call/scripts/call.sh skills/my-skill/scripts/run.sh
# 修改端點與參數

uv run python -m spec.validate skills/my-skill --level=L2
```

**RFC-200** 新 Skill 在合併前 MUST 通過 `--level=L2`。
