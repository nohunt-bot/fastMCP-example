#!/usr/bin/env python3
"""Fire a long job at the internal API and print the uuid it returns."""
import argparse, json, os, sys, time, urllib.request

p = argparse.ArgumentParser()
p.add_argument("--payload", default="{}")
p.add_argument("--url", default=os.getenv("API_BASE", "http://127.0.0.1:8899") + "/jobs")
p.add_argument("--timeout", type=float, default=10.0)
a = p.parse_args()

print(f"[{time.strftime('%H:%M:%S')}] POST {a.url}", file=sys.stderr, flush=True)
req = urllib.request.Request(
    a.url, data=a.payload.encode(),
    headers={"Content-Type": "application/json",
             "X-Request-Id": os.getenv("REQUEST_ID", "")}, method="POST")
with urllib.request.urlopen(req, timeout=a.timeout) as r:
    body = json.loads(r.read())
print(f"[{time.strftime('%H:%M:%S')}] accepted, key={body.get('key')}", file=sys.stderr, flush=True)
json.dump(body, sys.stdout, indent=2)
sys.stdout.write("\n")
