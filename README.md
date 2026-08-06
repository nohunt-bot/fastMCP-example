# skill-mcp

A FastMCP server that serves a library of **skills** — markdown instructions with
bundled scripts — to an LLM client over HTTP, with progressive disclosure and
sandboxed script execution.

Built against FastMCP **3.4.6** / Python 3.12.

```bash
uv sync
uv run pytest -q                 # 134 tests
uv run python acceptance.py      # 101 驗收項目，中文報告
uv run skill-mcp --port 8000 --context-tokens 30000
```

Point a client at `http://127.0.0.1:8000/mcp`.

**規範在 [`spec/`](spec/README.md)** — RFC-SKILL-1，可讓其他 MCP Skill
服務套用。附可執行的驗證器：`uv run python -m spec.validate <skill 目錄>`。

**中文文件在 [`docs/`](docs/README.md)** — 安裝部署、skill 撰寫、script 規範、
hooks、驗收測試、疑難排解。`acceptance.py` 是給便宜/弱模型執行的驗收流程：中文
報告，失敗時直接給修復指示，一次只讓它修一項。

---

## The idea: don't load skills, *offer* them

The naive skill server sends every skill's full text on every session. That is
the single biggest cost in a skill system, and it grows linearly with the
library — 200 skills at 2 KB each is ~100 K tokens before the user has said
anything.

This server implements four levels of disclosure. Each one is only paid for when
the previous one justified it:

| Level | Tool | Cost | When |
|-------|------|------|------|
| 1 | `list_skills` | ~30 tokens/skill | every session |
| 2 | `load_skill(name)` | one skill body | after the model picks one |
| 2b | `load_skill(name, section=…)` | one section | when only part is needed |
| 3 | `read_skill_file` | one reference file | when the body points at one |
| 4 | `run_skill_script` | script *output* only | instead of reading code in |

Level 4 is the one people skip. A 200-line profiler script costs ~2 K tokens to
read and re-implement; running it costs the size of its JSON summary. The script
is also the tested path, which the re-implementation is not.

## Performance design

The hot path is `list_skills`: every client calls it, most calls end there. So
it is the one path with a hard rule — **no disk I/O, no locks, no per-request
parsing**.

- **Frontmatter-only indexing.** Discovery reads the first 16 KB of each
  `SKILL.md` to get name/description/tags. Bodies are never read at startup.
- **Immutable snapshot.** The index is a frozen dataclass swapped in atomically.
  Readers bind it to a local, so they can never observe a torn state and no
  synchronisation is needed at all — not even a read lock.
- **Refresh off the request path.** A background task re-stats the tree every
  5 s. The unchanged case is one `stat` per skill and allocates nothing, so
  edits show up within one interval without a request ever paying for a rescan.
- **mtime-validated body cache.** Bodies are read once, then revalidated against
  `(mtime_ns, size)` — an edit invalidates precisely, without a full rebuild.
- **Blocking I/O stays off the event loop.** Cache-miss reads go through
  `asyncio.to_thread`. Conversely, the pure-memory tools are declared
  `run_in_thread=False`, because for them a thread hop costs more than the work.
- **Stateless + JSON replies by default.** `stateless_http=True` drops the
  per-session bookkeeping, and `json_response=True` skips SSE framing on every
  reply. Together they are what makes multiple workers possible.
- **libyaml** (`CSafeLoader`) when available, with a pure-Python fallback.

### Measured

MacBook, Python 3.12, 3 example skills. `bench.py` measures client-observed
end-to-end latency — HTTP, JSON-RPC framing and pydantic validation included.

Single worker, one client at concurrency 16:

| scenario | tool | rps | p50 | p95 |
|----------|------|-----|-----|-----|
| catalog | `list_skills` | 1,136 | 11.6 ms | 24.5 ms |
| search | `list_skills(query=…)` | 1,199 | 11.4 ms | 15.9 ms |
| load | `load_skill` | 1,089 | 12.2 ms | 26.2 ms |
| section | `load_skill(section=…)` | 1,123 | 12.4 ms | 15.5 ms |
| script | `run_skill_script` | 160 | 97.8 ms | 109.1 ms |

Worker scaling, 3 concurrent bench clients (aggregate):

