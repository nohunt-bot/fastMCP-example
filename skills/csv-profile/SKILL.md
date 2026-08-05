---
name: csv-profile
description: Profile a CSV or TSV file — column types, null counts, cardinality, numeric ranges and sample values. Use before writing any analysis or transformation code against an unfamiliar tabular file.
version: 1.0.0
tags: [data, csv, analysis]
---

# CSV profiling

Run the bundled profiler rather than reading the file into context. A 500 MB
CSV costs the same number of tokens as a 5 KB one, because only the summary
comes back.

## Usage

```
run_skill_script("csv-profile", "scripts/profile.py", ["/path/to/data.csv"])
```

Options, passed as further argv entries:

- `--limit N` — stop after N data rows (default: whole file)
- `--delimiter ,` — override sniffing
- `--max-uniques N` — how many distinct values to track per column (default 50)

Output is JSON on stdout: `{"rows": …, "columns": [{"name", "inferred_type",
"nulls", "distinct", "min", "max", "samples"}]}`.

## Reading the result

- `inferred_type` is decided by majority vote over non-empty cells, so a column
  reported as `integer` may still hold a few stray strings — check `samples`.
- `distinct` saturates at `--max-uniques`; a value equal to the cap means "at
  least this many", not "exactly this many".
- A column where `distinct == rows` is a candidate key. A column where
  `distinct == 1` carries no information and can be dropped.

See `references/type-inference.md` for the exact type-inference rules before
relying on them for schema generation.

## When not to use this

For files above ~2 GB, or anything already in a database, profile at the source
instead — this script is single-pass but still reads every row.
