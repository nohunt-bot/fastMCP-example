# RFC-07：安全

## 13.1 威脅模型

### 信任邊界

```mermaid
graph LR
    subgraph U["不信任"]
        DOC[外部文件<br/>API 回應<br/>使用者輸入]
    end
    subgraph S["半信任"]
        LLM[LLM Client]
    end
    subgraph T["信任"]
        BUNDLE[Skill Bundle<br/>由作者提交並經審查]
        SRV[Server]
    end

    DOC -.可說服.-> LLM
    LLM -->|工具呼叫| SRV
    SRV --> BUNDLE

    style U fill:#4a1a1a,color:#fff
    style S fill:#3d3d00,color:#fff
    style T fill:#2d5016,color:#fff
```

**核心假設**：Client 是 LLM，**會被它讀到的任何文件說服**。因此任何由
Client 控制、能改變「執行什麼」的輸入都是攻擊面。

### 威脅清單

| ID | 威脅 | 影響 | 緩解 | 測試 |
|---|---|---|---|---|
| T-01 | 路徑穿越讀取任意檔案 | 洩漏 | RFC-028 | SEC-021 |
| T-02 | 符號連結逃逸 | 洩漏／RCE | RFC-028 | SEC-021 |
| T-03 | **Hook symlink 逃逸** | **RCE，每次呼叫都執行** | RFC-027 | SEC-024 |
| T-04 | **呼叫端設定 PATH** | **RCE** | RFC-052 | SEC-020 |
| T-05 | **呼叫端設定 LD_PRELOAD** | **RCE** | RFC-052 | SEC-020 |
| T-06 | 執行 scripts/ 外的檔案 | RCE | RFC-022 | 單元測試 |
| T-07 | 伺服器機密洩漏給 Script | 憑證外洩 | RFC-053 | SEC-022 |
| T-08 | 機密出現在 argv | 日誌外洩 | RFC-089 | 審查 |
| T-09 | stdin 耗盡記憶體 | DoS | RFC-054 | SEC-023 |
| T-10 | 輸出耗盡記憶體 | DoS | RFC-054 | 單元測試 |
| T-11 | 子行程管線死鎖 | DoS | RFC-054 | 單元測試 |
| T-12 | 間接 prompt injection | 誤導模型 | RFC-057a | 審查 |
| T-13 | Skill 名稱路徑穿越 | 越權寫入 | RFC-031 | 參數化測試 |

> 粗體者為**實際攻擊測試中成功過**的項目。

## 13.2 可測試的要求

**RFC-050** 每個網路呼叫 MUST 設定明確的 timeout。

| 客戶端 | 預設 | 要求 |
|---|---|---|
| `requests` | **無——永遠等待** | MUST 傳 `timeout=` |
| `urllib.urlopen` | OS 層級（分鐘級） | MUST 傳 `timeout=` |
| `curl` | 讀取無上限 | MUST 傳 `--max-time` |

**RFC-051** Shell script MUST 設定 `set -e`（或以 RFC-055 豁免並附理由）。

**RFC-052** Server MUST 拒絕呼叫端設定下列環境變數：

```
PATH SHELL IFS BASH_ENV ENV CDPATH GLOBIGNORE
PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONEXECUTABLE
NODE_OPTIONS NODE_PATH PERL5LIB PERL5OPT RUBYOPT RUBYLIB
SKILL_NAME SKILL_DIR SKILL_OUTPUT_BUDGET_BYTES
前綴：LD_ DYLD_ _RLD
```

**判準**：變數若改變**執行什麼**（而非**如何執行**），MUST 被拒絕。
傳遞資料的變數（token、base URL）MUST 保持可用。

**RFC-053** 子行程環境 MUST 以允許清單建構，MUST NOT 繼承 `os.environ`。

允許清單：

| 群組 | 變數 | 條件 |
|---|---|---|
| 基本 | `PATH HOME LANG LC_ALL TMPDIR TZ` | 一律 |
| 網路 | `HTTP(S)_PROXY NO_PROXY ALL_PROXY SSL_CERT_* REQUESTS_CA_BUNDLE CURL_CA_BUNDLE NODE_EXTRA_CA_CERTS` | 預設開啟，MAY 關閉 |
| 服務 | `SKILL_NAME SKILL_DIR SKILL_OUTPUT_BUDGET_BYTES PYTHONUNBUFFERED` | 由 Server 設定 |

**RFC-053a** 網路變數的轉發 MUST 預設開啟。

**理由**：缺少 proxy 變數時，對外連線**不會快速失敗**，而是阻塞到連線
逾時（常長於服務自身的 timeout），症狀表現為「Script 莫名卡住」——這是
最難診斷的失敗形式之一。

**RFC-054** Server MUST 對下列項目施加上限：

| 項目 | 上限 | 行為 |
|---|---|---|
| stdin | 4 MB | 超過即拒絕（ERR-411） |
| 單一參數長度 | 4096 字元 | 拒絕 |
| 參數數量 | 64 | 拒絕 |
| 輸出保留量 | 由預算決定 | **截斷但繼續讀取** |
| 並行子行程 | 可設定 | 排隊 |

**RFC-054a** 輸出超過上限時，Server MUST 繼續讀取管線，MUST NOT 停止讀取。

**理由**：停止讀取會使子行程阻塞在滿的管線上，形成死鎖，只能等到逾時。

**RFC-057a** Server MUST NOT 將 Skill Body、Reference 或 Script 輸出的
內容當作指令執行。這些內容一律為資料。

## 13.3 沙箱邊界（明確的能力邊界）

**RFC-058a** 本規範的防護是**路徑與參數層級**的，**不是**沙箱。

| 本規範保證 | 本規範**不**保證 |
|---|---|
| 只能執行 Bundle 內 `scripts/` 的檔案 | Script 內部行為受限 |
| 路徑無法逃逸 Bundle | 網路存取受限 |
| 伺服器機密不外洩 | 檔案系統唯讀（需容器提供） |
| 資源有上限 | 系統呼叫受限 |

**RFC-058b** Skill 作者若不受信任，部署 MUST 以容器或等效隔離執行，
本規範的檢查 MUST NOT 被當作唯一防線。

## 13.4 稽核

**RFC-059a** 稽核記錄 MUST 輸出到 stdout/stderr，MUST NOT 寫入檔案
（RFC-041）。

**RFC-059b** 稽核記錄 MUST NOT 包含 `env` 參數的值。

## 13.5 供應鏈

**RFC-059c** 依賴 MUST 以鎖定檔（lockfile）固定版本。

**RFC-059d** 容器映像 MUST 以非 root 使用者執行，MUST 設定
`readOnlyRootFilesystem: true`，MUST drop 所有 capabilities。

**RFC-059e** Skill Bundle MUST 經過與程式碼相同的審查流程。

**理由**：Bundle 內的 Script 會被執行。它是程式碼，不是設定。
