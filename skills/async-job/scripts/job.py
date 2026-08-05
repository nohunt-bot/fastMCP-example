#!/usr/bin/env python3
"""Fire-and-forget an API job, then collect its result later by key.

The pattern this supports, which is a deliberate architecture rather than a
problem to work around: an endpoint accepts work, returns a key immediately,
runs to completion on its own, and writes the result to a database. The point is
to keep long work *out* of the model loop entirely — no LLM token is spent while
it runs.

Two commands cover it:

    submit  -> POST, print the key, exit. Never waits.
    fetch   -> GET the result by key, once the work has landed in the database.

The failure mode this design has is not orphaned jobs — the job is meant to run
to completion — it is **losing the key**. A key that only ever existed in the
model's context is gone the moment that context rolls, and the result then sits
in the database with no way to address it. So `submit` also appends the key to a
local ledger under SKILL_STATE_DIR, and `list` reads it back. The ledger is the
memory the model does not have.

`await` exists for the occasional case where you genuinely need the result in
the same turn. It is bounded and heartbeating, but on this architecture you
usually should not be using it.
"""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.error, urllib.request
from pathlib import Path

DONE = {"done", "completed", "succeeded", "success", "finished", "failed", "error", "cancelled"}


def progress(msg: str) -> None:
    """stderr heartbeat: also what keeps a polling script from being mistaken
    for a hung one by the server's stall detector."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def emit(obj: dict) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def ledger_path() -> Path | None:
    state = os.getenv("SKILL_STATE_DIR")
    return Path(state) / "jobs.jsonl" if state else None


def record(entry: dict) -> str | None:
    """Append-only, one JSON object per line. Append-only matters: two concurrent
    submits must not be able to lose each other's key."""
    path = ledger_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return str(path)


def read_ledger(limit: int, contains: str | None) -> list[dict]:
    path = ledger_path()
    if path is None or not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if contains:
        needle = contains.lower()
        rows = [r for r in rows if needle in json.dumps(r, ensure_ascii=False).lower()]
    return rows[-limit:][::-1]  # newest first


