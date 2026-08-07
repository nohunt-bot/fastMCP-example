# 附錄 A：事故紀錄

規範中多數規則的存在，是因為它的反面曾經真實發生。本附錄記錄這些案例，
每一筆都對應一條規範與一個迴歸測試。

**這份紀錄 MUST NOT 被刪減。** 移除事故紀錄會讓後人重蹈覆轍——規則看起來
像是任意的教條，而不是被證實過的約束。

## A.1 安全（攻擊測試中成功過）

| # | 事故 | 影響 | 規範 | 測試 |
|---|---|---|---|---|
| A-01 | `hooks/pre.py` 為指向外部的 symlink | **任意程式碼執行，且每次呼叫都執行**。scripts 走 jail 檢查，hooks 走另一條路徑，完全沒檢查 | RFC-027 | `test_hook_symlinked_outside_the_bundle_is_refused` |
| A-02 | 呼叫端可設 `env={"PATH": "/tmp/fake"}` | **RCE**。以假的 `date` 驗證，實測印出 PWNED。原檢查只驗變數名是否為英數，沒想到變數**本身**就是攻擊面 | RFC-052 | `test_caller_cannot_set_execution_changing_env` |
| A-03 | 呼叫端可設 `LD_PRELOAD` / `PYTHONPATH` | **RCE** | RFC-052 | 同上（參數化） |
| A-04 | skill `name` 為 `../../../tmp/escaped` | 狀態目錄逃出根目錄 | RFC-031 | `test_skill_name_follows_claude_code_rules` |
| A-05 | stdin 無上限 | 20 MB 全進記憶體 | RFC-054 | `test_stdin_is_bounded` |

**共同教訓**：防護的不對稱是最危險的。scripts 有 jail、hooks 沒有；變數名
有檢查、變數語意沒有。**新增執行路徑時，MUST 檢查它是否套用了既有路徑的
所有防護。**

## A.2 效能與資源

| # | 事故 | 影響 | 規範 |
|---|---|---|---|
| A-10 | `list_skills` 未套 context 預算 | 500 個 Skill = 23,467 tokens = 30 K 視窗的 **78%**。這是唯一沒套預算的工具，而它是 Progressive Disclosure 的第一層 | PERF-001 / RFC-066 |
| A-11 | `read_skill_file` / `load_skill` 未套預算 | 400 KB 參考檔 ≈ 114,285 tokens 直接回傳，**外觀是「讀取成功」而非錯誤** | RFC-025 / RFC-040a |
| A-12 | 以完整 CPU 數字推算 0.1 core | 推算冷啟動 3 秒，**實測 42 秒**（14 倍）。據此設定的探針延遲會造成 CrashLoopBackOff，且日誌無異常 | RFC-155 / RFC-167 |
| A-13 | bash 與 Python 差距被低估 | 推算 4~5 倍，**實測 21 倍**（102 ms vs 2,198 ms） | PERF-004 |

## A.3 執行契約

| # | 事故 | 影響 | 規範 | 測試 |
|---|---|---|---|---|
| A-20 | 逾時時丟棄 partial output | 只回傳空字串，無法判斷 Script 停在哪一步。緩衝區位於讀取協程內，取消即丟棄 | RFC-009 / RFC-010 | `test_output_survives_a_timeout` |
| A-21 | 無停滯偵測 | 卡住與很慢無法區分，必須等滿整個 timeout | RFC-080 | `test_stall_detection_kills_early` |
| A-22 | 孫行程繼承 stdout 使 pipe 不 EOF | `timeout=2` 的呼叫實際跑 **5.05 秒**——違反對呼叫端的承諾。收尾等待為固定 5 秒而非剩餘預算 | RFC-082 | `test_timeout_holds_when_a_grandchild_keeps_the_pipe_open` |
| A-23 | `_in_flight` 非 cancel-safe | uvicorn 關機時取消請求，計數器永久 +1，此後每次關機都等滿逾時 | RFC-014 | — |
| A-24 | 輸出達上限後停止讀取 | 子行程阻塞在滿的管線上，形成死鎖 | RFC-054a | `test_output_is_capped_without_deadlocking` |

## A.4 預設值

