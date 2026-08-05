---
name: api-fetch
description: Call an HTTP/JSON API with explicit timeouts, retries with backoff, and progress reporting. Use for any outbound API call from a skill, and read this before writing a script that hangs.
version: 1.0.0
tags: [http, api, network]
---

# Calling an API from a skill script

Scripts that call APIs are the ones that hang. This skill is both a working
fetcher and the reference for why hangs happen.

## Usage

```
run_skill_script("api-fetch", "scripts/fetch.py",
                 ["https://api.example.com/v1/orders", "--timeout", "10"])
```

Secrets go through `env`, never through `args` — argv is visible in process
listings and in the MCP call log:

```
run_skill_script("api-fetch", "scripts/fetch.py",
                 ["https://api.example.com/me", "--token-env", "API_TOKEN"],
                 env={"API_TOKEN": "..."})
```

Options: `--method`, `--timeout` (per attempt), `--retries`, `--backoff-cap`,
`--header K:V` (repeatable), `--json-body`, `--token-env`.

## The timeout budget

The script's worst case must stay **under** the server's `timeout`, or the
server kills it mid-retry and the retries were pointless:

```
budget = timeout x (retries + 1) + backoff_cap x retries
```

Defaults (`--timeout 10 --retries 2 --backoff-cap 8`) give a 46 s worst case, so
call it with `timeout=60`. The script prints its own budget on the first line —
if that number is larger than the `timeout` you passed, fix it before running.

## Reading a failure

The script always emits JSON on stdout, with `error` set to one of:

| `error` | Means | Usual cause |
|---------|-------|-------------|
| `dns` | name did not resolve | internal-only host, or `NO_PROXY` does not cover it |
| `tls` | certificate verification failed | TLS-inspecting proxy; needs its CA via `REQUESTS_CA_BUNDLE` |
| `unreachable` | no connection established | `HTTPS_PROXY` not reaching the script |
| `auth` | token env var not set | pass it via `env`, not `args` |

A **read timeout** is reported differently from a connection failure on purpose:
it means the server accepted the connection and then went quiet, which is a
problem with the endpoint, not with your network path.

See `references/hangs.md` for the full diagnostic tree.

## Writing your own API script

Three rules, all demonstrated in `scripts/fetch.py`:

1. **Every network call gets an explicit timeout.** `urlopen(url)` with no
   `timeout=` blocks on the OS default — minutes, not seconds. Same for
   `requests` (no default timeout at all) and `httpx` (5 s, but only if you
   don't override it).
2. **Print before you block, not after.** The line printed before a call is what
   identifies the endpoint that hung. Progress on stderr also acts as a
   heartbeat: the server kills silent scripts after `stall_timeout` (20 s), so a
   script that reports progress is never mistaken for a hung one.
3. **Retry only retryable things** — 408/425/429/5xx and connection errors, with
   exponential backoff *plus jitter*, honouring `Retry-After`. Retrying a 400
   just burns budget, and retrying without jitter reproduces the overload.
