#!/bin/bash
# Read-only summary of a git repository.
#
# Note on the absence of `| head`: under `pipefail`, `head` closing the pipe
# early sends SIGPIPE upstream and the pipeline exits 141 even though the output
# is correct. Every limit here is therefore applied by the producer (`git -n`)
# or by awk, which reads its input to EOF.
set -euo pipefail

repo="${1:-.}"
days="${2:-14}"

if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    echo "not a git repository: $repo" >&2
    exit 2
fi

git_ro() { git -C "$repo" --no-pager "$@"; }

if ! git_ro rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "## head"
    echo "repository has no commits yet"
    exit 0
fi

echo "## head"
git_ro log -1 --format='branch: %D%nsha:    %h%nauthor: %an%ndate:   %ad%nsubject:%s' --date=short
echo
echo "## recent commits (last ${days}d)"
git_ro log --since="${days} days ago" -n 40 --format='%h %ad %an: %s' --date=short
echo
echo "## churn (files by touching commits, last ${days}d)"
git_ro log --since="${days} days ago" --name-only --format='' \
    | { grep -v '^$' || true; } \
    | sort \
    | uniq -c \
    | sort -rn \
    | awk 'NR<=20 {printf "%5d  %s\n", $1, $2}'
echo
echo "## contributors (last ${days}d)"
git_ro shortlog -sn --since="${days} days ago" --all | awk 'NR<=15'
