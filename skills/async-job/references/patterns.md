# Working with fire-and-forget jobs

## The workflow

1. `submit` → **把回應裡的 uuid 交給呼叫端**，服務不會替你記住。
2. 讓它去跑。任務自己會完成，不要輪詢。
3. 之後用 `fetch --key <uuid>` 取結果。

第 3 步可以在完全不同的 session 進行——前提是 uuid 有被保管好。

## uuid 沒有任何地方會保存

服務不寫檔案。uuid 只存在於 `submit` 的回應裡，回應遺失就沒有第二份。

實務上會遺失回應的情況：滾動更新、連線中斷、pod 被驅逐。要讓這些都不痛，
唯一可靠的做法是**你的 API 支援 idempotency key**：

- 送出時帶一個由呼叫端產生的 key
- 客戶端沒收到回應時重送同一個 key
- API 回同一個 uuid，而不是建立第二個任務

只有你的 API 知道什麼算「同一個任務」，這件事沒辦法在 MCP 這層補償。

## Fetching

`fetch` reports `ready`:

- `ready: true` — the result is in `result`.
- `ready: false` — the backend still has it in a non-terminal state. Fetch again
  later; do not loop.

If `fetch` 404s, the key was never valid or the backend expired it. Check `list`
for a typo before re-submitting: re-submitting duplicates real work.

## When submit itself is slow

`submit` should return in milliseconds. If it does not, the endpoint is doing
work synchronously before handing back the key, and the async boundary is in the
wrong place — that is a backend fix, not something to paper over with a longer
timeout.


