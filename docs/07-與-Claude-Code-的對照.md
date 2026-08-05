# 07 與 Claude Code 的對照

目標是「貼近 Claude Code」，所以要說清楚哪裡一樣、哪裡不一樣、以及為什麼不一樣。

## 完全相同

| 項目 | 說明 |
|---|---|
| 目錄結構 | `<skill>/SKILL.md` + `scripts/` + `references/` |
| frontmatter 格式 | YAML，`---` 包住，放在檔案最前面 |
| `name` 規則 | 小寫字母、數字、連字號，最多 64 字元。**強制檢查** |
| `description` | 必要，上限 1024 字元 |
| `allowed-tools` | 有解析、有透傳 |
| `license` | 有解析 |
| 漸進式揭露 | list（只有 description）→ load（內文）→ read（參考檔） |
| 命名空間 | 支援多一層目錄分組 |

**現有的 Claude Code skill 可以直接複製過來用**，不需要改任何東西。

## 這裡有、Claude Code 沒有

這些是為了你的情境（地端模型、內網 API、背景任務）加的擴充。都是 frontmatter
的**可選**欄位，Claude Code 讀到會忽略，不會壞掉。

| 擴充 | 為什麼需要 |
|---|---|
| `execution.timeout` / `stall_timeout` | Claude Code 沒有 script 執行器。timeout 必須在執行前決定，是唯一無法從輸出得知的資訊 |
| `hooks/` | 內部服務需要強制檢查點，不能靠模型自律 |
| 卡住偵測 | script 呼叫 API 才有的問題 |
| 輸出預算（`--context-tokens`） | Claude Code 面對的是大 context 模型；30k 的地端模型必須壓縮 |
| `SKILL_STATE_DIR` | fire-and-forget 的 key 要活得比 context 久 |
| `reload_skills` 工具 | Claude Code 是 CLI，每次啟動都重新掃描；這是常駐服務 |

## Claude Code 有、這裡沒有

**都是刻意不做的**，理由如下：

| 項目 | 為什麼不做 |
|---|---|
| 強制執行 `allowed-tools` | 本服務是 MCP server，不擁有客戶端的工具清單。只能透傳，由客戶端自行強制。**如果你的客戶端沒實作，這個欄位就只是註解** |
| plugin 機制（`plugin:skill`） | 內部單一 skill 庫用不到；需要時用命名空間目錄即可 |
| `~/.claude/skills/` 標準路徑 | 這是服務不是 CLI，路徑由 `--skills` 指定 |
| skill 的自動觸發 | 由客戶端的模型決定，服務只負責提供 |

## 刻意不加的東西

一度加過 `execution.mode: background | sync`，後來移除。理由值得記錄：

送出型 script 印 uuid、等待型印資料，服務對兩者做的事**完全相同**——原樣回傳
stdout。模式旗標沒有改變任何行為，只是讓服務去猜它已經拿到的東西是什麼意思，
而猜測會把訂單的 `{"id": 123}` 誤判成任務代號。一個錯的代號比沒有更糟。

留下的只有 `timeout`，因為那是唯一必須在執行前決定、無法從輸出得知的東西。

## 值得注意的行為差異

### 不合規的 skill 會被拒絕，不是靜默降級

Claude Code 對格式問題比較寬容。這裡因為 `name` 同時是查詢鍵**也是狀態目錄的
名稱**，不合規會被拒絕載入，並列在 `skill_server_stats().index.rejected` 裡。

這是安全需求：一個叫 `../../etc` 的 skill 會把狀態寫到根目錄外面。這個攻擊在
加上檢查之前是真的可行的。

### 內文與 description 可以是中文，`name` 不行

`name` 必須是 ASCII kebab-case。但 `description` 和內文是給模型讀的，中文完全
沒問題，也建議用中文寫——你的使用者用中文提問。

```yaml
name: order-lookup                      # 英文
description: 查詢內部訂單系統的狀態與明細。當使用者問到訂單編號時使用。   # 中文
```

### hooks/ 不會列給模型看

`hooks/` 底下的檔案不會出現在 `files` 清單裡。它們是機制不是內容，列出來只是
浪費 token。

## 遷移檢查

從 Claude Code 搬 skill 過來時：

- [ ] `name` 是 kebab-case（Claude Code 也要求，但可能沒被強制過）
- [ ] `description` 在 1024 字元內
- [ ] 有 `scripts/` 的話，考慮加 `execution.timeout`（不加也能跑，用服務預設的 30 秒）
- [ ] 會呼叫 API 的 script 檢查有沒有設 timeout（見 [03](03-Script-撰寫規範.md)）
- [ ] 跑 `uv run python acceptance.py --group 規格對齊`

## 已修補的安全問題

以下都是實際攻擊測試中**成功**過的，現在每一項都有回歸測試。如果你自己改了
`runner.py` 或 `hooks.py`，記得跑 `--group 安全` 確認沒有退回去。

| 問題 | 影響 |
|---|---|
| `hooks/pre.py` 是指向外部的 symlink | 任意程式碼執行，而且**每次呼叫都執行** |
| 呼叫端可設 `env={"PATH": ...}` | 等同遠端執行任意程式。呼叫端是 LLM，可能被它讀到的文件說服 |
| 呼叫端可設 `LD_PRELOAD` / `PYTHONPATH` | 同上 |
| skill `name` 路徑穿越 | 狀態目錄寫到根目錄外面 |
| stdin 無上限 | 記憶體耗盡 |
| `read_skill_file` / `load_skill` 不受預算限制 | 400KB 參考檔（約 114k tokens）直接回給 30k 的模型，而且外觀是「讀取成功」 |

`env` 的封鎖清單只擋**會改變執行對象**的變數（`PATH`、`LD_*`、`PYTHON*`、
`SKILL_*` 等）。傳資料用的 `env`（token、base URL）完全正常，有測試確認沒有
誤傷。
