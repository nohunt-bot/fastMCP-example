# skill-mcp 文件

公司內部用的 FastMCP skill 服務。不對外、不接外部 AI 服務、skill 格式與
Claude Code 相同。

## 先讀哪一份

| 你想做的事 | 看這份 |
|---|---|
| 把服務跑起來、決定啟動參數 | [01-安裝與部署](01-安裝與部署.md) |
| 新增一個 skill | [02-Skill-撰寫指南](02-Skill-撰寫指南.md) |
| 寫 script（尤其是會呼叫 API 的） | [03-Script-撰寫規範](03-Script-撰寫規範.md) |
| 加檢查機制 | [04-Hooks-指南](04-Hooks-指南.md) |
| 驗收、交給便宜模型跑測試 | [05-驗收測試](05-驗收測試.md) |
| 出問題了 | [06-疑難排解](06-疑難排解.md) |
| 想知道跟 Claude Code 差在哪 | [07-與-Claude-Code-的對照](07-與-Claude-Code-的對照.md) |
| 部署到 k8s（尤其資源受限） | [08-k8s-部署](08-k8s-部署.md) |
| 讓其他服務套用同樣的規範 | [../spec/](../spec/README.md) |

## 三十秒版本

```bash
uv sync
uv run skill-mcp --port 8000 --context-tokens 30000
```

客戶端連 `http://127.0.0.1:8000/mcp`。

放一個 skill：

```
skills/我的技能/
├── SKILL.md          ← 必要
├── scripts/          ← 可執行的程式放這裡（只有這裡）
└── references/       ← 需要時才讀的參考文件
```

本機開發時呼叫 `reload_skills` 立即生效；正式環境 skill 打包在 image 裡，
重新部署即可。背景掃描預設關閉（見 [08-k8s-部署](08-k8s-部署.md)）。

## 核心觀念：漸進式揭露

這套服務的效能設計只有一個重點——**不要把 skill 全部塞進 context**。

| 層級 | 工具 | 花費 | 什麼時候 |
|---|---|---|---|
| 1 | `list_skills` | 每個 skill 約 30 tokens | 每次對話 |
| 2 | `load_skill(name)` | 一份 skill 內文 | 模型選定之後 |
| 3 | `read_skill_file` | 一份參考文件 | 內文指到才讀 |
| 4 | `run_skill_script` | 只有**執行結果** | 取代把程式碼讀進來 |

第 4 層最常被忽略：一支 200 行的腳本讀進 context 要 2K tokens，而且模型還得
自己重寫一遍；直接執行只花結果的大小，而且跑的是已經測過的程式。

## 目前狀態

134 個自動測試、101 個驗收項目，全數通過。實測吞吐量：

| 路徑 | rps | p50 |
|---|---|---|
| `list_skills` | 1,149 | 11.5 ms |
| `load_skill` | 1,114 | 11.8 ms |
| `run_skill_script` | 181 | 85.8 ms |
