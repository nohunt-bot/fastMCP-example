"""Fit script output into a context budget without destroying its meaning.

The problem this solves is not disk or bandwidth, it is the model's context
window. A 500-row REST response is ~200 KB ≈ 68 K tokens. On a 30 K-context
local model that is not "a bit long", it is unusable — and the prefill cost of
pushing it through the model is where the wall-clock time actually goes.

Byte-truncation is the obvious fix and the wrong one: cutting JSON mid-object
produces text that no longer parses, so the model burns tokens on a fragment it
cannot use and still does not get an answer. What works is *structural*
reduction — keep the shape, keep a sample, say what was dropped, and stay valid
JSON throughout:

    {"total": 500, "data": [ ...500 rows... ]}      207 KB
    ->
    {"total": 500, "data": [ ...3 rows... ],
     "_truncated": {"field": "data", "shown": 3, "of": 500,
                    "hint": "re-run with --offset/--limit for more"}}

The model can still answer "what fields does an order have", "what's the
status of the first one", "how many are there" — which is what it usually
needed — from 2 KB instead of 207 KB.
"""

from __future__ import annotations

import json
from typing import Any

#: Rough bytes-per-token for JSON-ish text. Deliberately conservative: better to
#: send less than to overshoot a context window.
BYTES_PER_TOKEN = 3.5


def tokens_for(text: str) -> int:
    """Approximate token count. A real tokenizer is better; this needs no model."""
    return int(len(text.encode("utf-8")) / BYTES_PER_TOKEN)


def budget_bytes_for(context_tokens: int, share: float = 0.25) -> int:
    """How many bytes of tool output a context window can afford.

    ``share`` defaults to a quarter of the window: the output has to coexist
    with the system prompt, the skill body, the conversation and the model's own
    reply. Handing a 30 K model 30 K tokens of tool output leaves no room to
    think about it.
    """
    return int(context_tokens * share * BYTES_PER_TOKEN)


def _wire_bytes(s: str) -> int:
    """Bytes ``s`` actually occupies once it is a JSON string value on the wire.

    Everything ``shape()`` returns ends up embedded as a string field in a
    larger tool-result object (``body``, ``content``, ``stdout``...), which
    the server then JSON-serialises. ``len(s.encode())`` is the size of ``s``
    on disk, not the size of ``"s-with-escapes"`` on the wire: a newline is
    one raw byte but a two-byte ``\\n`` escape once inside a JSON string, and
    markdown bodies and script stdout are newline-dense. Measuring raw bytes
    against the budget therefore systematically under-counts, worst on
    exactly the payloads (logs, formatted JSON) the budget most needs to
    catch.

    Chosen fix: let ``json.dumps`` do the escaping and measure the result.
    It is a single linear pass, same as ``str.encode`` — and it's the
    stdlib's C-accelerated encoder doing that pass, not a hand-rolled
    per-character Python loop, so it is not meaningfully more expensive than
    the ``.encode()`` call it replaces despite the extra allocation. See
    ``shape()`` for why this only runs once per call rather than in a loop.
    """
    return len(json.dumps(s, ensure_ascii=False).encode("utf-8"))


def _reduce_json(value: Any, budget: int, sample: int) -> tuple[Any, dict[str, Any] | None]:
    """Shrink the largest collection in ``value``, keeping the result valid.

    Handles the two shapes REST APIs actually return: a bare list, and an
    envelope object with the payload under one key.
    """
    if isinstance(value, list):
        if len(value) <= sample:
            return value, None
        return value[:sample], {"field": "(root)", "shown": sample, "of": len(value)}

    if isinstance(value, dict):
        # Find the longest list-valued field: that is the payload.
        lists = [(k, v) for k, v in value.items() if isinstance(v, list)]
        if not lists:
            return value, None
        key, rows = max(lists, key=lambda kv: len(kv[1]))
        if len(rows) <= sample:
            return value, None
        shrunk = dict(value)
        shrunk[key] = rows[:sample]
        return shrunk, {"field": key, "shown": sample, "of": len(rows)}

    return value, None