| workers | aggregate rps | p50 |
|---------|---------------|-----|
| 1 | ~1,247 | 34 ms |
| 4 | ~1,915 | 22 ms |

**Read that second table honestly: 4 workers buy ~1.5x, not 4x.** At this point
the benchmark client is itself a Python process saturating its own event loop,
so it is measuring the pair, not the server. The server-side headroom is real —
p50 dropped 35% at the same offered load — but if you need a defensible number
for your own deployment, drive it with a non-Python load generator.

`script` throughput is deliberately bounded: interpreter startup dominates
(~50 ms for `python -I`), and `--max-script-concurrency` (default 8) caps how
many subprocesses can exist at once. That cap is what stops a burst of script
calls from starving the cached paths. Raise it if your scripts are I/O-bound,
lower it if they are CPU-bound.

### Scaling out

```bash
uv run uvicorn skill_server.app:app --workers 8 --port 8000 --loop uvloop
```

Each worker keeps its own index (a few hundred KB) and its own refresh timer,
so after an edit they converge within one interval. Because the server is
stateless, no session affinity is needed and you can put any load balancer in
front of it.

## Scripts that call APIs

This is where skill servers hang, so it gets first-class treatment. Three
mechanisms, plus a skill that documents the root cause.

### 1. Output survives the kill

A killed script used to return an empty string — you learned only *that* it
failed, never *where*. Now the buffers are owned by the runner rather than by
the reader coroutines, so whatever was printed before the kill comes back:

```
status:     stalled
stdout:     step 1: auth ok
            step 2: GET /v1/orders      <- died here
silent_for: 3.0 s
hint:       Killed after 3s with no output. Last output: 'step 2: GET /v1/orders'. ...
```

### 2. Stall detection — fail in seconds, not at the ceiling

A hung API call and a slow one look identical until you notice nothing has been
written for a while. `stall_timeout` (default 20 s) kills a script that has gone
completely silent, so a hang costs seconds instead of the full `timeout`.

That is what makes `status` more useful than `exit_code`:

| `status` | Meaning | What to do |
|----------|---------|------------|
| `ok` | exited 0 | — |
| `failed` | exited non-zero | read `stderr` |
| `stalled` | silent for `stall_timeout` | **blocked, not slow.** Don't just raise the timeout |
| `timeout` | still printing at the ceiling | genuinely slow; raise `timeout` |

A script that prints progress is never mistaken for a hung one, which is the
whole reason the `api-fetch` skill prints before each call rather than after.

### 3. Live progress

Output lines are streamed to the client via `ctx.info` **while the script runs**,
so a slow call is visibly alive. stderr only — stdout is the result and is
returned in full anyway, so streaming it too would duplicate the payload into
the client's log. Hence the convention: progress to stderr, results to stdout.

### The environment fix you may not have noticed

The original `_child_env` allowlist stripped `HTTPS_PROXY`, `NO_PROXY`,
`REQUESTS_CA_BUNDLE` and friends. Behind a corporate proxy that alone makes
every outbound call hang — an outbound connection with no proxy variable doesn't
fail fast, it blocks until the connect timeout, which is often longer than the
server's own. These are now forwarded by default (`--no-network-env` opts out).
Unrelated secrets in the server's environment are still not inherited; there's a
test asserting exactly that.

### Really long work

For scripts that legitimately run for minutes, `--enable-tasks` lets a client
run the tool in background mode and poll, instead of holding a request open past
its own timeout. Opt-in because it needs `fastmcp[tasks]` (pydocket + Redis) —
too heavy to impose when stall detection covers the common case.

### The root cause, though

Most hangs are the script's fault, not the server's. `skills/api-fetch/` is a
working fetcher that demonstrates the fix, and
`skills/api-fetch/references/hangs.md` is the diagnostic tree. The short version:

| Client | Default timeout |
|--------|-----------------|
| `requests` | **none — waits forever** |
| `urllib.urlopen` | OS default, ~2 min |
| `httpx` | 5 s |

Keep the retry budget under the server's timeout —
`timeout x (retries+1) + backoff_cap x retries` — or the server kills the script
mid-retry and the retries bought nothing. `fetch.py` prints its own budget on
line one so a mismatch is visible before it costs you a run.

