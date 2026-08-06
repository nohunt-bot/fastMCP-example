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

# 呼叫內部 API

用 bash + curl，**不是 Python**。在資源受限的環境下實測：

| | p50 |
|---|---|
| 這支 bash script | **85 ms** |
| 等效的 Python script | **2,198 ms** |

差 26 倍，因為 Python 直譯器啟動在 0.1 core 上會被 CFS 切成幾十個週期。

## 用法

```
run_skill_script("api-call", "scripts/call.sh",
                 ["POST", "http://internal/api/jobs", "{\"report\":\"q3\"}"])
```

參數依序是：方法、URL、body（GET 時省略）。

```
run_skill_script("api-call", "scripts/call.sh",
                 ["GET", "http://internal/api/orders/SO-001"])
```

要帶 token 時透過 `env`（不要放 `args`，argv 會出現在行程列表）：

```
run_skill_script("api-call", "scripts/call.sh", ["GET", "http://internal/api/me"],
                 env={"API_TOKEN": "..."})
```

## 回傳的就是答案

script 把 API 的回應原樣印到 stdout，服務不解讀：

- 端點回 `{"key": "<uuid>"}` → 你拿到 uuid。**把它交給呼叫端然後停手**，
  工作在 API 那邊自己跑完。
- 端點回實際資料 → 你拿到資料。

失敗時 stdout 是 `{"ok": false, "http_status": …, "error": …}`，離開碼非零。

## 這支 script 為什麼快

- **bash + curl**，沒有直譯器啟動成本
- `--max-time` / `--connect-timeout` 都有設，不會無限等
- **印完立刻結束**——服務等的是行程結束，不是 stdout 關閉，所以
  script 不能在印完後還留著做別的事
- 不寫任何檔案，唯讀檔案系統可直接跑
