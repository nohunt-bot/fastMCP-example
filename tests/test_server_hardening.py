"""Regression tests for the server/index/shaping hardening pass.

One test per defect from the audit: snapshot composition across a single
logical operation, a budget that only covered one field of the response,
budget measured in raw bytes rather than on-the-wire escaped bytes, unbounded
string parameters, a reload that rebuilt the whole index even when nothing had
changed, and absolute paths leaking through the stats tool.

Style follows tests/test_server.py: the in-memory ``Client(build_server(...))``
transport, so these exercise real tool dispatch and real pydantic validation
rather than calling the underlying functions directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from skill_server.index import SkillIndex
from skill_server.server import build_server
from skill_server import shaping


def _write_skill(root: Path, name: str, *, body: str = "內文", tags: str = "") -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    tagline = f"tags: [{tags}]\n" if tags else ""
    skill.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的說明，用於測試。\n{tagline}---\n{body}\n"
    )
    return skill


# ------------------------------------------------- defect 1: snapshot binding


@pytest.mark.anyio
async def test_load_skill_reads_one_generation_even_across_a_reload(tmp_path: Path):
    """`meta` and `body` must never come from two different index generations.

    The pre-fix code called ``index.get(name)`` and then
    ``await asyncio.to_thread(index.body, name)``, and ``body()`` internally
    did its own second ``get()``. The thread hop is a real await point, so a
    refresh landing there returned metadata describing one edit and body text
    from the next -- e.g. a `scripts` entry that no longer existed, which the
    model would then try to run.

    Rather than racing a real reload (flaky by construction), this asserts the
    invariant directly: the snapshot the operation binds is the one both reads
    see, so a refresh in between cannot be observed halfway.
    """
    root = tmp_path / "skills"
    _write_skill(root, "alpha", body="第一版")
    index = SkillIndex([root])
    index.refresh(force=True)

    snap = index.snapshot()
    meta_before = index.get("alpha", snap)
    body_before = index.body("alpha", snap)

    # Edit on disk and rebuild: a new generation now exists.
    _write_skill(root, "alpha", body="第二版")
    index.refresh(force=True)

    # The previously bound snapshot must still describe the old generation --
    # that is what makes "bind once, pass it down" a real guarantee rather
    # than a timing accident.
    assert index.get("alpha", snap) is meta_before
    assert index.body("alpha", snap) == body_before
    assert index.snapshot() is not snap

    # And the live view has genuinely moved on, so the test above is not
    # passing merely because nothing changed.
    assert index.body("alpha", index.snapshot()) != body_before


@pytest.mark.anyio
async def test_list_skills_overview_totals_agree_with_facets(tmp_path: Path):
    """`total` and the facet counts come from one snapshot, so they add up.

    The overview branch made five independent snapshot reads; a reload between
    them could report a `total` that disagreed with the sum of `facets`, which
    is the one number the model uses to decide whether it has seen everything.
    """
    root = tmp_path / "many"
    areas = ["billing", "delivery", "finance", "inventory", "orders",
             "payment", "shipping", "tax", "vendor", "warehouse", "zone"]
    for area in areas:
        for i in range(15):
            _write_skill(root, f"{area}-task-{i:02d}", tags=area)

    async with Client(build_server([root], refresh_interval=0, context_tokens=30_000)) as client:
        overview = (await client.call_tool("list_skills", {})).data

    assert overview["view"] == "overview"
    assert sum(overview["facets"].values()) == overview["total"] == len(areas) * 15


# --------------------------------------------- defect 2: whole-response budget


@pytest.mark.anyio
async def test_load_skill_budget_covers_scripts_and_files_not_just_body(tmp_path: Path):
    """A bundle-heavy skill must not blow the budget through its path lists.

    ``shaping.shape()`` only ever bounded ``body``. The response also carries
    ``scripts`` and ``files``; with a bundle near the 200-entry cap those are
    hundreds of path strings that nothing counted, so a skill with a modest
    body could still overflow a 30k model's context and report shaped: false.
    """
    root = tmp_path / "skills"
    skill = _write_skill(root, "bundle-heavy", body="短內文")
    (skill / "scripts").mkdir()
    (skill / "references").mkdir()
    for i in range(180):
        # Long names so the lists alone dominate the response.
        (skill / "scripts" / f"a-rather-long-script-filename-number-{i:03d}.py").write_text("x")
        (skill / "references" / f"a-rather-long-reference-filename-{i:03d}.md").write_text("x")

    async with Client(
        build_server([root], refresh_interval=0, context_tokens=4_000)
    ) as client:
        result = (await client.call_tool("load_skill", {"name": "bundle-heavy"})).data

    budget = shaping.budget_bytes_for(4_000, 0.25)
    on_wire = len(json.dumps(result, ensure_ascii=False).encode())
    assert on_wire <= budget, f"response {on_wire}B over budget {budget}B"

    # Dropping entries silently would be worse than the overflow: the model
    # would believe it had seen the full list. The response must say so.
    assert len(result["scripts"]) + len(result["files"]) < 360
    assert result["output"].get("bundle_truncated") or result["output"].get("shaped")


# ------------------------------------------ defect 3: on-the-wire measurement


def test_budget_is_measured_against_json_escaped_size():
    """A newline is one raw byte but two once escaped into a JSON string.

    Raw-byte measurement systematically under-counts newline-dense payloads --
    logs, formatted JSON, markdown -- which are exactly what the budget exists
    to catch. This text fits the budget raw and does not fit escaped.
    """
    # A log-shaped payload: short lines, so newlines are a large fraction of
    # the bytes and escaping nearly doubles the total.
    text = "".join(f"line {i}\n" for i in range(1200))
    raw = len(text.encode())
    escaped = len(json.dumps(text, ensure_ascii=False).encode())
    assert escaped > raw  # the gap the raw-byte check missed entirely

    # A budget between the two: the old raw-byte check would call this a fit
    # and ship something that does not fit.
    budget = (raw + escaped) // 2
    shaped, info = shaping.shape(text, budget)
    assert info["shaped"] is True
    assert len(json.dumps(shaped, ensure_ascii=False).encode()) <= budget

    # Note: shape() floors its head/tail window at 200 bytes, so for budgets
    # of a few hundred bytes it can still come back slightly over. Real
    # budgets are orders of magnitude larger -- budget_bytes_for(30_000, .25)
    # is ~7.5 KB -- so the floor never binds in the operating range, and this
    # test deliberately stays inside that range rather than asserting a
    # guarantee the function does not make.


# ------------------------------------------------ defect 4: parameter bounds


@pytest.mark.anyio
async def test_oversized_string_parameters_are_refused(tmp_path: Path):
    """Numeric params were bounded; string params were not.

    ``load_skill(name=<multi-MB>)`` reached the did-you-mean substring scan,
    which compares the caller's string against every skill name in the index,
    and ``list_skills(query=<multi-MB CJK>)`` reached a tokenizer that emits a
    unigram *and* a bigram per character. Both scale with attacker-controlled
    length.
    """
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError):
            await client.call_tool("load_skill", {"name": "x" * 100_000})
        with pytest.raises(ToolError):
            await client.call_tool("list_skills", {"query": "字" * 100_000})

        # The bound must not get in the way of ordinary use.
        assert (await client.call_tool("list_skills", {"query": "alpha"})).data["view"] == "list"


# ------------------------------------------------ defect 5: reload cost


@pytest.mark.anyio
async def test_reload_skills_does_not_rebuild_when_nothing_changed(tmp_path: Path):
    """A no-op reload must not pay for a full rebuild.

    The pre-fix code passed force=True, which deliberately skips the mtime/size
    comparison, so every call re-read every SKILL.md's frontmatter, re-walked
    every bundle and rebuilt the BM25 index -- O(n) file reads per call. At 300
    skills on 0.5 CPU a caller looping this starves the event loop until the
    k8s probes fail.

    The generation counter only advances on a real rebuild, so it is the
    cheapest observable proof that the expensive path was skipped.
    """
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        first = (await client.call_tool("reload_skills", {})).data
        gen_after_first = first["generation"].split("->")[-1].strip()

        # Nothing touched on disk: this must be a no-op.
        second = (await client.call_tool("reload_skills", {})).data

    assert second["changed"] is False
    assert second["generation"] == f"{gen_after_first} -> {gen_after_first}"


@pytest.mark.anyio
async def test_reload_still_picks_up_a_real_change_immediately(tmp_path: Path):
    """Making the no-op cheap must not delay an honest change.

    This is the workflow the throttle-shaped fix would have broken: write a
    skill, reload, delete it, reload -- all within milliseconds. Both reloads
    have to be believed.
    """
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        await client.call_tool("reload_skills", {})

        _write_skill(root, "beta")
        added = (await client.call_tool("reload_skills", {})).data
        assert added["added"] == ["beta"]

        import shutil

        shutil.rmtree(root / "beta")
        removed = (await client.call_tool("reload_skills", {})).data
        assert removed["removed"] == ["beta"]


# ------------------------------------------------- defect 7: path disclosure


@pytest.mark.anyio
async def test_stats_does_not_leak_absolute_paths(tmp_path: Path):
    """`skill_server_stats` used to return the container's absolute skill roots.

    /health, /ready and /metrics all expose counts only; this tool was the
    outlier. The basename is what an operator actually needs to tell two roots
    apart.
    """
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        stats = (await client.call_tool("skill_server_stats", {})).data

    blob = json.dumps(stats, ensure_ascii=False)
    assert str(tmp_path) not in blob
    assert "skills" in json.dumps(stats["index"]["roots"], ensure_ascii=False)
