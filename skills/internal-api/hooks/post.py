#!/usr/bin/env python3
"""Post-hook: inspect or rewrite the result after the script runs.

stdin  : {"skill","script","args","result"}
stdout : optional JSON -- {"result":{...}} to replace it
exit   : 0 accepts, non-zero fails the call
"""
import json, os, pathlib, sys, time

req = json.load(sys.stdin)
result = req.get("result", {})

# A submit that produced no output is a failure even if it exited 0: whatever
# handle the API returned is the only way to reach the work later.
if req["script"].endswith("submit.py") and result.get("status") == "ok" \
        and not result.get("stdout", "").strip():
    print(json.dumps({"reason": "submit exited 0 but printed nothing -- the job handle is lost"}))
    sys.exit(1)

if state := os.getenv("SKILL_STATE_DIR"):
    line = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": req.get("script"),
            "status": result.get("status"), "output": result.get("stdout", "")[:200]}
    path = pathlib.Path(state) / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(line) + "\n")

result["audited"] = True
print(json.dumps({"result": result}))