def shape(text: str, budget: int, *, sample: int = 3) -> tuple[str, dict[str, Any]]:
    """Fit ``text`` into ``budget`` bytes. Returns (text, info).

    ``info`` always reports what happened, so the model is never silently
    reading a fragment and mistaking it for the whole answer.

    "Fits" is judged by ``_wire_bytes`` (escaped, on-the-wire size), not raw
    UTF-8 length — see there for why the two diverge. As an incident once
    showed: a raw-byte check reported "shaped: false" for a script's stdout
    that, once JSON-escaped into the response, blew straight through a 30k
    model's context anyway. The raw length is still checked first and used
    as a cheap short-circuit: escaping only ever adds bytes, so if the raw
    length alone already exceeds budget the escaped length surely does too,
    and the (slightly pricier) escape-aware pass over a potentially huge
    payload can be skipped entirely. That covers the common case — an
    oversized dump is usually oversized by a wide margin, not by a few
    escape sequences — so the expensive path only runs for genuinely
    borderline input.
    """
    raw_len = len(text.encode("utf-8"))
    if raw_len <= budget and _wire_bytes(text) <= budget:
        return text, {"shaped": False, "bytes": raw_len, "approx_tokens": tokens_for(text)}

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _shape_text(text, budget)

    # Shrink the sample until it fits; a few rows of a wide schema can still
    # blow a small budget.
    for size in (sample, 2, 1, 0):
        reduced, dropped = _reduce_json(parsed, budget, size)
        if dropped is None and size != sample:
            break
        if dropped is not None:
            reduced = _annotate(reduced, dropped)
        candidate = json.dumps(reduced, ensure_ascii=False, indent=2, default=str)
        # Same short-circuit as above: only pay for the escape-aware measure
        # once the cheap raw check says this candidate is at least close.
        if len(candidate.encode("utf-8")) <= budget and _wire_bytes(candidate) <= budget:
            return candidate, {
                "shaped": True,
                "how": "json-structural",
                "dropped": dropped,
                "original_bytes": raw_len,
                "original_approx_tokens": tokens_for(text),
                "approx_tokens": tokens_for(candidate),
            }

    # Even one row does not fit: fall back to describing the shape only.
    outline = json.dumps(_outline(parsed), ensure_ascii=False, indent=2, default=str)
    return outline, {
        "shaped": True,
        "how": "json-outline-only",
        "original_bytes": raw_len,
        "original_approx_tokens": tokens_for(text),
        "approx_tokens": tokens_for(outline),
    }


def _annotate(value: Any, dropped: dict[str, Any]) -> Any:
    note = {
        **dropped,
        "hint": "Output exceeded the context budget. Re-run with narrower "
        "arguments (--limit/--offset, a filter, or fewer fields) rather than "
        "asking for this again.",
    }
    if isinstance(value, dict):
        return {**value, "_truncated": note}
    return {"_truncated": note, "data": value}


def _outline(value: Any, depth: int = 0) -> Any:
    """Types and sizes, no values. The last resort that still parses."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: _outline(v, depth + 1) for k, v in list(value.items())[:20]}
    if isinstance(value, list):
        head = _outline(value[0], depth + 1) if value else "empty"
        return {"_list_of": len(value), "_item_shape": head}
    return type(value).__name__


def _shape_text(text: str, budget: int) -> tuple[str, dict[str, Any]]:
    """Non-JSON: keep the head and the tail, drop the middle.

    Errors and summaries cluster at the end of a log, so a pure head-truncation
    reliably throws away the part that mattered.

    The head/tail split is first sized in raw bytes, then checked against the
    escaped wire size (see ``_wire_bytes``): a log or stack trace is exactly
    the newline-dense content that expands most under JSON escaping, so a
    split that looks budget-sized in raw bytes can still overshoot once
    escaped. When it does, shrink ``keep`` proportionally to the overshoot
    and re-check. This is a handful of iterations at most (the expansion
    ratio barely moves once the split is already close), and every iteration
    re-measures only the shaped candidate — which is budget-sized by
    construction — never the original (possibly huge) ``text``.
    """
    raw = text.encode("utf-8")
    keep = max(budget - 120, 200)
    shaped = ""
    dropped = 0
    for _ in range(6):
        head_size, tail_size = int(keep * 0.6), keep - int(keep * 0.6)
        head = raw[:head_size].decode("utf-8", "ignore")
        tail = raw[-tail_size:].decode("utf-8", "ignore") if tail_size else ""
        dropped = len(raw) - head_size - tail_size
        shaped = f"{head}\n\n... [{dropped:,} bytes dropped from the middle] ...\n\n{tail}"
        wire = _wire_bytes(shaped)
        if wire <= budget or keep <= 200:
            break
        keep = max(int(keep * budget / wire), 200)
    return shaped, {
        "shaped": True,
        "how": "text-head-tail",
        "dropped_bytes": dropped,
        "original_bytes": len(raw),
        "original_approx_tokens": tokens_for(text),
        "approx_tokens": tokens_for(shaped),
    }
