#!/usr/bin/env python3
"""Single-pass CSV profiler. Stdlib only, so it runs under `python -I`."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")
BOOL_VALUES = {"true", "false", "yes", "no", "y", "n", "0", "1", "t", "f"}


def cell_type(value: str) -> str:
    if INT_RE.match(value):
        return "integer"
    if FLOAT_RE.match(value):
        return "float"
    if DATE_RE.match(value):
        return "datetime"
    if value.lower() in BOOL_VALUES:
        return "boolean"
    return "string"


class Column:
    __slots__ = ("name", "types", "nulls", "uniques", "overflowed", "lo", "hi", "samples")

    def __init__(self, name: str):
        self.name = name
        self.types: Counter[str] = Counter()
        self.nulls = 0
        self.uniques: set[str] = set()
        self.overflowed = False
        self.lo: float | None = None
        self.hi: float | None = None
        self.samples: list[str] = []

    def add(self, value: str, max_uniques: int) -> None:
        value = value.strip()
        if not value:
            self.nulls += 1
            return

        kind = cell_type(value)
        self.types[kind] += 1

        if len(self.uniques) < max_uniques:
            self.uniques.add(value)
        elif value not in self.uniques:
            self.overflowed = True

        if kind in ("integer", "float"):
            number = float(value)
            self.lo = number if self.lo is None else min(self.lo, number)
            self.hi = number if self.hi is None else max(self.hi, number)

        if len(self.samples) < 3:
            self.samples.append(value)

    def report(self) -> dict:
        inferred = self.types.most_common(1)[0][0] if self.types else "empty"
        out = {
            "name": self.name,
            "inferred_type": inferred,
            "nulls": self.nulls,
            "distinct": len(self.uniques),
            "distinct_is_capped": self.overflowed,
            "samples": self.samples,
        }
        if self.lo is not None:
            out["min"], out["max"] = self.lo, self.hi
        if len(self.types) > 1:
            out["type_mix"] = dict(self.types.most_common())
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile a delimited text file.")
    parser.add_argument("path", help="CSV/TSV file, or '-' for stdin")
    parser.add_argument("--limit", type=int, default=0, help="stop after N data rows")
    parser.add_argument("--delimiter", default=None, help="override delimiter sniffing")
    parser.add_argument("--max-uniques", type=int, default=50)
    args = parser.parse_args()

    handle = sys.stdin if args.path == "-" else open(args.path, newline="", encoding="utf-8-sig")
    try:
        sample = handle.read(64 * 1024)
        handle.seek(0) if args.path != "-" else None
        delimiter = args.delimiter
        if delimiter is None:
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = ","
        if args.path == "-":
            import io

            handle = io.StringIO(sample + handle.read())

        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            json.dump({"rows": 0, "columns": [], "error": "empty file"}, sys.stdout)
            return 1

        columns = [Column(name.strip() or f"col_{i}") for i, name in enumerate(header)]
        rows = 0
        ragged = 0
        for row in reader:
            if len(row) != len(columns):
                ragged += 1
            for col, value in zip(columns, row):
                col.add(value, args.max_uniques)
            rows += 1
            if args.limit and rows >= args.limit:
                break
    finally:
        if handle is not sys.stdin:
            handle.close()

    json.dump(
        {
            "rows": rows,
            "delimiter": delimiter,
            "ragged_rows": ragged,
            "truncated": bool(args.limit and rows >= args.limit),
            "columns": [c.report() for c in columns],
        },
        sys.stdout,
        indent=2,
        default=str,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
