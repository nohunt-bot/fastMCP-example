# 04 Hooks 指南

在 script 執行前後插入檢查。hook 就是一般的程式，用 JSON 透過 stdin/stdout
溝通，跑在跟 script 相同的沙箱裡——**hook 不會比它守護的 script 有更多權限**。

## 放在哪裡

```
skills/我的技能/
├── hooks/
│   ├── pre.py      ← 執行前
│   └── post.py     ← 執行後
└── scripts/
```

檔名固定是 `pre.py` / `post.py`。放進去就生效，不用註冊、不用重啟。

全域 hook（套用到**每一個** skill）：

```bash
uv run skill-mcp --hooks-dir /srv/skill-hooks
```

## pre-hook

**收到**（stdin）：

```json
{
  "skill": "internal-api",
  "script": "scripts/submit.py",
  "args": ["--payload", "{...}"],
  "mode": "background",
  "caller": "<客戶端 id>",
  "skill_dir": "/srv/skills/internal-api"
}
```

**回傳**：

| 離開碼 | stdout | 結果 |
|---|---|---|
| 0 | 空 | 放行 |
| 0 | `{"env": {"K": "V"}}` | 放行，並注入環境變數 |
| 0 | `{"args": [...]}` | 放行，並改寫參數 |
| 0 | `{"note": "..."}` | 放行，附註記（會出現在結果的 `hook_notes`） |
| **非 0** | `{"reason": "..."}` | **拒絕，script 完全不執行** |

拒絕時 `reason`（或 stderr）會傳回給呼叫端。

### 範例：擋掉過大的 payload 並注入追蹤 id

```python
#!/usr/bin/env python3
import json, sys, uuid

req = json.load(sys.stdin)
args = req.get("args", [])

# 在這裡擋掉，而不是讓 API 稍後才拒絕
for i, arg in enumerate(args):
    if args[i - 1] == "--payload" and len(arg) > 4096:
        print(json.dumps({"reason": f"payload 有 {len(arg)} bytes，上限 4096"}))
        sys.exit(1)

out = {"note": f"已檢查 {req['script']}"}
if req.get("mode") == "background":
    out["env"] = {"REQUEST_ID": str(uuid.uuid4())}
print(json.dumps(out))
```

## post-hook

**收到**：跟 pre 一樣，外加 `"result"`（script 的完整執行結果）。

**回傳**：

| 離開碼 | stdout | 結果 |
|---|---|---|
| 0 | 空 | 原樣通過 |
| 0 | `{"result": {...}}` | 用這個取代結果 |
| **非 0** | `{"reason": "..."}` | **整個呼叫失敗** |

### 範例：稽核記錄 + 驗證

```python
#!/usr/bin/env python3
import json, os, pathlib, sys, time

req = json.load(sys.stdin)
result = req.get("result", {})

# 送出成功卻沒有 key，等於任務失聯——視為失敗
if req.get("mode") == "background" and result.get("status") == "ok" and not result.get("key"):
    print(json.dumps({"reason": "背景任務沒有回傳 job key"}))
    sys.exit(1)

if state := os.getenv("SKILL_STATE_DIR"):
    line = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": req.get("script"),
            "mode": req.get("mode"), "status": result.get("status"), "key": result.get("key")}
    path = pathlib.Path(state) / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(line) + "\n")

result["audited"] = True
print(json.dumps({"result": result}))
```

## 執行順序

```
pre：   全域 → skill        （組織政策先擋，成本最低）
script
post：  skill → 全域        （組織稽核看到最終送出的東西）
```

全域 hook 的 pre 先跑，所以組織層級的拒絕最早發生也最便宜。post 則讓全域最後
看過，單一 skill 無法繞過稽核。

## 成本（實測，不是估算）

| | p50 | 子行程數 |
|---|---|---|
| script，無 hook | 12.9 ms | 1 |
| script + pre + post | 41.8 ms | 3 |

約 **3.2 倍**。這是強制檢查的真實代價。

但**只有宣告 hook 的 skill 要付**：

- 沒有 `hooks/` 目錄的 skill 走原本的路徑，完全沒有額外成本
- `list_skills`、`load_skill` **完全不經過** hook 路徑（仍然 1,149 rps）

所以請把 hook 用在真正需要強制檢查的 skill 上，不要無差別套用。全域 hook 尤其
要謹慎——它會讓**每一次** script 執行都變慢。

## 限制

- hook 自己的 timeout 固定 10 秒。hook 是檢查，不是工作。
- hook 只有 `.py` / `.sh` / `.js`（跟 script 同樣的白名單）。
- hook 不能執行 skill 目錄以外的東西，跟 script 同樣的路徑限制。
- pre、script、post 是**依序**執行，不會巢狀（巢狀會在並行號誌上死鎖）。

## 適合與不適合

**適合：**

- 參數驗證（大小、格式、範圍）
- 注入追蹤 id、request id
- 稽核記錄
- 輸出敏感資訊攔截
- 組織層級的黑名單

**不適合：**

- 需要超過 10 秒的檢查
- 呼叫外部服務做驗證（會把延遲疊上去）
- 高頻率、對延遲敏感的 skill
- 純粹的記錄（改用 `--log-level` 或中介層更便宜）

## 驗證

```bash
uv run python acceptance.py --group Hooks
```

五項：拒絕時 script 確實沒執行、環境變數注入、參數改寫、post 改寫與拒絕、
全域 hook 生效、沒 hook 的 skill 不受影響。
