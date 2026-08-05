---
name: rest-client
description: Call an internal RESTful API and return only the fields you asked for. Use when a plain REST endpoint returns more rows or columns than the answer needs.
version: 1.0.0
tags: [http, api, rest]
---

# Internal REST client

Built for small context windows: it filters **at the source**, so a 500-row
response never becomes 68 K tokens in the first place.

## Usage

```
run_skill_script("rest-client", "scripts/call.py",
                 ["http://internal/api/orders", "--limit", "5", "--fields", "id,status,amount"])
```

- `--limit N` / `--offset N` — page instead of fetching everything
- `--fields a,b,c` — keep only these keys per row (the biggest single saving)
- `--count-only` — return just the row count and the field names
- `--timeout` / `--retries` — same budget rules as `api-fetch`

The script reads `SKILL_OUTPUT_BUDGET_BYTES` (set by the server from your
context window) and warns on stderr when its own output would exceed it.

## On a small context window

Ask for `--count-only` first, then `--fields` with `--limit 3` to see the shape,
and only then fetch what you actually need. Fetching everything and letting the
server shrink it works, but the server can only guess which rows mattered — your
filter cannot.
