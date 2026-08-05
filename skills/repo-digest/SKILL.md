---
name: repo-digest
description: Summarise a git repository — recent commits, churn by file, contributor counts and branch state. Use to orient yourself in an unfamiliar repo before reading any source.
version: 1.0.0
tags: [git, repo, orientation]
---

# Repository digest

A shell skill, included here to show that the runner is not Python-only:
anything with a registered interpreter under `scripts/` works the same way.

## Usage

```
run_skill_script("repo-digest", "scripts/digest.sh", ["/path/to/repo", "30"])
```

Arguments are positional: repository path (default `.`), then the number of days
of history to summarise (default `14`).

## Output

Plain text, four sections: `## head`, `## recent commits`, `## churn`
(files ranked by number of touching commits), `## contributors`.

## Notes

- Read-only: the script never writes to the repo, and runs no command that can
  change state.
- Churn ranks by *commit count*, not lines changed, so a file with one enormous
  rewrite ranks below one with many small edits. That is usually the more
  useful signal for "where is the activity", but it is the wrong metric if you
  are looking for large refactors.
- Exits non-zero with a message on stderr if the path is not a git repository.