def request(url: str, *, method: str, headers: dict, body: bytes | None, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return r.status, raw.decode("utf-8", "replace")[:2000]


def dig(payload, names: list[str]):
    if not isinstance(payload, dict):
        return None
    for name in names:
        value = payload.get(name)
        if isinstance(value, (str, int)):
            return str(value)
    for value in payload.values():
        if isinstance(value, dict) and (found := dig(value, names)) is not None:
            return found
    return None


def build_headers(args) -> dict | None:
    headers = {"Accept": "application/json"}
    for raw in args.header:
        k, _, v = raw.partition(":")
        headers[k.strip()] = v.strip()
    if args.token_env:
        token = os.getenv(args.token_env)
        if not token:
            emit({"ok": False, "error": "auth",
                  "detail": f"{args.token_env} not set; pass it via the tool's env argument"})
            return None
        headers["Authorization"] = f"Bearer {token}"
    return headers


def resolve(url: str, key: str) -> str:
    if "{key}" in url:
        return url.replace("{key}", key)
    return url if url.rstrip("/").endswith(key) else f"{url.rstrip('/')}/{key}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["submit", "fetch", "list", "await"])
    p.add_argument("url", nargs="?", default="", help="endpoint (not needed for list)")
    p.add_argument("--key")
    p.add_argument("--body", help="JSON request body for submit")
    p.add_argument("--method", default=None)
    p.add_argument("--header", action="append", default=[], metavar="K:V")
    p.add_argument("--token-env")
    p.add_argument("--timeout", type=float, default=float(os.getenv("HTTP_TIMEOUT", "10")),
                   help="per-HTTP-request timeout; NOT the job duration")
    p.add_argument("--key-field", default="key,id,job_id,task_id,uuid,request_id")
    p.add_argument("--label", help="note stored with the key, so `list` is readable later")
    p.add_argument("--limit", type=int, default=10, help="list: how many recent keys")
    p.add_argument("--grep", help="list: only entries matching this text")
    p.add_argument("--max-wait", type=float, default=60.0, help="await only")
    p.add_argument("--interval", type=float, default=2.0, help="await only")
    p.add_argument("--heartbeat", type=float, default=5.0, help="await only")
    args = p.parse_args()

    # ------------------------------------------------------------------ list
    if args.action == "list":
        rows = read_ledger(args.limit, args.grep)
        emit({"ok": True, "action": "list", "count": len(rows),
              "ledger": str(ledger_path() or "(SKILL_STATE_DIR not set)"), "jobs": rows})
        return 0

    if not args.url:
        emit({"ok": False, "error": "usage", "detail": "url is required for this action"})
        return 2
    headers = build_headers(args)
    if headers is None:
        return 1

    try:
        # ---------------------------------------------------------- submit
        if args.action == "submit":
            body = args.body.encode() if args.body else None
            if body:
                headers["Content-Type"] = "application/json"
            progress(f"{args.method or 'POST'} {args.url} (fire-and-forget)")
            status, payload = request(args.url, method=args.method or "POST",
                                      headers=headers, body=body, timeout=args.timeout)
            key = dig(payload, args.key_field.split(","))
            ledger = record({
                "key": key, "label": args.label or "", "url": args.url,
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "http_status": status,
            }) if key else None
            progress(f"key={key} recorded in {ledger}" if ledger else f"key={key} (no ledger)")
            emit({"ok": key is not None, "action": "submit", "http_status": status,
                  "key": key, "label": args.label or "", "ledger": ledger, "response": payload,
                  "next": "The job now runs to completion on its own. Do not wait for it. "
                          f"Collect it later with: fetch --key {key}" if key else
                          "no job key found in the response; pass --key-field with the right name"})
            return 0 if key else 1

        if not args.key:
            emit({"ok": False, "error": "usage", "detail": "--key is required"})
            return 2
        url = resolve(args.url, args.key)

        # ----------------------------------------------------------- fetch
        if args.action == "fetch":
            progress(f"GET {url}")
            status, payload = request(url, method="GET", headers=headers,
                                      body=None, timeout=args.timeout)
            state = dig(payload, ["status", "state", "job_status", "phase"])
            ready = state is None or state.lower() in DONE
            emit({"ok": True, "action": "fetch", "key": args.key, "http_status": status,
                  "state": state, "ready": ready, "result": payload,
                  "next": None if ready else "not written to the database yet; fetch again later"})
            return 0

        # ----------------------------------------------------------- await
        progress(f"awaiting {args.key} (max {args.max_wait}s) -- prefer submit+fetch on this API")
        started = time.monotonic()
        last_beat = 0.0
        polls = 0
        last_state = None
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= args.max_wait:
                emit({"ok": True, "action": "await", "key": args.key, "finished": False,
                      "state": last_state, "waited_s": round(elapsed, 1), "polls": polls,
                      "next": f"still running -- fetch --key {args.key} later"})
                return 0
            status, payload = request(url, method="GET", headers=headers,
                                      body=None, timeout=args.timeout)
            polls += 1
            last_state = dig(payload, ["status", "state", "job_status", "phase"])
            if (now := time.monotonic()) - last_beat >= args.heartbeat:
                progress(f"poll {polls}: state={last_state} ({elapsed:.0f}s)")
                last_beat = now
            if last_state and last_state.lower() in DONE:
                progress(f"finished: {last_state} after {elapsed:.1f}s")
                emit({"ok": last_state.lower() not in ("failed", "error"), "action": "await",
                      "key": args.key, "finished": True, "state": last_state,
                      "waited_s": round(elapsed, 1), "polls": polls, "result": payload})
                return 0 if last_state.lower() not in ("failed", "error") else 1
            time.sleep(min(args.interval, max(0.1, args.max_wait - elapsed)))

    except urllib.error.HTTPError as e:
        emit({"ok": False, "error": "http", "status": e.code, "key": args.key,
              "detail": e.read().decode("utf-8", "replace")[:1000]})
        return 1
    except Exception as e:
        emit({"ok": False, "error": type(e).__name__, "key": args.key, "detail": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