Secrets go through the tool's `env` argument, never `args`: argv shows up in
process listings and in the MCP call log.

## Execution policy: timeouts, not modes

The skill declares how long each script may take. That is the only thing that
cannot be known from the output, because it has to be decided before the script
runs:

```yaml
execution:
  default: {timeout: 60}
  scripts/submit.py:
    timeout: 15
    description: fires the job, API returns a uuid and keeps working
  scripts/report.py:
    timeout: 300
    stall_timeout: 30
```

**Whatever the script prints is the answer.** A script that fired off work
prints a uuid; one that waited prints data. The server does the same thing with
both — returns stdout verbatim.

An earlier version had `mode: background | sync`. It was removed because it
changed no behaviour: it only let the server *guess* at the meaning of something
it already had, and the guess was wrong on ordinary data —

| script output | guessed "job key" |
|---|---|
| `{"key": "7f3a-uuid", "status": "accepted"}` | `7f3a-uuid` ✓ |
| `{"id": 123, "order_no": "SO-001"}` | `123` ✗ that's an order id |
| `{"id": "u-88", "name": "Amy"}` | `u-88` ✗ that's a user id |

A wrong handle is worse than none, because it looks right. Use `description` to
tell the model what a script does — natural language is both more precise and
more flexible than an enum.

### A short timeout is an assertion

Setting `timeout: 15` says "this should answer immediately". So overrunning it
produces a different diagnosis than overrunning a 300 s ceiling:

- **short ceiling** → the endpoint is finishing the work before replying; the
  async boundary is on the wrong side. A bigger timeout hides it, not fixes it.
- **long ceiling** → genuinely slow; raise `timeout` or page the work.

A test pins that the two give opposite advice, since conflating them sends you
in the wrong direction.

## Hooks: pre and post checks

Drop executable checks into the bundle. They are ordinary scripts speaking JSON
over stdin/stdout, so they run under the same sandbox and need no new language:

```
my-skill/
├── hooks/
│   ├── pre.py     # gate: deny, inject env, or rewrite args
│   └── post.py    # audit: reject or rewrite the result
└── scripts/
```

| stage | receives | exit 0 | exit non-zero |
|---|---|---|---|
| `pre` | `{skill, script, args, mode, caller}` | allow; optional `{"env":…}`, `{"args":…}`, `{"note":…}` | **deny** — reason reaches the caller, script never runs |
| `post` | `… + {"result": …}` | pass through; optional `{"result":…}` to replace | **fail the call** |

Global hooks (`--hooks-dir`) run for every skill: global pre first (org policy
rejects early and cheaply), skill post first then global (so org audit sees what
actually went out).

Worked example in `skills/internal-api/hooks/` — the pre-hook rejects oversized
payloads and injects a request id; the post-hook writes an audit line and fails
any background submit that produced no key.

**Cost, measured, not estimated:**

| | p50 | subprocesses |
|---|---|---|
| script, no hooks | 12.9 ms | 1 |
| script + pre + post | 41.8 ms | 3 |

That is ~3.2x on `run_skill_script`, and it is the real price of an enforced
check. It is paid **only** by skills that declare hooks — a skill with no
`hooks/` directory takes the original path, and `list_skills` / `load_skill`
never touch it at all (still 1,149 rps).

## Loading skills at runtime

Skills load into a running server. There is no cold start and no restart, ever —
for a new skill, an edited body, a changed execution policy, or a hook added
after the fact.

Two mechanisms:

- **Automatic**: the background refresher picks up changes within
  `--refresh-interval` (default 5 s; measured at 1.0 s with a 1 s interval).
  Costs one `stat` per skill when nothing changed.
- **Immediate**: `reload_skills` forces a rescan and reports what moved.

```
reload → generation 1 -> 2  added=['hot-added']
list_skills sees it immediately: True
```

A newly added skill is *runnable* on the same call, not merely listed. A test
pins the harder case too: changing a skill's mode from `sync` to `background`
and adding a `hooks/pre.py` both take effect on reload, without a restart.

Multi-worker deployments converge within one refresh interval, since each worker
holds its own index.

## Fire-and-forget jobs (`{"key": "<uuid>"}` APIs)

