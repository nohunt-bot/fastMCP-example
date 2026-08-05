---
name: internal-api
description: Call the internal job API. submit.py fires a long job and returns its uuid for the front end; report.py waits for a synchronous answer. Use submit for anything that takes more than a few seconds.
version: 1.0.0
tags: [api, internal, jobs]
execution:
  default:
    timeout: 60
  scripts/submit.py:
    timeout: 15
    stall_timeout: 10
    description: 送出任務，API 立刻回 uuid 後自己跑完；把 uuid 交給呼叫端即可
  scripts/report.py:
    timeout: 300
    stall_timeout: 30
    description: 等 API 回傳完整答案，過程中會印心跳
---

# Internal job API

Two scripts. Whatever each one prints **is** the answer — the server does not
interpret it. `submit.py` prints a uuid; `report.py` prints data. The only thing
declared in frontmatter is how long each is allowed to take, because that has to
be decided before the script runs.

## Background: `submit.py`

```
run_skill_script("internal-api", "scripts/submit.py", ["--payload", "{...}"])
```

Returns in a couple of seconds. `stdout` is the uuid the API handed back; the
API keeps working and writes the result to its database. **Return that uuid to
the front end and stop.** Do not wait for it here.

The 15 s ceiling is the point: if it is ever hit, the endpoint is doing the work
before handing back the uuid — an async-boundary bug on the API side, not
something a longer timeout fixes. The tool result says so explicitly.

## Synchronous: `report.py`

```
run_skill_script("internal-api", "scripts/report.py", ["--range", "7d"])
```

Waits for the real answer, up to 300 s. It prints a heartbeat while waiting, so
stall detection can tell it apart from a hang.

## Hooks

`hooks/pre.py` rejects payloads above a size limit and injects a request id.
`hooks/post.py` records an audit line for every run. Both are plain scripts;
see the top of each file.