| # | 事故 | 影響 | 規範 |
|---|---|---|---|
| A-30 | 預設建立狀態目錄 | 唯讀容器中**每一支 Script 都失敗**。服務本身起得來、`/ready` 回 200，但所有執行都爆 | RFC-041 |
| A-31 | `--context-tokens` 預設為雲端模型大小 | 小 context 模型每次呼叫都逾時，且**症狀是逾時而非報錯** | RFC-131 |
| A-32 | k8s probe 指向 `/mcp` | `GET /mcp` 回 405/406，pod **永遠 NotReady** | RFC-008 |

**共同教訓**：預設值即契約。以上三者都不需要使用者改任何設定就會發生。
**RFC-131 因此將預設值變更列為 major。**

## A.5 設計過度

| # | 事故 | 影響 | 規範 |
|---|---|---|---|
| A-40 | 以 `execution.mode` 宣告輸出語意 | 旗標不改變任何行為，只讓 Server 猜測它已持有的東西。自動擷取「任務代號」把訂單的 `{"id": 123}`、使用者的 `{"id": "u-88"}` 誤判為 handle | RFC-036 / RFC-074 |

**教訓**：錯誤的 handle 比沒有 handle 更糟，因為它看起來是對的。
**Server MUST NOT 解讀輸出語意。**

## A.6 工具鏈

| # | 事故 | 影響 | 規範 |
|---|---|---|---|
| A-50 | CLI 參數定義遺漏 | 服務**完全無法啟動**，但全部單元測試通過——它們都直接呼叫內部建構函式，繞過 CLI | RFC-162 |
| A-51 | `.dockerignore` 排除了 `README.md` | `pyproject.toml` 宣告 `readme`，hatchling 建置失敗 | RFC-162 |
| A-52 | Linter 對**註解內容**做模式比對 | 三條規則誤報，其中一條的目標註解正好在說明「為什麼不該這樣寫」 | RFC-176 |
| A-53 | Linter 只比對同一行 | `--max-time` 放在陣列變數中，被誤判為缺少 | RFC-177 |
| A-54 | Shell 的 JSON 轉義逐行加引號 | 多行錯誤訊息變成**無效 JSON** | RFC-105 |
| A-55 | `pipefail` 搭配 `\| head` | 上游收到 SIGPIPE，離開碼 141，但輸出正確 | LINT-021 |
| A-56 | 驗證器容許豁免不可豁免的安全規則 | RFC-057 明訂 `error` 等級安全規則不可豁免，但 `flag()` 對所有規則一視同仁地接受 `# spec:allow`。一支 `requests.get()` 毫無 timeout 的 script 加一行註解即印出「**通過 L2**」。**條文是對的，執行條文的程式不是**，而通過的是驗證器、不是條文，所以不會有人發現 | RFC-057 |
| A-57 | 大量 stdin 送給不讀 stdin 的 script | `drain()` 在期限迴圈**開始之前**就阻塞：`timeout=2` 實測跑滿 **60.02 秒**（30 倍），且以未捕捉的 `BrokenPipeError` 收場，連已擷取的 partial output 一併丟棄。多數 script 本來就不讀 stdin | RFC-009 / RFC-082 |

## A.7 關機

| # | 事故 | 影響 | 規範 |
|---|---|---|---|
| A-60 | 假設 graceful shutdown 能保護進行中的請求 | 實測：grace=25 秒、Script 只需 8 秒，客戶端**仍在 6.1 秒斷線**。uvicorn 延長行程壽命，但 streamable-http 連線在關機開始即中斷 | RFC-013 / RFC-042 |

**教訓**：無法在 MCP 層提供的保證，MUST 明確標示為能力邊界，並指出正確的
解決位置（此處為後端 API 的 idempotency）。

## A.8 統計

| 類別 | 事故數 | 全部有迴歸測試 |
|---|---|---|
| 安全 | 5 | ✓ |
| 效能 | 4 | ✓ |
| 執行契約 | 5 | ✓ |
| 預設值 | 3 | ✓ |
| 設計過度 | 1 | ✓ |
| 工具鏈 | 6 | 部分 |
| 關機 | 1 | ✓（記錄為限制） |
| **合計** | **25** | — |

**RFC-190** 新發現的事故 MUST 增列於本附錄，MUST 對應至少一條規範與
一個迴歸測試。
