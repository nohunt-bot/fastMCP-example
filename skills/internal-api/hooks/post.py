#!/usr/bin/env python3
"""Post-hook: inspect or rewrite the result after the script runs.

stdin  : {"skill","script","args","result"}
stdout : optional JSON -- {"result":{...}} to replace it
exit   : 0 accepts, non-zero fails the call

不寫任何檔案：服務跑在唯讀檔案系統上。要稽核就把事件送到你的 API 或
標準輸出（容器日誌會收），不要寫本地檔案。
"""
import json, sys

req = json.load(sys.stdin)
result = req.get("result", {})

# A submit that produced no output is a failure even if it exited 0: whatever
# handle the API returned is the only way to reach the work later.
if req["script"].endswith("submit.py") and result.get("status") == "ok" \
        and not result.get("stdout", "").strip():
    print(json.dumps({"reason": "submit exited 0 but printed nothing -- the job handle is lost"}))
    sys.exit(1)

# 稽核走 stderr -> 容器日誌 -> 你的日誌系統，不落地成檔案
print(f"AUDIT script={req.get('script')} status={result.get('status')}", file=sys.stderr)
result["audited"] = True
print(json.dumps({"result": result}))
