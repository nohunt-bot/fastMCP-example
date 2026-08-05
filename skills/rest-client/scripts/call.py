#!/usr/bin/env python3
"""Fetch JSON from a REST endpoint and reduce it before it ever reaches a model."""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.error, urllib.request


def progress(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def rows_of(payload):
    """Find the row list in either a bare array or a {data: [...]} envelope."""
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        lists = [(k, v) for k, v in payload.items() if isinstance(v, list)]
        if lists:
            return max(lists, key=lambda kv: len(kv[1]))[1], max(lists, key=lambda kv: len(kv[1]))[0]
    return None, None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--fields", default="")
    p.add_argument("--count-only", action="store_true")
    p.add_argument("--timeout", type=float, default=float(os.getenv("HTTP_TIMEOUT", "10")))
    p.add_argument("--header", action="append", default=[])
    p.add_argument("--token-env")
    args = p.parse_args()

    headers = {"Accept": "application/json"}
    for raw in args.header:
        k, _, v = raw.partition(":")
        headers[k.strip()] = v.strip()
    if args.token_env:
        token = os.getenv(args.token_env)
        if not token:
            progress(f"{args.token_env} not set")
            return 1
        headers["Authorization"] = f"Bearer {token}"

    progress(f"GET {args.url} (timeout {args.timeout}s)")
    req = urllib.request.Request(args.url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        json.dump({"ok": False, "status": e.code, "body": e.read().decode("utf-8", "replace")[:2000]}, sys.stdout)
        return 1
    except Exception as e:
        json.dump({"ok": False, "error": type(e).__name__, "detail": str(e)}, sys.stdout)
        return 1

    rows, key = rows_of(payload)
    if rows is None:
        json.dump({"ok": True, "data": payload}, sys.stdout, ensure_ascii=False, indent=2, default=str)
        return 0

    total = len(rows)
    progress(f"200 OK, {total} rows")

    if args.count_only:
        json.dump({"ok": True, "total": total,
                   "fields": sorted(rows[0].keys()) if rows and isinstance(rows[0], dict) else []},
                  sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    window = rows[args.offset: args.offset + args.limit] if args.limit else rows[args.offset:]
    if args.fields:
        keep = [f.strip() for f in args.fields.split(",") if f.strip()]
        window = [{k: r.get(k) for k in keep} if isinstance(r, dict) else r for r in window]

    out = {"ok": True, "total": total, "offset": args.offset,
           "returned": len(window), (key or "data"): window}
    text = json.dumps(out, ensure_ascii=False, indent=2, default=str)

    budget = int(os.getenv("SKILL_OUTPUT_BUDGET_BYTES", "0"))
    if budget and len(text.encode()) > budget:
        progress(f"WARNING: {len(text.encode()):,} bytes exceeds the {budget:,} byte budget "
                 f"-- narrow it with --limit/--fields; the server will shrink this otherwise")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
