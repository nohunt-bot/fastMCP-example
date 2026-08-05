#!/usr/bin/env python3
"""Wait for a synchronous answer, printing a heartbeat so stall detection can
tell "still working" from "hung"."""
import argparse, json, os, sys, threading, time, urllib.request

p = argparse.ArgumentParser()
p.add_argument("--range", default="7d")
p.add_argument("--url", default=os.getenv("API_BASE", "http://127.0.0.1:8899") + "/report")
p.add_argument("--timeout", type=float, default=280.0)
a = p.parse_args()

stop = threading.Event()
def beat():
    n = 0
    while not stop.wait(5):
        n += 5
        print(f"[{time.strftime('%H:%M:%S')}] still waiting on {a.url} ({n}s)",
              file=sys.stderr, flush=True)
threading.Thread(target=beat, daemon=True).start()

print(f"[{time.strftime('%H:%M:%S')}] GET {a.url}?range={a.range}", file=sys.stderr, flush=True)
try:
    with urllib.request.urlopen(f"{a.url}?range={a.range}", timeout=a.timeout) as r:
        body = json.loads(r.read())
finally:
    stop.set()
json.dump(body, sys.stdout, indent=2)
sys.stdout.write("\n")
