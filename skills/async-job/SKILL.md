---
name: async-job
description: Fire off a long-running API job that returns a key immediately and writes its result to a database, then collect that result later by key. Use for work that must not occupy the model while it runs.
version: 2.0.0
tags: [http, api, async, jobs]
---

# Fire-and-forget API jobs

For an endpoint that accepts work, returns `{"key": "<uuid>"}` straight away,
runs to completion on its own, and writes the result to a database. The job is
*meant* to outlive the call — the point is that no model tokens are spent while
it runs.

## The two calls you actually need

```
# 1. fire it off — returns in milliseconds, never waits
run_skill_script("async-job", "scripts/job.py",
                 ["submit", "http://internal/api/jobs",
                  "--body", "{\"report\":\"q3\"}", "--label", "q3 report"])

# 2. later, possibly in a different session — collect by key
run_skill_script("async-job", "scripts/job.py",
                 ["fetch", "http://internal/api/results", "--key", "<uuid>"])
```

`submit` returns and the job keeps running. That is correct behaviour, not a
race to be closed.

## 這裡沒有任何暫存

服務不寫檔案，所以 **uuid 只存在於 submit 的回應裡**，由呼叫端負責保管。

要讓「回應遺失」不痛，正確的位置是你的 API：送出時帶一個由呼叫端產生的
idempotency key，客戶端重送同一個 key 時回同一個 uuid，而不是建立第二個任務。

`--label` 只是把說明回顯在結果裡，方便閱讀，沒有被保存在任何地方。

## Deliberately not here

- **No waiting after submit.** That would put the long task back inside the
  model loop, which is exactly what this design avoids.
- **No cancellation.** Cancelling a partially-complete job is only safe if your
  backend says it is.
- **`await` 是最後手段。** 有界、會印心跳，只給「真的要同一輪拿到結果」的場合。
  這個架構下優先用 `submit` + `fetch`。
- **沒有 `list`。** 服務不記得你送出過什麼。

## Field names

Job key: looked up under `key,id,job_id,task_id,uuid,request_id`, one level
deep. Override with `--key-field`. State (for `fetch`): `status,state,
job_status,phase`. A response with no state field is treated as ready.

`--timeout` bounds **one HTTP request**, not the job. 10 s is plenty for a
submit that returns immediately.

See `references/patterns.md` for the retrieval workflow.
