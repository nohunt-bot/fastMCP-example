---
name: async-job
description: Fire off a long-running API job that returns a key immediately and writes its result to a database, then collect that result later by key. Use for work that must not occupy the model while it runs.
version: 2.0.0
tags: [http, api, async, jobs]
---

# Fire-and-forget API jobs

For an endpoint that accepts work, returns `{"key": "<uuid>"}` straight away,
runs to completion on its own, and writes the result to a database. The job is
*meant* to outlive the call — the point is that no model tokens are spent while
it runs.

## The two calls you actually need

```
# 1. fire it off — returns in milliseconds, never waits
run_skill_script("async-job", "scripts/job.py",
                 ["submit", "http://internal/api/jobs",
                  "--body", "{\"report\":\"q3\"}", "--label", "q3 report"])

# 2. later, possibly in a different session — collect by key
run_skill_script("async-job", "scripts/job.py",
                 ["fetch", "http://internal/api/results", "--key", "<uuid>"])
```

`submit` returns and the job keeps running. That is correct behaviour, not a
race to be closed.

## The real risk here is losing the key

The job will finish regardless. What breaks is the *retrieval*: a key that only
ever existed in the model's context disappears the moment that context rolls,
and the result then sits in the database with no way to address it. On a small
context window this is a matter of minutes, not days.

So `submit` also appends the key to a ledger under `SKILL_STATE_DIR`, which
survives across calls, sessions and restarts:

```
run_skill_script("async-job", "scripts/job.py", ["list", "--limit", "10"])
run_skill_script("async-job", "scripts/job.py", ["list", "--grep", "q3"])
```

Always pass `--label` — it is what makes the ledger readable weeks later, when
the uuid on its own tells you nothing.

## Deliberately not here

- **No waiting after submit.** That would put the long task back inside the
  model loop, which is exactly what this design avoids.
- **No cancellation.** Cancelling a partially-complete job is only safe if your
  backend says it is.
- **`await` exists but is a last resort.** Bounded and heartbeating, for the
  occasional case where you truly need the result in the same turn. On this
  architecture, reach for `submit` + `fetch` instead.

## Field names

Job key: looked up under `key,id,job_id,task_id,uuid,request_id`, one level
deep. Override with `--key-field`. State (for `fetch`): `status,state,
job_status,phase`. A response with no state field is treated as ready.

`--timeout` bounds **one HTTP request**, not the job. 10 s is plenty for a
submit that returns immediately.

See `references/patterns.md` for the retrieval workflow.
