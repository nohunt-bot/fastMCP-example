#!/usr/bin/env python3
"""Word/line/sentence statistics and term frequencies. Reads a file or stdin."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter

WORD_RE = re.compile(r"[A-Za-z0-9']+")
SENTENCE_RE = re.compile(r"[.!?]+(?:\s|$)")
STOPWORDS = frozenset(
    """a an the and or but if then than that this these those of in on at to for with
    from by as is are was were be been being it its it's i you he she they we not no
    do does did have has had will would can could should may might must there their
    what which who whom how when where why all any both each more most other some such
    only own same so too very just about into over after before""".split()
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Text statistics.")
    parser.add_argument("path", nargs="?", default="-", help="file to read, or '-' for stdin")
    parser.add_argument("--top", type=int, default=10, help="how many frequent terms to return")
    parser.add_argument("--keep-stopwords", action="store_true")
    args = parser.parse_args()

    if args.path == "-":
        text = sys.stdin.read()
    else:
        with open(args.path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()

    words = WORD_RE.findall(text)
    lowered = [w.lower() for w in words]
    if not args.keep_stopwords:
        lowered = [w for w in lowered if w not in STOPWORDS and len(w) > 1]

    sentences = [s for s in SENTENCE_RE.split(text) if s.strip()]
    counts = Counter(lowered)

    json.dump(
        {
            "chars": len(text),
            "words": len(words),
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "sentences": len(sentences),
            "avg_word_length": round(sum(len(w) for w in words) / len(words), 2) if words else 0,
            "avg_sentence_length": round(len(words) / len(sentences), 2) if sentences else 0,
            "top_terms": [{"term": t, "count": c} for t, c in counts.most_common(args.top)],
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
