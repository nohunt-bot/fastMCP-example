# Why a skill script hangs

Work top to bottom. Each step is cheap and rules out a whole class.

## 0. Read the result before theorising

The server returns everything the script printed, even when it kills it. Look at
`status` first:

| `status` | Meaning | Response |
|----------|---------|----------|
| `stalled` | no output for `stall_timeout` | it is blocked, not slow — continue below |
| `timeout` | still printing at the ceiling | genuinely slow; raise `timeout` |
| `failed` | exited non-zero | read `stderr`, this is not a hang |

`stdout` shows how far it got and `silent_for_s` how long it has been quiet.
A `stalled` result whose last line is `GET /v1/orders` has already told you the
endpoint — you do not need to reproduce anything.

## 1. Is there a timeout on the call at all?

The most common cause, by a wide margin.

| Client | Default timeout | Fix |
|--------|-----------------|-----|
| `urllib.request.urlopen` | OS default (~2 min) | `urlopen(req, timeout=10)` |
| `requests` | **none — waits forever** | `requests.get(url, timeout=(5, 10))` |
| `httpx` | 5 s | fine, but be explicit |
| `curl` | none for read | `--max-time 30 --connect-timeout 5` |

`requests` having no default is what turns a blip into a hang that outlives
every timeout you set elsewhere.

## 2. Proxy variables

If `curl` works in your shell but the script hangs, this is it. The server
forwards `HTTP(S)_PROXY`, `NO_PROXY`, `ALL_PROXY` and the TLS trust variables by
default; `--no-network-env` turns that off. Confirm what actually arrived:

```
run_skill_script("api-fetch", "scripts/fetch.py", ["https://example.com"])
```

and check `skill_server_stats()` → `runner.network_env_forwarded`.

Without a proxy variable an outbound connection does not fail fast — it blocks
until the connect timeout, which is why this presents as a hang rather than an
error.

## 3. Internal hosts and `NO_PROXY`

An internal host routed *through* the external proxy times out at the proxy.
`NO_PROXY` must list it. Note that `NO_PROXY` matching is suffix-based and does
not understand CIDR: `NO_PROXY=10.0.0.0/8` does nothing. List domain suffixes
(`.corp.internal`) instead.

## 4. TLS interception

A TLS-inspecting proxy re-signs certificates with a private CA. Verification
then fails — sometimes as a slow error rather than a fast one. Pass the CA via
`REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS`. Do not disable
verification to "fix" this.

## 5. The endpoint is genuinely slow

If the script reports progress right up to the ceiling, `status` is `timeout`,
not `stalled`, and the answer is a bigger `timeout` — or, better, a script that
pages its work so it can report progress and be resumed.

For work that legitimately runs for minutes, run the server with
`--enable-tasks` and call the tool in background mode: the client gets a handle
immediately and polls, instead of holding a request open past its own timeout.

## 6. Deadlocked on output, not on the network

A script writing megabytes to stdout can block on a full pipe if the reader
stops. This server drains past its retention cap specifically so that cannot
happen — but a script that shells out to a *grandchild* whose output nobody
reads can still deadlock. Redirect grandchild output explicitly.

## Making the next hang self-diagnosing

- Print a line before every network call, naming the endpoint.
- Print progress at least every few seconds during long work, so stall
  detection can tell "working" from "blocked".
- Keep the worst-case retry budget under the server's `timeout`.
- Send progress to stderr and results to stdout, so a partial capture is still
  parseable.
