#!/usr/bin/env python3
"""HTTP fetch with the timeouts, retries and progress reporting that a script
called by an MCP server actually needs. Stdlib only, so it runs under `python -I`.

The three rules this file exists to demonstrate:

1. **Every network call has an explicit timeout.** `urlopen` without one blocks
   on the OS default, which can be minutes. This is the single most common
   reason a skill script "just hangs".
2. **Print before you block, not after.** The line you print before a call is
   what tells you which endpoint hung. Progress goes to stderr so stdout stays
   clean JSON, and it doubles as the heartbeat the server's stall detector
   watches — a script that reports progress is never mistaken for a hung one.
3. **Retry only what is retryable**, with backoff and jitter, and honour
   `Retry-After`. Retrying a 400 just burns the timeout budget.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def progress(message: str) -> None:
    """Heartbeat + breadcrumb. stderr, so stdout stays machine-readable."""
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _sleep_for(attempt: int, retry_after: str | None, cap: float) -> float:
    """Honour Retry-After when the server sent one, else exponential + jitter.

    The jitter matters: without it, N scripts that failed together retry
    together and reproduce the overload that caused the failure.
    """
    if retry_after:
        try:
            return min(float(retry_after), cap)
        except ValueError:
            pass  # Retry-After can be an HTTP-date; fall through to backoff
    return min(2.0**attempt + random.uniform(0, 1), cap)


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    retries: int,
    backoff_cap: float,
) -> tuple[int, dict[str, str], bytes]:
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        # Printed BEFORE the call: if we hang, this is the last thing you see,
        # and it names the endpoint and the attempt.
        progress(f"{method} {url} (attempt {attempt + 1}/{retries + 1}, timeout {timeout}s)")
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
                elapsed = time.monotonic() - started
                progress(f"{response.status} in {elapsed:.2f}s, {len(payload)} bytes")
                return response.status, dict(response.headers), payload

        except urllib.error.HTTPError as exc:
            payload = exc.read()
            elapsed = time.monotonic() - started
            progress(f"HTTP {exc.code} in {elapsed:.2f}s")
            if exc.code not in RETRYABLE_STATUS or attempt == retries:
                return exc.code, dict(exc.headers), payload
            delay = _sleep_for(attempt, exc.headers.get("Retry-After"), backoff_cap)
            progress(f"retryable status {exc.code}, sleeping {delay:.1f}s")
            time.sleep(delay)
            last_error = exc

        except (socket.timeout, TimeoutError) as exc:
            # Distinguished from a connection error on purpose: a read timeout
            # means we reached the server and it went quiet, which is a very
            # different problem from not reaching it at all.
            progress(f"TIMEOUT after {timeout}s — server accepted the connection but did not respond")
            last_error = exc
            if attempt == retries:
                break
            time.sleep(_sleep_for(attempt, None, backoff_cap))

        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLError):
                progress(f"TLS failure: {reason}. Check REQUESTS_CA_BUNDLE / SSL_CERT_FILE.")
                raise SystemExit(_fail(url, "tls", str(reason)))
            if isinstance(reason, socket.gaierror):
                progress(f"DNS failure for {url}: {reason}")
                raise SystemExit(_fail(url, "dns", str(reason)))
            progress(f"connection failed: {reason}")
            last_error = exc
            if attempt == retries:
                break
            time.sleep(_sleep_for(attempt, None, backoff_cap))

    raise SystemExit(_fail(url, "unreachable", str(last_error)))


def _fail(url: str, kind: str, detail: str) -> int:
    hints = {
        "dns": "Name did not resolve. Inside a corporate network this usually means "
               "the host is internal-only, or NO_PROXY is not covering it.",
        "tls": "Certificate verification failed. A TLS-inspecting proxy needs its CA "
               "passed via REQUESTS_CA_BUNDLE or SSL_CERT_FILE.",
        "unreachable": "Could not establish a connection. If curl works from a shell "
                       "but this does not, HTTPS_PROXY is probably not reaching the script.",
    }
    json.dump(
        {"ok": False, "url": url, "error": kind, "detail": detail, "hint": hints.get(kind, "")},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a URL with sane timeouts and retries.")
    parser.add_argument("url")
    parser.add_argument("--method", default="GET")
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("HTTP_TIMEOUT", "10")),
        help="Per-attempt timeout. Keep total (timeout x attempts + backoff) "
        "below the server's own timeout, or you get killed mid-retry.",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff-cap", type=float, default=8.0)
    parser.add_argument(
        "--header", action="append", default=[], metavar="K:V", help="repeatable"
    )
    parser.add_argument("--json-body", help="request body; implies POST if --method unset")
    parser.add_argument("--token-env", help="env var holding a bearer token, e.g. API_TOKEN")
    args = parser.parse_args()

    headers = {"User-Agent": "skill-mcp/api-fetch", "Accept": "application/json"}
    for raw in args.header:
        key, _, value = raw.partition(":")
        headers[key.strip()] = value.strip()

    if args.token_env:
        # Read the secret from the environment; never from argv, which shows up
        # in process listings and in the MCP call log.
        token = os.getenv(args.token_env)
        if not token:
            progress(f"{args.token_env} is not set in this script's environment")
            return _fail(args.url, "auth", f"{args.token_env} not set; pass it via the `env` argument")
        headers["Authorization"] = f"Bearer {token}"

    body = None
    method = args.method
    if args.json_body:
        body = args.json_body.encode()
        headers["Content-Type"] = "application/json"
        if method == "GET":
            method = "POST"

    budget = args.timeout * (args.retries + 1) + args.backoff_cap * args.retries
    progress(f"worst-case budget {budget:.0f}s — server timeout must exceed this")

    status, response_headers, payload = fetch(
        args.url,
        method=method,
        headers=headers,
        body=body,
        timeout=args.timeout,
        retries=args.retries,
        backoff_cap=args.backoff_cap,
    )

    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = payload.decode("utf-8", "replace")[:4000]

    json.dump(
        {
            "ok": 200 <= status < 300,
            "status": status,
            "url": args.url,
            "content_type": response_headers.get("Content-Type", ""),
            "body": parsed,
        },
        sys.stdout,
        indent=2,
        default=str,
    )
    sys.stdout.write("\n")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