For an endpoint that accepts work, returns a key immediately, runs to completion
on its own, and writes the result to a database. This is a deliberate
architecture — it keeps long work *out* of the model loop, so no tokens are
spent while it runs — not a race to be closed.

```
submit  ->  45 ms, returns the key, never waits
fetch   ->  collect the result by key, possibly in a different session
```

### Nothing is remembered here

The server writes no files at all — no state directory, no ledger, nothing. The
uuid exists only in the submit response, and the caller owns it from there.

To make a lost response harmless, the fix belongs in **your API**: accept a
caller-generated idempotency key on submit, so a retry with the same key returns
the same uuid instead of creating a second job. Only your API knows what counts
as "the same job"; this cannot be compensated for at the MCP layer.

### Deliberately absent

- **No waiting after submit.** That would put the long task back inside the
  model loop, which is the thing this design avoids.
- **No `list`.** The server does not remember what you submitted.
- **No cancellation.** Only your backend knows whether killing a half-finished
  job is safe.
- **`await` exists but is a last resort** — bounded and heartbeating, for the
  rare case you need the result in the same turn.

### If your poller must wait

Any script that polls has to print a heartbeat, or stall detection kills it at
20 s regardless of `timeout`. Two tests pin the contract: a silent poller **must**
be killed (it is indistinguishable from a hang), a heartbeating one **must**
survive.

## Small context windows

On a 128 K hosted model, tool output size is a rounding error. On a 30 K local
model it is the whole problem — and it usually presents as a *timeout*, not as
an error, because the wall-clock cost is the model prefilling tokens it cannot
fit anyway.

A perfectly ordinary internal REST response:

| | tokens |
|---|---|
| 500-row order list, as returned | **59,222** |
| The model's entire context window | 30,000 |

The script finished in 0.2 s. Nothing hung. The model then had to swallow twice
its context, which is where the time went.

### Byte-truncation is the wrong fix

Cutting JSON at a byte offset produces text that no longer parses. The model
burns tokens on a fragment *and* still cannot answer. `shaping.py` reduces
**structurally** instead — shrink the largest collection, keep the envelope,
say what was dropped, stay valid JSON:

```
{"total": 500, "data": [ ...500 rows... ]}                    59,222 tok
{"total": 500, "data": [ ...3 rows... ], "_truncated": {...}}     442 tok
```

`total` survives, so "how many are there" is still answerable. `_truncated`
carries a hint telling the model to narrow its arguments rather than retry.

Non-JSON keeps **head and tail**, never head alone: errors cluster at the end of
a log, so head-only truncation reliably discards the part that mattered.

### Set the budget to your actual model

```bash
uv run skill-mcp --context-tokens 30000 --port 8000
```

Everything sizes off this: a single tool result may occupy `--context-share`
(default 25%) of the window, because the output has to coexist with the prompt,
the skill body, the conversation and the reply. The 128 K default will hand a
30 K model output it cannot digest.

### Better: filter at the source

The server can only guess which rows mattered; your query cannot. `rest-client`
filters before the data is ever produced, and the server passes
`SKILL_OUTPUT_BUDGET_BYTES` to every script so it can warn when it is about to
overshoot:

| call | tokens |
|---|---|
| `[url]` — everything, server reduces it | 442 |
| `--count-only` | **43** |
| `--limit 5 --fields id,status,amount` | **134** |

On a small window the useful order is: `--count-only` to see the shape, then
`--fields` with a small `--limit`, then fetch only what you need.

## Security model

The client is treated as semi-trusted — it is an LLM, and LLMs can be talked
into things by the documents they read. The skill bundles on disk are trusted.
So every control exists to guarantee one property: **a caller can only run a
script that a skill author put in that skill's own `scripts/` directory.**

| Control | Implementation |
|---------|----------------|
| Path jail | `resolve()` then `is_relative_to(skill_dir)` — blocks `../`, absolute paths, *and* symlinks pointing out of the bundle |
| Execution scope | must live under the skill's own `scripts/` |
| Interpreter allowlist | dispatch on suffix (`.py`, `.sh`, `.js`); the executable bit grants nothing |
| No shell | `create_subprocess_exec` with an argv list — quoting/injection is not a category that exists here |
| Clean environment | minimal allowlisted env, so server API keys are not inherited |
| Timeout | `start_new_session=True` + `killpg`, so a forking script's children die too |
| Output cap | capped retention but continued draining, so a chatty script can't deadlock on a full pipe |
| Concurrency cap | semaphore, so scripts can't starve the cached paths |

