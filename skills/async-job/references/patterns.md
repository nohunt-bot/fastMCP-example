# Working with fire-and-forget jobs

## The workflow

1. `submit` with a `--label`. Record nothing manually — the ledger does it.
2. Do something else. The job runs on its own; do not poll it.
3. Later, `list` to find the key, then `fetch --key <uuid>` for the result.

Step 3 can happen in a completely different session. That is the whole point.

## Why the ledger matters more than it looks

The job always completes. What fails is remembering how to ask for it:

- a 30 K context window rolls over in minutes of ordinary work
- the model that submitted the job may not be the one collecting it
- a restart, a new session, or a cleared conversation all lose in-context keys

The ledger (`$SKILL_STATE_DIR/jobs.jsonl`) is append-only, one JSON object per
line, so concurrent submits cannot clobber each other. It is plain text — read
it with anything.

## Labels

`--label "q3 revenue report"` costs nothing at submit time and is the difference
between a usable ledger and a list of uuids. Include what the job was *for*, not
what endpoint it hit — the endpoint is already recorded.

## Fetching

`fetch` reports `ready`:

- `ready: true` — the result is in `result`.
- `ready: false` — the backend still has it in a non-terminal state. Fetch again
  later; do not loop.

If `fetch` 404s, the key was never valid or the backend expired it. Check `list`
for a typo before re-submitting: re-submitting duplicates real work.

## When submit itself is slow

`submit` should return in milliseconds. If it does not, the endpoint is doing
work synchronously before handing back the key, and the async boundary is in the
wrong place — that is a backend fix, not something to paper over with a longer
timeout.

## Housekeeping

The ledger grows without bound. It is one line per job, so this takes a long
time to matter, but it is a plain file: rotate or truncate it on whatever
schedule suits. Nothing reads it except `list`.
