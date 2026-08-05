#!/usr/bin/env python3
"""Pre-hook: gate the call before the script runs.

stdin  : {"skill","script","args","caller","skill_dir"}
stdout : optional JSON -- {"env":{...}} to inject, {"args":[...]} to rewrite,
         {"note":"..."} to annotate, {"reason":"..."} when denying
exit   : 0 allows, non-zero denies (the reason reaches the caller)
"""
import json, sys, uuid

req = json.load(sys.stdin)
args = req.get("args", [])

# Refuse oversized payloads here rather than letting the API reject them later.
for i, arg in enumerate(args):
    if args[i - 1] == "--payload" and len(arg) > 4096:
        print(json.dumps({"reason": f"payload is {len(arg)} bytes; limit is 4096"}))
        sys.exit(1)

# Every submit gets a traceable request id.
out = {"note": f"gated {req['script']}"}
if req["script"].endswith("submit.py"):
    out["env"] = {"REQUEST_ID": str(uuid.uuid4())}
print(json.dumps(out))