Tests cover each of these, including a symlink-escape case and a check that
`MY_API_KEY` in the server's environment does not reach the child.

**What this is not:** the script still runs as the server's user, with its
filesystem and network access. This is a jail against *path and argument*
attacks, not a container. If skill authors are untrusted, run the whole server
in one — that is the correct boundary, and it is not this code's job.

## Layout

```
skill_server/
  index.py     discovery, frontmatter parsing, snapshot + body cache
  runner.py    sandboxed subprocess execution
  server.py    FastMCP tools, resources, timing middleware, CLI
  hooks.py     pre/post hook chain (skill-local + global)
  shaping.py   fit output to a context budget without breaking JSON
  app.py       ASGI entrypoint for multi-worker deployment
skills/
  csv-profile/   SKILL.md + scripts/profile.py + references/
  text-stats/    SKILL.md + scripts/wordcount.py
  repo-digest/   SKILL.md + scripts/digest.sh   (a non-Python script)
  api-fetch/     SKILL.md + scripts/fetch.py + references/hangs.md
  rest-client/   SKILL.md + scripts/call.py   (filters at the source)
  api-call/      SKILL.md + scripts/call.sh   (bash+curl, the fast path)
  async-job/     SKILL.md + scripts/job.py    (submit / fetch)
  internal-api/  two modes + hooks/pre.py + hooks/post.py
bench.py       load generator
tests/         69 tests: indexing, sandbox, hangs, modes, hooks, reload, budget, CLI
```

## Skill format

Claude Code's format, so existing skills work unchanged:

```
my-skill/
├── SKILL.md            required
├── scripts/            optional, the only runnable location
└── references/         optional, read on demand
```

```markdown
---
name: my-skill
description: One line. This is what the model sees in list_skills — write it as
  a routing decision, not a title.
version: 1.0.0
tags: [data, csv]
---

# My skill
...
```

Only `name` and `description` are read at index time. Nesting one level deeper
(`skills/<team>/<skill>/SKILL.md`) also works, for per-team namespacing.

Adding a skill is `mkdir` + write `SKILL.md` — no restart, no registration; the
refresher picks it up within 5 s.

## Tools

| Tool | Purpose |
|------|---------|
| `list_skills(query?, tags?, limit)` | compact cards; the entry point |
| `load_skill(name, section?)` | full body, or one heading's section |
| `read_skill_file(name, path, max_bytes)` | a bundled reference file |
| `run_skill_script(name, script, args?, stdin?, timeout?, stall_timeout?, env?)` | execute a bundled script; returns output even when killed |
| `skill_server_stats()` | index state, subprocess counters, per-tool p50/p95 |
| `reload_skills()` | rescan the skill tree now, no restart |

Resources mirror the read paths for clients that prefer attaching over calling:
`skill://{name}` and `skill://{name}/files/{path}`.

## Options

```
--skills DIR              skill root; repeat for several (default ./skills)
--host / --port / --path  bind address and mount path (default /mcp)
--refresh-interval SEC    index refresh timer, 0 disables (default 5)
--max-script-concurrency  concurrent subprocesses (default 8)
--script-timeout SEC      hard ceiling per script (default 30)
--script-stall-timeout    kill a script silent for this long (default 20, 0 disables)
--hooks-dir DIR           global pre.py / post.py applied to every skill
--context-tokens N        client's context window; sizes all output (default 128000)
--context-share F         max fraction of it one tool result may use (default 0.25)
--no-network-env          stop forwarding proxy/TLS vars to scripts
--enable-tasks            allow background execution; needs `uv sync --extra tasks`
--stateful                keep session state; needed for sampling/elicitation
--sse                     stream over SSE instead of plain JSON replies
```

`--stateful` and `--sse` both cost throughput and `--stateful` rules out
multiple workers. Reach for them only when a feature actually requires them.
