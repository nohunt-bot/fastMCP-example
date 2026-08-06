#!/bin/bash
# 呼叫內部 API 並原樣輸出回應。
#
# 設計重點：
#  1. 不寫任何檔案 —— 可跑在唯讀根檔案系統上
#  2. 印完立刻結束 —— 服務等的是行程結束，留尾巴會拖慢整個呼叫
#  3. 每個網路呼叫都有 timeout —— curl 預設讀取沒有上限
# spec:allow LINT-020 需要自行判斷 curl 的離開碼以產生結構化錯誤，用 -e 會直接中止
set -uo pipefail

METHOD="${1:?用法: call.sh <METHOD> <URL> [BODY]}"
URL="${2:?缺少 URL}"
BODY="${3:-}"

# 把任意文字轉成一個合法的 JSON 字串值。
# 順序很重要：先轉義反斜線再轉義引號，最後把換行折成字面的 \n
# ——逐行加引號會讓多行輸出變成無效 JSON。
json_string() {
    printf '%s' "$1" | head -c 400 \
        | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' -e 's/\r//g' \
        | awk 'BEGIN{ORS=""; printf "\""} {print (NR>1 ? "\\n" : "") $0} END{printf "\""}'
}

CONNECT_TIMEOUT="${HTTP_CONNECT_TIMEOUT:-5}"
MAX_TIME="${HTTP_TIMEOUT:-15}"

# 進度走 stderr：卡住時最後一行會指出卡在哪個端點，
# 同時也是心跳，避免被停滯偵測誤判。
echo "[$(date +%H:%M:%S)] $METHOD $URL (max ${MAX_TIME}s)" >&2

args=(-sS --show-error
      --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME"
      -X "$METHOD" -H 'Accept: application/json'
      -w '\n%{http_code}')

[ -n "${API_TOKEN:-}" ] && args+=(-H "Authorization: Bearer $API_TOKEN")
[ -n "$BODY" ] && args+=(-H 'Content-Type: application/json' -d "$BODY")

# stderr 併進來，這樣連線失敗的原因才拿得到（curl 的錯誤訊息走 stderr）
response=$(curl "${args[@]}" "$URL" 2>&1) || curl_failed=1

if [ "${curl_failed:-0}" = "1" ]; then
    echo "[$(date +%H:%M:%S)] 連線失敗" >&2
    printf '{"ok":false,"error":"unreachable","detail":%s}\n' "$(json_string "$response")"
    exit 1
fi

status="${response##*$'\n'}"          # 最後一行是 HTTP 狀態碼
payload="${response%$'\n'*}"          # 其餘是 body

echo "[$(date +%H:%M:%S)] HTTP $status" >&2

if [ "$status" -ge 200 ] && [ "$status" -lt 300 ]; then
    printf '%s\n' "$payload"          # 回應原樣輸出，服務不解讀
    exit 0
fi

printf '{"ok":false,"http_status":%s,"body":%s}\n' "$status" "$(json_string "$payload")"
exit 1
