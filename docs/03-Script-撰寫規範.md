# 03 Script 撰寫規範

script 放在 `scripts/`，這是**唯一**可執行的目錄。支援 `.py`、`.sh`，
裝了 node 就多支援 `.js`。有執行位元不代表可以跑，白名單才算數。

## 五條鐵則

這五條是從實際踩過的坑整理出來的，每一條都有對應的自動測試。

### 1. 每個網路呼叫都要有明確 timeout

**這是最常見的卡住原因，沒有之一。**

| 套件 | 預設 timeout |
|---|---|
| `requests` | **沒有——會永遠等下去** |
| `urllib.request.urlopen` | 作業系統預設，約 2 分鐘 |
| `httpx` | 5 秒 |
| `curl` | 讀取沒有上限 |

```python
# 錯
urllib.request.urlopen(url)
requests.get(url)

# 對
urllib.request.urlopen(url, timeout=10)
requests.get(url, timeout=(5, 10))   # (連線, 讀取)
```

### 2. 在阻塞「之前」印，不是之後

```python
# 錯 —— 卡住時你什麼都看不到
resp = call_api(url)
print(f"取得 {url}")

# 對 —— 卡住時最後一行就告訴你卡在哪個端點
print(f"GET {url}", file=sys.stderr, flush=True)
resp = call_api(url)
```

服務在殺掉 script 時**會保留已經印出的內容**，所以逾時報告會長這樣：

```
status:     stalled
stdout:     步驟 1: 認證完成
            步驟 2: GET /v1/orders     ← 死在這裡
silent_for: 3.0 秒
```

沒有這個習慣，你只會得到「逾時了」三個字。

### 3. 長時間等待要有心跳

服務會殺掉**靜默超過 `stall_timeout`（預設 20 秒）**的 script，因為卡住的
script 和很慢的 script 在沒有輸出時完全無法區分。

```python
import threading, time, sys

stop = threading.Event()
def heartbeat():
    n = 0
    while not stop.wait(5):
        n += 5
        print(f"仍在等待 {url}（已 {n} 秒）", file=sys.stderr, flush=True)

threading.Thread(target=heartbeat, daemon=True).start()
try:
    result = slow_call()
finally:
    stop.set()
```

**心跳間隔要小於 `stall_timeout`。** 預設是 20 秒，所以每 5 秒印一次很安全。

輪詢迴圈同理——`time.sleep()` 迴圈不印東西的話，20 秒就會被砍掉，就算你把
`timeout` 設成 300 也一樣。

### 4. 進度走 stderr，結果走 stdout

```python
print("進度訊息", file=sys.stderr, flush=True)     # 進度
json.dump(result, sys.stdout)                      # 結果
```

三個理由：

- stdout 保持是乾淨的 JSON，好解析
- 進度會即時串流給呼叫端；stdout 不會（因為結果本來就會完整回傳，重複串流會
  把客戶端日誌灌爆）
- script 被砍時，部分擷取的 stdout 仍然可能是可解析的

### 5. 在來源就過濾，不要事後縮減

服務會把過大的輸出壓縮到 context 預算內，但**它只能猜哪些資料重要，你的查詢
不用猜**。

```python
# 服務會把 500 筆壓成 3 筆 + 說明（59,222 → 442 tokens）
# 但模型可能正好需要被丟掉的那幾筆

# 更好：讓 script 支援過濾
parser.add_argument("--limit", type=int, default=0)
parser.add_argument("--fields", default="")
parser.add_argument("--count-only", action="store_true")
```

實測差異：

| 取法 | tokens |
|---|---|
| 全抓，服務自動縮減 | 442 |
| `--count-only` | **43** |
| `--limit 5 --fields id,status,amount` | **134** |

script 可以讀 `SKILL_OUTPUT_BUDGET_BYTES` 環境變數，超過時在 stderr 提醒：

```python
budget = int(os.getenv("SKILL_OUTPUT_BUDGET_BYTES", "0"))
if budget and len(text.encode()) > budget:
    print(f"警告：輸出 {len(text.encode()):,} bytes 超過預算 {budget:,}，"
          f"建議加 --limit 或 --fields", file=sys.stderr, flush=True)
```

## 服務提供的環境變數

| 變數 | 內容 |
|---|---|
| `SKILL_NAME` | 目前 skill 的名稱 |
| `SKILL_DIR` | skill 目錄的絕對路徑 |
| `SKILL_OUTPUT_BUDGET_BYTES` | 建議的輸出上限 |
| `HTTP_PROXY` 等 | 公司 proxy 與 TLS 設定（自動轉發） |

工作目錄（cwd）是 skill 目錄，所以相對路徑會指到自己的 bundle。

**不會**繼承伺服器環境裡的其他變數（例如各種金鑰）。要傳機密請用工具的 `env`
參數：

```python
run_skill_script("skill", "scripts/x.py", ["--token-env", "API_TOKEN"],
                 env={"API_TOKEN": "..."})
```

然後在 script 裡讀 `os.getenv("API_TOKEN")`。**不要放 `args`**——argv 會出現在
行程列表與日誌裡。

## 背景任務的 script

送出型的 script 只有一個責任：**拿到 uuid 就印出來，然後
結束**。

```python
print(f"POST {url}", file=sys.stderr, flush=True)
resp = post(url, payload, timeout=10)
# 第一件事就是印出 key。之後就算出事，uuid 也已經保住了
json.dump({"key": resp["key"], "status": "accepted"}, sys.stdout)
```

**先印 key，再做其他事。** script 被砍掉時 uuid 若沒印出來，任務會跑完但沒有人
能取回結果。

### script 不能寫任何檔案

服務跑在唯讀根檔案系統上，也不提供任何可寫目錄。這是刻意的：pod 掉了就掉了，
沒有 volume 要準備，副本之間沒有狀態要同步。

所以 **uuid 沒有地方可以暫存**。它只存在於這次呼叫的回應裡，由呼叫端負責保管。

要讓「回應遺失」不痛，正確的位置是**你的 API**：

- 送出時帶一個由呼叫端產生的 idempotency key
- 客戶端沒收到回應時重送同一個 key，API 回同一個 uuid 而不是建立第二個任務

只有你的 API 知道什麼算「同一個任務」，這件事沒辦法在 MCP 這一層補償。

需要暫存檔時用 `$TMPDIR`（容器裡要另外掛 `emptyDir` 的 tmpfs），
並且自己清乾淨——但先想清楚是不是真的需要。

## 「在背景跑」到底是誰在背景跑

這是最容易混淆的地方，先分清楚兩件事：

### 情況 A：API server 端在背景跑（你要的通常是這個）

```
script POST → API handler 把工作丟進佇列，立刻 return uuid → script 收到就結束
                                       ↑ 背景是在這裡發生的
```

script 完全是同步的，它只是「很快就拿到回應」。**這不需要服務端做任何事**，
也是最單純的做法。

**關鍵是 handler 什麼時候 return，不是 return 什麼。** 實測兩個 handler，
兩個都沒有回傳有意義的資料：

| handler 寫法 | script 耗時 |
|---|---|
| 把工作丟進佇列後立刻 return | **0.04 秒** |
| 宣告成 void，但同步把工作做完才 return | **5.07 秒** |

HTTP 沒有「void」這回事——就算你的 handler 宣告成 `void`，HTTP 還是會回一個
200 加空 body，script 還是會等到那一刻。所以：

```csharp
// 這不會在背景跑，script 會等到 LongWork() 做完
[HttpPost] public void Run() { LongWork(); }

// 這才會，script 幾十毫秒就拿到 uuid
[HttpPost] public IActionResult Run() {
    _queue.Enqueue(job);
    return Accepted(new { key });
}
```

### 情況 B：script 自己開背景行程（很少需要）

如果真的要，**一定要把孫行程的輸出導開**：

```python
subprocess.Popen([sys.executable, "worker.py"],
                 stdout=subprocess.DEVNULL,      # ← 必須
                 stderr=subprocess.DEVNULL)
print("已派工")                                   # script 立刻結束
```

不導開的話，孫行程會繼承 script 的 stdout pipe，pipe 一直不會 EOF，runner 會
被卡住直到孫行程結束——你的 script 明明 0.05 秒就 return 了，這個呼叫卻要等
到孫行程跑完。

導開之後：script **0.04 秒返回**，孫行程繼續跑到完成。兩項都有測試釘住。

## Shell script 的坑

`skills/repo-digest/scripts/digest.sh` 是實際踩過的例子：

```bash
set -euo pipefail

# 錯：head 提早關閉 pipe，上游收到 SIGPIPE，整條 pipeline 離開碼 141
git log ... | head -40

# 對：由產生端限制，或用 awk（awk 會讀到 EOF）
git log ... -n 40
... | awk 'NR<=20 {print}'

# grep 沒配到任何行會回傳 1，在 pipefail 下會讓整個 script 掛掉
... | { grep -v '^$' || true; } | ...
```

## 非零離開碼不是錯誤

script 回傳非零離開碼時，服務會**回報**而不是拋例外：

```json
{"status": "failed", "exit_code": 2, "stderr": "not a git repository: /x"}
```

所以把有意義的失敗寫進 stderr 並用非零離開碼結束，模型看得到也處理得了。

## 撰寫檢查清單

- [ ] 每個網路呼叫都有 `timeout=`
- [ ] 呼叫前先印出目標端點（stderr）
- [ ] 長等待有心跳，間隔小於 `stall_timeout`
- [ ] 進度 → stderr，結果 → stdout
- [ ] 支援 `--limit` / `--fields` 之類的過濾參數
- [ ] 機密從 `os.getenv` 讀，不從 `sys.argv`
- [ ] 送出型：第一件事就是印出 uuid
- [ ] 重試預算 `timeout × (retries+1) + backoff × retries` 小於工具的 `timeout`

## 可以直接抄的範本

| 情境 | 檔案 |
|---|---|
| 一般 API 呼叫（timeout/重試/退避） | `skills/api-fetch/scripts/fetch.py` |
| REST 查詢並在來源過濾 | `skills/rest-client/scripts/call.py` |
| 呼叫內部 API（**最快，bash**） | `skills/api-call/scripts/call.sh` |
| 背景任務送出／取回 | `skills/async-job/scripts/job.py` |
| 兩種模式並存 | `skills/internal-api/scripts/` |
| 純資料處理（無網路） | `skills/csv-profile/scripts/profile.py` |
| shell script | `skills/repo-digest/scripts/digest.sh` |
