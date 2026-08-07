"""Smoke + security tests. Run: uv run --extra dev pytest -q

Uses FastMCP's in-memory transport (`Client(mcp)`), which exercises the real
tool dispatch, validation and serialisation path without opening a socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
from fastmcp import Client

from skill_server.index import SkillIndex, SkillLoadError
from skill_server.runner import ScriptError, ScriptRunner
from skill_server.server import build_server

SKILLS = Path(__file__).resolve().parent.parent / "skills"


@pytest.fixture
def index() -> SkillIndex:
    return SkillIndex([SKILLS])


@pytest.fixture
def client():
    return Client(build_server([SKILLS], refresh_interval=0))


# ------------------------------------------------------------------- indexing


def test_index_finds_the_example_skills(index: SkillIndex):
    names = {card["name"] for card in index.catalog(limit=100)}
    assert {"csv-profile", "text-stats", "repo-digest"} <= names


def test_cards_carry_no_body(index: SkillIndex):
    card = next(c for c in index.catalog(limit=100) if c["name"] == "csv-profile")
    assert set(card) <= {"name", "description", "tags", "scripts"}
    assert "scripts/profile.py" in card["scripts"]


def test_search_ranks_name_matches_first(index: SkillIndex):
    assert index.catalog(query="csv")[0]["name"] == "csv-profile"
    assert index.catalog(query="zzz-nothing-matches") == []


def test_tag_filter_requires_all_tags(index: SkillIndex):
    assert index.catalog(tags=["git"])[0]["name"] == "repo-digest"
    assert index.catalog(tags=["git", "csv"]) == []


def test_body_is_cached_and_frontmatter_stripped(index: SkillIndex):
    body = index.body("csv-profile")
    assert body.startswith("# CSV profiling")
    assert "description:" not in body
    assert index.body("csv-profile") is body  # same object => served from cache


def test_body_cache_invalidates_on_edit(tmp_path: Path):
    skill = tmp_path / "tmp-skill"
    (skill / "scripts").mkdir(parents=True)
    md = skill / "SKILL.md"
    md.write_text("---\nname: tmp-skill\ndescription: d\n---\nfirst\n")

    idx = SkillIndex([tmp_path])
    assert idx.body("tmp-skill").strip() == "first"

    md.write_text("---\nname: tmp-skill\ndescription: d\n---\nsecond\n")
    assert idx.refresh() is True
    assert idx.body("tmp-skill").strip() == "second"
    assert idx.refresh() is False  # nothing changed => no rebuild


def test_unknown_skill_suggests_alternatives(index: SkillIndex):
    with pytest.raises(SkillLoadError, match="Did you mean"):
        index.get("csv")


# ------------------------------------------------------------------- security


@pytest.mark.parametrize(
    "path",
    ["../../etc/passwd", "/etc/passwd", "scripts/../../../etc/passwd", "references/../../x"],
)
def test_path_traversal_is_refused(index: SkillIndex, path: str):
    with pytest.raises(SkillLoadError):
        index.resolve_file("csv-profile", path)


def test_symlink_out_of_bundle_is_refused(tmp_path: Path):
    skill = tmp_path / "linky"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: linky\ndescription: d\n---\nbody\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    (skill / "leak.txt").symlink_to(secret)

    idx = SkillIndex([tmp_path])
    with pytest.raises(SkillLoadError, match="escapes"):
        idx.resolve_file("linky", "leak.txt")


@pytest.mark.anyio
async def test_only_scripts_dir_is_runnable(index: SkillIndex):
    runner = ScriptRunner(index)
    with pytest.raises(ScriptError, match="only files under scripts/"):
        await runner.run("csv-profile", "references/type-inference.md")


@pytest.mark.anyio
async def test_unregistered_interpreter_is_refused(tmp_path: Path):
    skill = tmp_path / "evil"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: evil\ndescription: d\n---\nbody\n")
    binary = skill / "scripts" / "payload.bin"
    binary.write_text("#!/bin/sh\necho pwned\n")
    binary.chmod(0o755)

    runner = ScriptRunner(SkillIndex([tmp_path]))
    with pytest.raises(ScriptError, match="no interpreter"):
        await runner.run("evil", "scripts/payload.bin")


@pytest.mark.anyio
async def test_server_secrets_are_not_inherited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-should-not-leak")
    skill = tmp_path / "envcheck"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: envcheck\ndescription: d\n---\nbody\n")
    (skill / "scripts" / "dump.py").write_text(
        "import os, json; print(json.dumps(sorted(os.environ)))"
    )

    result = await ScriptRunner(SkillIndex([tmp_path])).run("envcheck", "scripts/dump.py")
    assert result.exit_code == 0
    assert "MY_API_KEY" not in json.loads(result.stdout)
    assert "SKILL_NAME" in json.loads(result.stdout)


# -------------------------------------------------------------------- running


def _make_skill(tmp_path: Path, name: str, script: str, code: str) -> Path:
    skill = tmp_path / name
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
    (skill / "scripts" / script).write_text(code)
    return tmp_path


# ------------------------------------------------- hangs: the API-call problem


@pytest.mark.anyio
async def test_output_survives_a_timeout(tmp_path: Path):
    """The whole point: a killed script still reports how far it got.

    Without this, a script that hangs on an API call returns an empty string and
    you cannot tell which endpoint it died on.
    """
    root = _make_skill(
        tmp_path,
        "hangs",
        "call.py",
        "import time\n"
        "print('step 1: auth ok', flush=True)\n"
        "print('step 2: GET /v1/orders', flush=True)\n"
        "time.sleep(60)\n",
    )
    result = await ScriptRunner(SkillIndex([root])).run(
        "hangs", "scripts/call.py", timeout=1.5, stall_timeout=0
    )
    assert result.status == "timeout"
    assert "step 1: auth ok" in result.stdout
    assert "step 2: GET /v1/orders" in result.stdout
    assert result.hint


@pytest.mark.anyio
async def test_stall_detection_kills_early(tmp_path: Path):
    """A silent script dies at stall_timeout, not at the much larger ceiling."""
    root = _make_skill(
        tmp_path,
        "stalls",
        "call.py",
        "import time\nprint('GET /v1/orders', flush=True)\ntime.sleep(60)\n",
    )
    result = await ScriptRunner(SkillIndex([root])).run(
        "stalls", "scripts/call.py", timeout=60, stall_timeout=1.0
    )
    assert result.status == "stalled"
    assert result.duration_ms < 10_000, "should not have waited for the 60s ceiling"
    assert result.silent_for_s >= 1.0
    assert "GET /v1/orders" in result.stdout
    assert "no output" in result.hint


@pytest.mark.anyio
async def test_progress_keeps_a_slow_script_alive(tmp_path: Path):
    """A script that reports progress is never mistaken for a hung one, even
    though each step takes longer than nothing."""
    root = _make_skill(
        tmp_path,
        "chatty-slow",
        "work.py",
        "import time\n"
        "for i in range(6):\n"
        "    print(f'page {i}', flush=True)\n"
        "    time.sleep(0.3)\n"
        "print('done', flush=True)\n",
    )
    result = await ScriptRunner(SkillIndex([root])).run(
        "chatty-slow", "scripts/work.py", timeout=30, stall_timeout=1.0
    )
    assert result.status == "ok"
    assert "done" in result.stdout


@pytest.mark.anyio
async def test_on_output_streams_lines_while_running(tmp_path: Path):
    root = _make_skill(
        tmp_path,
        "streamer",
        "work.py",
        "import time\n"
        "for i in range(3):\n    print(f'line {i}', flush=True)\n    time.sleep(0.1)\n",
    )
    seen: list[tuple[str, str]] = []

    async def collect(which: str, line: str) -> None:
        seen.append((which, line))

    result = await ScriptRunner(SkillIndex([root])).run(
        "streamer", "scripts/work.py", on_output=collect
    )
    assert result.status == "ok"
    assert [line for _, line in seen] == ["line 0", "line 1", "line 2"]


@pytest.mark.anyio
async def test_a_real_hanging_socket_is_caught(tmp_path: Path):
    """End-to-end against a socket that accepts and then never replies —
    the exact shape of an API call that hangs."""
    import socket as sock
    import threading

    listener = sock.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list = []

    def accept_and_go_quiet() -> None:
        conn, _ = listener.accept()
        accepted.append(conn)  # held open, never written to

    threading.Thread(target=accept_and_go_quiet, daemon=True).start()

    root = _make_skill(
        tmp_path,
        "realhang",
        "call.py",
        "import urllib.request\n"
        f"print('GET http://127.0.0.1:{port}/v1/orders', flush=True)\n"
        # No timeout= on purpose: this is the bug the skill docs warn about.
        f"urllib.request.urlopen('http://127.0.0.1:{port}/v1/orders')\n",
    )
    try:
        result = await ScriptRunner(SkillIndex([root])).run(
            "realhang", "scripts/call.py", timeout=60, stall_timeout=1.5
        )
    finally:
        listener.close()
        for conn in accepted:
            conn.close()

    assert result.status == "stalled"
    assert result.duration_ms < 15_000
    assert f"127.0.0.1:{port}/v1/orders" in result.stdout


@pytest.mark.anyio
async def test_proxy_and_tls_env_reach_the_script(tmp_path: Path, monkeypatch):
    """Regression: stripping these made every outbound call hang behind a proxy."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", ".corp.internal")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp.pem")
    monkeypatch.setenv("MY_API_KEY", "sk-should-not-leak")

    root = _make_skill(
        tmp_path, "envcheck2", "dump.py", "import os, json; print(json.dumps(dict(os.environ)))"
    )
    result = await ScriptRunner(SkillIndex([root])).run("envcheck2", "scripts/dump.py")
    env = json.loads(result.stdout)
    assert env["HTTPS_PROXY"] == "http://proxy.corp:3128"
    assert env["NO_PROXY"] == ".corp.internal"
    assert env["REQUESTS_CA_BUNDLE"] == "/etc/ssl/corp.pem"
    assert "MY_API_KEY" not in env  # still not leaking unrelated secrets


@pytest.mark.anyio
async def test_network_env_can_be_switched_off(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    root = _make_skill(
        tmp_path, "envcheck3", "dump.py", "import os, json; print(json.dumps(dict(os.environ)))"
    )
    runner = ScriptRunner(SkillIndex([root]), pass_network_env=False)
    result = await runner.run("envcheck3", "scripts/dump.py")
    assert "HTTPS_PROXY" not in json.loads(result.stdout)


@pytest.mark.anyio
async def test_per_run_env_carries_secrets_out_of_argv(tmp_path: Path):
    root = _make_skill(tmp_path, "tok", "show.py", "import os; print(os.environ['API_TOKEN'])")
    result = await ScriptRunner(SkillIndex([root])).run(
        "tok", "scripts/show.py", env={"API_TOKEN": "secret-value"}
    )
    assert result.stdout.strip() == "secret-value"


@pytest.mark.anyio
async def test_timeout_kills_the_process(tmp_path: Path):
    skill = tmp_path / "slow"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: slow\ndescription: d\n---\nbody\n")
    (skill / "scripts" / "sleep.py").write_text("import time; time.sleep(30)")

    result = await ScriptRunner(SkillIndex([tmp_path])).run(
        "slow", "scripts/sleep.py", timeout=0.5
    )
    assert result.timed_out is True
    assert result.duration_ms < 5000


@pytest.mark.anyio
async def test_output_is_capped_without_deadlocking(tmp_path: Path):
    skill = tmp_path / "chatty"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: chatty\ndescription: d\n---\nbody\n")
    (skill / "scripts" / "flood.py").write_text(
        "import sys\n"
        "for _ in range(20000): sys.stdout.write('x' * 100 + '\\n')\n"
        "sys.stderr.write('done\\n')\n"
    )

    runner = ScriptRunner(SkillIndex([tmp_path]), output_cap_bytes=4096)
    result = await runner.run("chatty", "scripts/flood.py", timeout=30)
    assert result.exit_code == 0, result.stderr
    assert result.truncated is True
    assert len(result.stdout) <= 4096


@pytest.mark.anyio
async def test_nonzero_exit_is_reported_not_raised(index: SkillIndex):
    result = await ScriptRunner(index).run("repo-digest", "scripts/digest.sh", ["/nonexistent"])
    assert result.exit_code == 2
    assert "not a git repository" in result.stderr


@pytest.mark.anyio
async def test_shell_skill_succeeds_on_a_repo_with_history(index: SkillIndex, tmp_path: Path):
    """Regression: `| head` under pipefail used to exit 141 (SIGPIPE) whenever
    the history was longer than the display limit, despite correct output."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    for i in range(45):  # more commits than the script's own -n 40 limit
        (repo / f"f{i}.txt").write_text(str(i))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", f"c{i}"], check=True, env=env)

    result = await ScriptRunner(index).run(
        "repo-digest", "scripts/digest.sh", [str(repo), "3650"], timeout=60
    )
    assert result.exit_code == 0, result.stderr
    assert "## churn" in result.stdout
    assert "## contributors" in result.stdout


@pytest.mark.anyio
async def test_shell_skill_handles_a_repo_with_no_commits(index: SkillIndex, tmp_path: Path):
    """Regression: `git log -1` on a fresh repo exits 128 and killed the script."""
    import subprocess

    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    result = await ScriptRunner(index).run("repo-digest", "scripts/digest.sh", [str(repo)])
    assert result.exit_code == 0, result.stderr
    assert "no commits yet" in result.stdout


# ------------------------------------------------------------------ MCP layer


@pytest.mark.anyio
async def test_tools_are_exposed(client: Client):
    async with client:
        names = {t.name for t in await client.list_tools()}
    assert names == {
        "list_skills",
        "load_skill",
        "read_skill_file",
        "run_skill_script",
        "skill_server_stats",
        "reload_skills",
    }


@pytest.mark.anyio
async def test_list_then_load_then_run(client: Client, tmp_path: Path):
    csv_file = tmp_path / "sample.csv"
    csv_file.write_text("id,city,score\n1,taipei,9.5\n2,osaka,8\n3,taipei,\n")

    async with client:
        listing = (await client.call_tool("list_skills", {"query": "csv"})).data
        assert listing["skills"][0]["name"] == "csv-profile"

        loaded = (await client.call_tool("load_skill", {"name": "csv-profile"})).data
        assert "# CSV profiling" in loaded["body"]

        section = (
            await client.call_tool("load_skill", {"name": "csv-profile", "section": "Usage"})
        ).data
        assert section["body"].startswith("## Usage")
        assert len(section["body"]) < len(loaded["body"])

        run = (
            await client.call_tool(
                "run_skill_script",
                {
                    "name": "csv-profile",
                    "script": "scripts/profile.py",
                    "args": [str(csv_file)],
                },
            )
        ).data
        assert run["exit_code"] == 0
        report = json.loads(run["stdout"])

    assert report["rows"] == 3
    by_name = {c["name"]: c for c in report["columns"]}
    assert by_name["id"]["inferred_type"] == "integer"
    assert by_name["city"]["distinct"] == 2
    assert by_name["score"]["nulls"] == 1


@pytest.mark.anyio
async def test_stdin_is_piped_through(client: Client):
    async with client:
        run = (
            await client.call_tool(
                "run_skill_script",
                {
                    "name": "text-stats",
                    "script": "scripts/wordcount.py",
                    "args": ["--top", "3"],
                    "stdin": "the quick brown fox. the quick dog runs. quick!",
                },
            )
        ).data
    assert run["exit_code"] == 0
    stats = json.loads(run["stdout"])
    assert stats["sentences"] == 3
    assert stats["top_terms"][0]["term"] == "quick"


@pytest.mark.anyio
async def test_reference_file_is_readable(client: Client):
    async with client:
        got = (
            await client.call_tool(
                "read_skill_file",
                {"name": "csv-profile", "path": "references/type-inference.md"},
            )
        ).data
    assert "Type inference rules" in got["content"]


@pytest.mark.anyio
async def test_traversal_through_the_tool_is_an_error(client: Client):
    from fastmcp.exceptions import ToolError

    async with client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "read_skill_file", {"name": "csv-profile", "path": "../../../etc/passwd"}
            )


@pytest.mark.anyio
async def test_resource_serves_the_body(client: Client):
    async with client:
        content = await client.read_resource("skill://text-stats")
    assert "# Text statistics" in content[0].text


@pytest.mark.anyio
async def test_stats_reports_latency(client: Client):
    async with client:
        await client.call_tool("list_skills", {})
        stats = (await client.call_tool("skill_server_stats", {})).data
    assert stats["index"]["skills"] >= 3
    assert stats["tools"]["list_skills"]["calls"] >= 1


# ------------------------------------------------ context budget (small models)


def test_budget_scales_with_the_context_window():
    from skill_server import shaping

    assert shaping.budget_bytes_for(30_000) < shaping.budget_bytes_for(128_000)
    # A quarter of 30k tokens, leaving room for the prompt, skill and reply.
    assert 20_000 < shaping.budget_bytes_for(30_000) < 30_000


def test_json_is_reduced_structurally_not_cut_in_half():
    """Byte-truncating JSON yields a fragment the model cannot parse; the whole
    point of structural reduction is that the result still loads."""
    from skill_server import shaping

    payload = json.dumps(
        {"total": 500, "data": [{"id": i, "name": f"row {i}", "amount": 1.5} for i in range(500)]}
    )
    shaped, info = shaping.shape(payload, budget=2000)

    assert info["shaped"] is True
    assert info["how"] == "json-structural"
    assert len(shaped.encode()) <= 2000
    parsed = json.loads(shaped)  # would raise on a byte-truncated payload
    assert parsed["total"] == 500, "the total must survive so counts stay answerable"
    assert len(parsed["data"]) < 500
    assert parsed["_truncated"]["of"] == 500


def test_small_output_is_passed_through_untouched():
    from skill_server import shaping

    text = json.dumps({"ok": True})
    shaped, info = shaping.shape(text, budget=10_000)
    assert shaped == text
    assert info["shaped"] is False


def test_non_json_keeps_head_and_tail():
    """Errors cluster at the end of a log, so head-only truncation loses them."""
    from skill_server import shaping

    text = "START\n" + ("filler line\n" * 5000) + "FATAL: the thing broke\n"
    shaped, info = shaping.shape(text, budget=1200)
    assert info["how"] == "text-head-tail"
    assert "START" in shaped
    assert "FATAL: the thing broke" in shaped
    assert "dropped from the middle" in shaped


def test_wide_rows_fall_back_to_an_outline_that_still_parses():
    from skill_server import shaping

    fat = {"data": [{f"field_{i}": "x" * 200 for i in range(50)} for _ in range(100)]}
    shaped, info = shaping.shape(json.dumps(fat), budget=800)
    assert info["how"] in ("json-structural", "json-outline-only")
    json.loads(shaped)


@pytest.mark.anyio
async def test_tool_shapes_output_to_the_context_budget(tmp_path: Path):
    """A 500-row REST response must not arrive as 59k tokens on a 30k model."""
    root = _make_skill(
        tmp_path,
        "big",
        "dump.py",
        "import json\n"
        "print(json.dumps({'total': 500, 'data': "
        "[{'id': i, 'order_no': f'SO-{i:06d}', 'status': 'pending', 'amount': 123.45} "
        "for i in range(500)]}))\n",
    )
    server = build_server([root], refresh_interval=0, context_tokens=30_000)
    async with Client(server) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "big", "script": "scripts/dump.py"}
            )
        ).data

    info = result["output"]
    assert info["shaped"] is True
    assert info["approx_tokens"] < 30_000 * 0.25
    assert info["original_approx_tokens"] > info["approx_tokens"] * 10
    assert json.loads(result["stdout"])["total"] == 500


@pytest.mark.anyio
async def test_scripts_are_told_the_budget(tmp_path: Path):
    """So they can page at the source instead of being reduced afterwards."""
    root = _make_skill(
        tmp_path, "budget", "show.py", "import os; print(os.environ['SKILL_OUTPUT_BUDGET_BYTES'])"
    )
    runner = ScriptRunner(SkillIndex([root]), output_budget_bytes=26_250)
    result = await runner.run("budget", "scripts/show.py")
    assert result.stdout.strip() == "26250"


# ------------------------------------------- async jobs (submit / poll / await)


@pytest.mark.anyio
async def test_heartbeat_saves_a_poller_from_stall_detection(tmp_path: Path):
    """A polling script MUST print while it waits.

    Without a heartbeat the runner cannot tell "waiting on a job" from "hung",
    and kills it at stall_timeout even though the caller asked for far longer.
    This test pins both halves of that contract.
    """
    silent = _make_skill(
        tmp_path / "a",
        "silent-poller",
        "poll.py",
        "import time\nprint('submitted: uuid-1', flush=True)\ntime.sleep(30)\n",
    )
    result = await ScriptRunner(SkillIndex([silent])).run(
        "silent-poller", "scripts/poll.py", timeout=60, stall_timeout=1.5
    )
    assert result.status == "stalled", "a silent poller is indistinguishable from a hang"

    beating = _make_skill(
        tmp_path / "b",
        "beating-poller",
        "poll.py",
        "import sys, time\n"
        "print('submitted: uuid-1', flush=True)\n"
        "for i in range(8):\n"
        "    time.sleep(0.4)\n"
        "    print(f'poll {i}: running', file=sys.stderr, flush=True)\n"
        "print('done', flush=True)\n",
    )
    result = await ScriptRunner(SkillIndex([beating])).run(
        "beating-poller", "scripts/poll.py", timeout=60, stall_timeout=1.5
    )
    assert result.status == "ok", "heartbeats must keep a legitimate poller alive"
    assert "done" in result.stdout


@pytest.mark.anyio
async def test_a_killed_submit_still_yields_the_job_id(tmp_path: Path):
    """The orphan-job guarantee.

    If a submit is killed after the backend created the job, the uuid must still
    come back — otherwise the job runs to completion with nobody to collect it,
    and the caller re-submits and creates a second one.
    """
    root = _make_skill(
        tmp_path,
        "submitter",
        "submit.py",
        "import json, time\n"
        "print(json.dumps({'key': 'uuid-7f3a', 'status': 'pending'}), flush=True)\n"
        "time.sleep(60)\n",  # e.g. it then tried to wait for the job
    )
    result = await ScriptRunner(SkillIndex([root])).run(
        "submitter", "scripts/submit.py", timeout=60, stall_timeout=1.0
    )
    assert result.status == "stalled"
    assert json.loads(result.stdout.strip())["key"] == "uuid-7f3a"


@pytest.mark.anyio
async def test_await_returns_bounded_and_not_finished_is_success(tmp_path: Path):
    """`finished: false` is a normal result, not an error: it is what keeps every
    individual call short regardless of how long the job takes."""
    import http.server
    import json as _json
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # always still running
            body = _json.dumps({"key": "k1", "status": "running"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await ScriptRunner(SkillIndex([SKILLS])).run(
            "async-job",
            "scripts/job.py",
            [
                "await",
                f"http://127.0.0.1:{port}/api/jobs",
                "--key",
                "k1",
                "--max-wait",
                "2",
                "--heartbeat",
                "0.5",
            ],
            timeout=30,
        )
    finally:
        server.shutdown()

    assert result.status == "ok", result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["finished"] is False
    # Points at fetch, not at another await: on a fire-and-forget API the job
    # finishes on its own, so re-waiting is the wrong next move.
    assert "fetch --key k1" in payload["next"]
    assert result.duration_ms < 15_000, "await must respect its own max-wait"


# --------------------------------------------- fire-and-forget: the key ledger














# ============ 執行政策：只宣告 timeout，不宣告「意義」 ======================


def _skill_with_frontmatter(root: Path, name: str, frontmatter: str, scripts: dict) -> Path:
    skill = root / name
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n{frontmatter}---\nbody\n")
    for filename, code in scripts.items():
        (skill / "scripts" / filename).write_text(code)
    return root


def test_execution_policy_is_parsed_per_script(tmp_path: Path):
    root = _skill_with_frontmatter(
        tmp_path,
        "modes",
        "execution:\n"
        "  default: {timeout: 60}\n"
        "  scripts/fire.py: {timeout: 15, description: 送出後立刻回 uuid}\n",
        {"fire.py": "print('x')", "wait.py": "print('x')"},
    )
    meta = SkillIndex([root]).get("modes")
    fire = meta.policy_for("scripts/fire.py")
    assert fire.timeout == 15
    assert fire.description == "送出後立刻回 uuid"
    assert meta.policy_for("scripts/wait.py").timeout == 60


@pytest.mark.anyio
async def test_output_is_returned_without_interpretation(tmp_path: Path):
    root = _skill_with_frontmatter(
        tmp_path,
        "verbatim",
        "",
        {
            "fire.py": "import json; print(json.dumps({'key':'7f3a-uuid','status':'accepted'}))",
            "wait.py": "import json; print(json.dumps({'rows':842}))",
        },
    )
    async with Client(build_server([root], refresh_interval=0)) as client:
        fired = (
            await client.call_tool(
                "run_skill_script", {"name": "verbatim", "script": "scripts/fire.py"}
            )
        ).data
        waited = (
            await client.call_tool(
                "run_skill_script", {"name": "verbatim", "script": "scripts/wait.py"}
            )
        ).data

    # 兩者都是 ok，且 stdout 原封不動 —— 服務沒有替它們貼標籤
    assert json.loads(fired["stdout"])["key"] == "7f3a-uuid"
    assert json.loads(waited["stdout"])["rows"] == 842
    assert "mode" not in fired and "mode" not in waited
    assert "key" not in fired, "服務不該把它猜到的東西提升成頂層欄位"


def test_server_never_guesses_a_job_key_from_ordinary_data():
    """自動猜 job key 會把一般查詢結果誤判成任務代號。

    這是移除它的原因：訂單的 id、使用者的 id 都會被當成 job handle，
    而一個錯的 handle 比沒有 handle 更糟 —— 它看起來是對的。
    """
    from skill_server import server as srv

    assert not hasattr(srv, "_extract_key"), "自動猜測已移除，不應復活"


@pytest.mark.anyio
async def test_short_ceiling_overrun_points_at_the_async_boundary(tmp_path: Path):
    """短 timeout 就是作者在說「這應該馬上回」。超時代表端點在回覆前就把工作做完了。

    這個診斷不需要 mode 旗標 —— timeout 的值本身已經表達了預期。
    """
    root = _skill_with_frontmatter(
        tmp_path,
        "slowfire",
        "execution:\n  scripts/fire.py: {timeout: 1, stall_timeout: 0}\n",
        {"fire.py": "import time; print('posting', flush=True); time.sleep(30)"},
    )
    async with Client(build_server([root], refresh_interval=0)) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "slowfire", "script": "scripts/fire.py"}
            )
        ).data
    assert result["status"] == "timeout"
    assert "async boundary is on the wrong side" in result["hint"]


def test_the_two_timeout_diagnoses_give_opposite_advice():
    """長短兩種 ceiling 超時，建議必須相反，否則會把人導到錯的方向。

    直接測 _diagnose，因為端對端要真的等滿 31 秒才能觸發長 ceiling 的分支。
    """
    from skill_server.runner import ScriptRunner, _Capture

    state = _Capture(cap=1000)
    state.last_line = "POST /api/jobs"

    short = ScriptRunner._diagnose("timeout", state, limit=15, stall=20)
    long = ScriptRunner._diagnose("timeout", state, limit=300, stall=20)

    # 短 ceiling：問題在端點，不要調大 timeout
    assert "async boundary is on the wrong side" in short
    assert "would hide that, not fix it" in short
    # 長 ceiling：真的慢，調大 timeout 才對
    assert "genuinely slow" in long
    assert "raise `timeout`" in long
    assert "async boundary" not in long

    # 卡住是第三種，跟兩者都不同
    stalled = ScriptRunner._diagnose("stalled", state, limit=300, stall=20)
    assert "no timeout set on it" in stalled
    assert "POST /api/jobs" in stalled


@pytest.mark.anyio
async def test_declared_timeout_applies_without_the_caller_knowing(tmp_path: Path):
    root = _skill_with_frontmatter(
        tmp_path,
        "declared",
        "execution:\n  scripts/slow.py: {timeout: 1, stall_timeout: 0}\n",
        {"slow.py": "import time; print('go', flush=True); time.sleep(30)"},
    )
    async with Client(build_server([root], refresh_interval=0)) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "declared", "script": "scripts/slow.py"}
            )
        ).data
    assert result["status"] == "timeout"
    assert result["duration_ms"] < 10_000


def test_timeout_is_visible_in_the_catalog(tmp_path: Path):
    """模型看得到每支 script 的 ceiling 與作者的說明。"""
    root = _skill_with_frontmatter(
        tmp_path,
        "shown",
        "execution:\n  scripts/fire.py: {timeout: 15, description: 送出後回 uuid}\n",
        {"fire.py": "print(1)"},
    )
    card = SkillIndex([root]).catalog()[0]
    assert card["scripts"]["scripts/fire.py"] == {"timeout": 15.0, "about": "送出後回 uuid"}


# ================================ scenario 2: hooks =========================


def _hooked_skill(root: Path, name: str, pre: str | None, post: str | None, script: str) -> Path:
    skill = root / name
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
    (skill / "scripts" / "run.py").write_text(script)
    if pre or post:
        (skill / "hooks").mkdir(exist_ok=True)
    if pre:
        (skill / "hooks" / "pre.py").write_text(pre)
    if post:
        (skill / "hooks" / "post.py").write_text(post)
    return root


@pytest.mark.anyio
async def test_pre_hook_can_deny_and_the_script_never_runs(tmp_path: Path):
    marker = tmp_path / "ran.txt"
    root = _hooked_skill(
        tmp_path,
        "gated",
        pre='import json,sys; print(json.dumps({"reason":"not allowed here"})); sys.exit(1)',
        post=None,
        script=f"open({str(marker)!r}, 'w').write('ran')",
    )
    from fastmcp.exceptions import ToolError

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError, match="not allowed here"):
            await client.call_tool(
                "run_skill_script", {"name": "gated", "script": "scripts/run.py"}
            )
    assert not marker.exists(), "a denied call must not execute the script"


@pytest.mark.anyio
async def test_pre_hook_injects_env_and_rewrites_args(tmp_path: Path):
    root = _hooked_skill(
        tmp_path,
        "inject",
        pre='import json; print(json.dumps({"env":{"INJECTED":"yes"},'
            '"args":["rewritten"],"note":"touched"}))',
        post=None,
        script="import os,sys; print(os.environ.get('INJECTED'), sys.argv[1:])",
    )
    async with Client(build_server([root], refresh_interval=0)) as client:
        result = (
            await client.call_tool(
                "run_skill_script",
                {"name": "inject", "script": "scripts/run.py", "args": ["original"]},
            )
        ).data
    assert "yes ['rewritten']" in result["stdout"]
    assert result["hook_notes"] == ["skill: touched"]


@pytest.mark.anyio
async def test_post_hook_can_rewrite_and_can_reject(tmp_path: Path):
    root = _hooked_skill(
        tmp_path,
        "shaped",
        pre=None,
        post='import json,sys\n'
             'r = json.load(sys.stdin)["result"]\n'
             'if "secret" in r.get("stdout", ""):\n'
             '    print(json.dumps({"reason": "output contained a secret"})); sys.exit(1)\n'
             'r["reviewed"] = True\n'
             'print(json.dumps({"result": r}))',
        script="import sys; print(sys.argv[1] if len(sys.argv)>1 else 'clean')",
    )
    from fastmcp.exceptions import ToolError

    async with Client(build_server([root], refresh_interval=0)) as client:
        ok = (
            await client.call_tool(
                "run_skill_script", {"name": "shaped", "script": "scripts/run.py"}
            )
        ).data
        assert ok["reviewed"] is True

        with pytest.raises(ToolError, match="contained a secret"):
            await client.call_tool(
                "run_skill_script",
                {"name": "shaped", "script": "scripts/run.py", "args": ["secret"]},
            )


@pytest.mark.anyio
async def test_global_hooks_apply_to_every_skill(tmp_path: Path):
    root = _make_skill(tmp_path / "skills", "plain", "run.py", "print('hello')")
    hooks_dir = tmp_path / "globalhooks"
    hooks_dir.mkdir()
    (hooks_dir / "pre.py").write_text('import json; print(json.dumps({"note":"org policy ok"}))')
    (hooks_dir / "post.py").write_text(
        'import json,sys\n'
        'r = json.load(sys.stdin)["result"]; r["org_audited"] = True\n'
        'print(json.dumps({"result": r}))'
    )
    server = build_server([root], refresh_interval=0, global_hooks_dir=hooks_dir)
    async with Client(server) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "plain", "script": "scripts/run.py"}
            )
        ).data
    assert result["org_audited"] is True
    assert result["hook_notes"] == ["global: org policy ok"]


@pytest.mark.anyio
async def test_skills_without_hooks_pay_nothing(tmp_path: Path):
    """The hook path must not cost anything for the skills that do not use it."""
    root = _make_skill(tmp_path, "nohooks", "run.py", "print('fast')")
    index = SkillIndex([root])
    assert index.get("nohooks").hooks == {}
    async with Client(build_server([root], refresh_interval=0)) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "nohooks", "script": "scripts/run.py"}
            )
        ).data
    assert result["status"] == "ok"
    assert "hook_notes" not in result


# ========================= scenario 3: runtime skill loading =================


@pytest.mark.anyio
async def test_skills_load_at_runtime_without_a_restart(tmp_path: Path):
    root = tmp_path / "skills"
    _make_skill(root, "first", "run.py", "print(1)")
    server = build_server([root], refresh_interval=0)  # polling off: reload only

    async with Client(server) as client:
        before = (await client.call_tool("list_skills", {})).data
        assert {s["name"] for s in before["skills"]} == {"first"}

        _make_skill(root, "second", "run.py", "print(2)")
        reload_result = (await client.call_tool("reload_skills", {})).data
        assert reload_result["added"] == ["second"]

        after = (await client.call_tool("list_skills", {})).data
        assert {s["name"] for s in after["skills"]} == {"first", "second"}

        # ... and the new skill is immediately runnable, not just listed
        run = (
            await client.call_tool(
                "run_skill_script", {"name": "second", "script": "scripts/run.py"}
            )
        ).data
        assert run["stdout"].strip() == "2"

        import shutil

        shutil.rmtree(root / "second")
        assert (await client.call_tool("reload_skills", {})).data["removed"] == ["second"]


@pytest.mark.anyio
async def test_edited_policy_and_new_hooks_take_effect_on_reload(tmp_path: Path):
    """Hot reload must cover behaviour, not just text: a policy change or a
    newly added hook has to apply without a restart."""
    root = tmp_path / "skills"
    skill = root / "evolving"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: evolving\ndescription: d\n---\nbody\n")
    (skill / "scripts" / "run.py").write_text("print('v1')")

    server = build_server([root], refresh_interval=0)
    async with Client(server) as client:
        first = (
            await client.call_tool(
                "run_skill_script", {"name": "evolving", "script": "scripts/run.py"}
            )
        ).data
        assert "hook_notes" not in first
        assert SkillIndex([root]).get("evolving").policy_for("scripts/run.py").timeout is None

        (skill / "SKILL.md").write_text(
            "---\nname: evolving\ndescription: d\n"
            "execution:\n  scripts/run.py: {timeout: 11}\n---\nbody\n"
        )
        (skill / "hooks").mkdir()
        (skill / "hooks" / "pre.py").write_text(
            'import json; print(json.dumps({"note":"added later"}))'
        )
        (skill / "scripts" / "run.py").write_text("import json; print(json.dumps({'key':'k9'}))")

        assert (await client.call_tool("reload_skills", {})).data["changed"] is True
        second = (
            await client.call_tool(
                "run_skill_script", {"name": "evolving", "script": "scripts/run.py"}
            )
        ).data

    assert json.loads(second["stdout"])["key"] == "k9"
    assert second["hook_notes"] == ["skill: added later"], "new hook must apply too"
    # policy 變更也要免重啟生效
    assert SkillIndex([root]).get("evolving").policy_for("scripts/run.py").timeout == 11


# ================================ CLI wiring ================================


def test_every_cli_flag_reaches_build_server():
    """Regression: --state-dir and --hooks-dir were defined in build_server and
    referenced in main(), but never registered on the parser, so the CLI raised
    AttributeError on startup. Every test used build_server directly and missed
    it. This asserts the parser and main() agree."""
    import inspect
    import re

    from skill_server.server import main, parse_args

    args = parse_args([])
    referenced = set(re.findall(r"\bargs\.([A-Za-z_]\w*)", inspect.getsource(main)))
    missing = sorted(name for name in referenced if not hasattr(args, name))
    assert not missing, f"main() reads args.{missing} but the parser never defines them"


def test_cli_parses_the_documented_flags():
    from skill_server.server import parse_args

    args = parse_args(
        [
            "--skills", "/tmp/a",
            "--port", "9001",
            "--hooks-dir", "/tmp/hooks",
            "--context-tokens", "30000",
            "--script-stall-timeout", "10",
            "--no-network-env",
        ]
    )
    assert args.port == 9001
    assert str(args.hooks_dir) == "/tmp/hooks"
    assert args.context_tokens == 30000
    assert args.script_stall_timeout == 10
    assert args.no_network_env is True


# ===================== 安全：實際攻擊的迴歸測試 =============================
# 以下每一項都對應一個曾經真實可行的攻擊。


@pytest.mark.anyio
async def test_hook_symlinked_outside_the_bundle_is_refused(tmp_path: Path):
    """曾經可行：hooks/pre.py 指向外部的 symlink 會被執行。

    scripts 走 resolve_file() 有 jail，hooks 走的是另一條路徑，當時完全沒檢查。
    hook 比 script 更危險，因為它在每次呼叫時都執行。
    """
    outside = tmp_path / "outside.py"
    outside.write_text('import json; print(json.dumps({"note": "PWNED"}))')

    root = tmp_path / "skills"
    skill = root / "hooked"
    (skill / "scripts").mkdir(parents=True)
    (skill / "hooks").mkdir()
    (skill / "SKILL.md").write_text("---\nname: hooked\ndescription: d\n---\nb\n")
    (skill / "scripts" / "run.py").write_text("print('ok')")
    (skill / "hooks" / "pre.py").symlink_to(outside)

    from fastmcp.exceptions import ToolError

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError, match="resolves outside its skill directory") as caught:
            await client.call_tool(
                "run_skill_script", {"name": "hooked", "script": "scripts/run.py"}
            )

    # RFC-101: the message that reaches the caller carries no absolute path.
    # It used to name both the jail and the resolved target, which told an
    # attacker exactly where the bundle lives on disk and confirmed whether a
    # probed path existed. That detail now goes to the log instead.
    message = str(caught.value)
    assert str(tmp_path) not in message
    assert str(outside) not in message


@pytest.mark.parametrize(
    "variable",
    ["PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "BASH_ENV",
     "NODE_OPTIONS", "SKILL_NAME", "SKILL_DIR"],
)
@pytest.mark.anyio
async def test_caller_cannot_set_execution_changing_env(tmp_path: Path, variable: str):
    """曾經可行：env={"PATH": "/tmp/fake"} 讓 shell script 執行任意程式。

    呼叫端是 LLM，可能被它讀到的任何文件說服，所以這等於 RCE 而不是設定選項。
    SKILL_* 則是不能讓呼叫端偽造身分或改寫 state 目錄指向。
    """
    root = _make_skill(tmp_path, "envtest", "run.py", "print('x')")
    runner = ScriptRunner(SkillIndex([root]))
    with pytest.raises(ScriptError, match="cannot be set by the caller"):
        await runner.run("envtest", "scripts/run.py", env={variable: "/tmp/attacker"})


@pytest.mark.anyio
async def test_caller_can_still_pass_data_env(tmp_path: Path):
    """封鎖清單不能誤傷正常用途：傳 token、base URL 仍要可行。"""
    root = _make_skill(
        tmp_path, "envok", "run.py",
        "import os; print(os.environ['API_TOKEN'], os.environ['API_BASE'])",
    )
    result = await ScriptRunner(SkillIndex([root])).run(
        "envok", "scripts/run.py",
        env={"API_TOKEN": "secret", "API_BASE": "http://internal"},
    )
    assert result.stdout.strip() == "secret http://internal"


@pytest.mark.anyio
async def test_stdin_is_bounded(tmp_path: Path):
    """曾經可行：20 MB stdin 全部進記憶體。"""
    root = _make_skill(tmp_path, "big", "run.py", "import sys; print(len(sys.stdin.read()))")
    runner = ScriptRunner(SkillIndex([root]))
    with pytest.raises(ScriptError, match="over the"):
        await runner.run("big", "scripts/run.py", stdin="A" * (5 * 1024 * 1024))
    ok = await runner.run("big", "scripts/run.py", stdin="A" * 1000)
    assert ok.stdout.strip() == "1000"


@pytest.mark.anyio
async def test_reference_files_respect_the_context_budget(tmp_path: Path):
    """曾經可行：400 KB 參考檔（約 114k tokens）直接回給 30k 的模型，
    而且外觀是「讀取成功」不是錯誤。"""
    root = tmp_path / "skills"
    skill = root / "docs"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: docs\ndescription: d\n---\nb\n")
    (skill / "references" / "big.txt").write_text("x" * 400_000)

    server = build_server([root], refresh_interval=0, context_tokens=30_000)
    async with Client(server) as client:
        got = (
            await client.call_tool(
                "read_skill_file",
                {"name": "docs", "path": "references/big.txt", "max_bytes": 400_000},
            )
        ).data
    assert got["output"]["shaped"] is True
    assert got["output"]["approx_tokens"] < 30_000 * 0.25
    assert got["truncated"] is True


@pytest.mark.anyio
async def test_oversized_skill_body_respects_the_budget(tmp_path: Path):
    """同樣的缺口也存在於 load_skill：一份超大 SKILL.md 也會炸掉 context。"""
    root = tmp_path / "skills"
    skill = root / "huge"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: huge\ndescription: d\n---\n" + ("內容很長。" * 40_000)
    )
    async with Client(build_server([root], refresh_interval=0, context_tokens=30_000)) as client:
        loaded = (await client.call_tool("load_skill", {"name": "huge"})).data
    assert loaded["output"]["shaped"] is True
    assert loaded["output"]["approx_tokens"] < 30_000 * 0.25


# =============== Claude Code 規格對齊 =======================================


@pytest.mark.parametrize(
    "name,ok",
    [
        ("order-lookup", True), ("csv-profile", True), ("a1", True),
        ("../../etc", False),        # 路徑穿越（曾經可讓 state 目錄逃逸）
        ("Order-Lookup", False),     # Claude Code 要求小寫
        ("order_lookup", False),     # 底線不合規
        ("訂單查詢", False),          # 非 ASCII 不合規
        ("order lookup", False),     # 空白不合規
        ("-lead", False), ("trail-", False),
        ("x" * 65, False),           # 超過 64 字元
    ],
)
def test_skill_name_follows_claude_code_rules(tmp_path: Path, name: str, ok: bool):
    """Claude Code 的命名規則：小寫字母、數字、連字號，最多 64 字元。

    這裡是強制而非建議，因為 name 同時是查詢鍵，也（經過淨化後）是目錄名稱。
    """
    root = tmp_path / "skills"
    skill = root / "somedir"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nb\n")

    index = SkillIndex([root])
    names = {card["name"] for card in index.catalog(limit=50)}
    if ok:
        assert name in names
        assert index.stats()["rejected"] == []
    else:
        assert name not in names
        # 不能無聲消失：必須說得出被拒絕的原因
        assert index.stats()["rejected"], "被拒絕的 skill 必須出現在 stats 裡"
        assert "invalid" in index.stats()["rejected"][0]["reason"]


def test_oversized_description_is_rejected_with_a_reason(tmp_path: Path):
    """description 是唯一每次對話都會載入的文字，過長等於對每個使用者課稅。"""
    root = tmp_path / "skills"
    skill = root / "wordy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: wordy\ndescription: {'長' * 1100}\n---\nb\n"
    )
    index = SkillIndex([root])
    assert len(index) == 0
    assert "description" in index.stats()["rejected"][0]["reason"]


def test_allowed_tools_is_parsed_and_passed_through(tmp_path: Path):
    """Claude Code 的 allowed-tools。本服務不強制執行（它不擁有客戶端的工具清單），
    但原樣透傳，讓客戶端可以自行強制。"""
    root = tmp_path / "skills"
    skill = root / "restricted"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: restricted\ndescription: d\n"
        "allowed-tools: [Read, Grep]\nlicense: MIT\n---\nb\n"
    )
    meta = SkillIndex([root]).get("restricted")
    assert meta.allowed_tools == ("Read", "Grep")
    assert meta.license == "MIT"
    assert SkillIndex([root]).catalog()[0]["allowed_tools"] == ["Read", "Grep"]


def test_hooks_are_not_listed_as_readable_content(tmp_path: Path):
    """hooks 是機制不是內容，不該出現在給模型看的檔案清單裡。"""
    root = tmp_path / "skills"
    skill = root / "hooked"
    (skill / "hooks").mkdir(parents=True)
    (skill / "references").mkdir()
    (skill / "SKILL.md").write_text("---\nname: hooked\ndescription: d\n---\nb\n")
    (skill / "hooks" / "pre.py").write_text("print()")
    (skill / "references" / "a.md").write_text("x")

    meta = SkillIndex([root]).get("hooked")
    assert meta.files == ("references/a.md",)
    assert meta.hooks == {"pre": "hooks/pre.py"}


@pytest.mark.anyio
async def test_timeout_holds_when_a_grandchild_keeps_the_pipe_open(tmp_path: Path):
    """曾經違約：script 自己 0.05 秒就結束，但它 spawn 的孫行程繼承了 stdout，
    pipe 因此沒有 EOF。runner 固定等 5 秒讀完 pipe，導致 timeout=2s 的呼叫
    實際跑了 5 秒。

    timeout 是對呼叫端的承諾，不能被孫行程延長。
    """
    skill = tmp_path / "forker"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: forker\ndescription: d\n---\nb\n")
    (skill / "scripts" / "f.py").write_text(
        "import subprocess, sys\n"
        # 沒有導向 stdout：孫行程會一直握著我們的 pipe
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
        "print('spawned', flush=True)\n"
    )

    started = time.monotonic()
    result = await ScriptRunner(SkillIndex([tmp_path])).run(
        "forker", "scripts/f.py", timeout=2, stall_timeout=0
    )
    elapsed = time.monotonic() - started

    assert elapsed < 3.5, f"timeout=2s 卻花了 {elapsed:.2f}s：孫行程把 pipe 撐開了"
    assert "spawned" in result.stdout, "已印出的內容仍要保留"


@pytest.mark.anyio
async def test_a_detached_background_child_survives_and_returns_immediately(tmp_path: Path):
    """把孫行程的輸出導開，script 就能立刻返回而孫行程繼續跑完。

    這是「在 script 裡開背景工作」唯一可靠的寫法。不導開的話 runner 會被
    pipe 卡住（見上一個測試）。
    """
    skill = tmp_path / "detached"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: detached\ndescription: d\n---\nb\n")
    marker = tmp_path / "child-done.txt"
    (skill / "scripts" / "f.py").write_text(
        "import subprocess, sys\n"
        "child = ('import time, pathlib, sys; time.sleep(1.5); '\n"
        "         'pathlib.Path(sys.argv[1]).write_text(\"x\")')\n"
        "subprocess.Popen([sys.executable, '-c', child, sys.argv[1]],\n"
        "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print('dispatched', flush=True)\n"
    )

    result = await ScriptRunner(SkillIndex([tmp_path])).run(
        "detached", "scripts/f.py", [str(marker)], timeout=10
    )
    assert result.status == "ok"
    assert result.duration_ms < 1000, "導開輸出後應該立刻返回"
    assert not marker.exists(), "此刻孫行程還在跑"

    await asyncio.sleep(2.5)
    assert marker.exists(), "孫行程必須活過 script 的結束"


# ======================== 維運面：健康檢查、指標、關機 ======================


@pytest.mark.anyio
async def test_health_and_ready_endpoints_answer_plain_http(tmp_path: Path):
    """k8s 的 probe 不會說 JSON-RPC，維運也需要能直接 curl。

    迴歸重點：GET /mcp 回 405（MCP 端點只收 POST），所以把 readinessProbe
    指向 /mcp 會讓 pod 永遠 NotReady。
    """
    from starlette.testclient import TestClient

    root = _make_skill(tmp_path, "alive", "run.py", "print(1)")
    app = build_server([root], refresh_interval=0).http_app()

    with TestClient(app) as client:
        # 實際狀態碼視 Accept header 而定（curl 得到 405，這裡 406），
        # 但兩者都不是 2xx，httpGet probe 一律判定失敗。
        assert not 200 <= client.get("/mcp").status_code < 300, "probe 不能指向 /mcp"

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["skills"] == 1


@pytest.mark.anyio
async def test_ready_reports_503_when_no_skills_loaded(tmp_path: Path):
    """空索引通常代表 --skills 路徑設錯。送流量進去比不送更糟：
    模型會拿到空目錄，然後認定這些工具不存在。"""
    from starlette.testclient import TestClient

    empty = tmp_path / "nothing"
    empty.mkdir()
    app = build_server([empty], refresh_interval=0).http_app()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200, "行程活著就該回 200"
        ready = client.get("/ready")
        assert ready.status_code == 503, "沒有 skill 就不該接流量"
        assert ready.json()["skills"] == 0


@pytest.mark.anyio
async def test_metrics_endpoint_is_prometheus_readable(tmp_path: Path):
    from starlette.testclient import TestClient

    root = _make_skill(tmp_path, "counted", "run.py", "print(1)")
    app = build_server([root], refresh_interval=0).http_app()

    with TestClient(app) as client:
        body = client.get("/metrics").text

    assert "# TYPE skill_mcp_skills gauge" in body
    assert "skill_mcp_skills 1" in body
    for metric in ("skill_mcp_scripts_total", "skill_mcp_scripts_timeout_total",
                   "skill_mcp_scripts_stalled_total"):
        assert metric in body


@pytest.mark.anyio
async def test_drain_waits_for_in_flight_scripts(tmp_path: Path):
    """滾動更新時，正在跑的 script 要有機會跑完。

    否則一個已經送達 API、還沒印出 uuid 的 submit 會被 SIGKILL：
    工作在後端跑到完成，handle 卻永遠消失。
    """
    root = _make_skill(
        tmp_path, "slowish", "run.py",
        "import time; time.sleep(0.8); print('finished', flush=True)",
    )
    runner = ScriptRunner(SkillIndex([root]))

    task = asyncio.create_task(runner.run("slowish", "scripts/run.py", timeout=10))
    await asyncio.sleep(0.2)
    assert runner.in_flight == 1

    started = time.monotonic()
    remaining = await runner.drain(timeout=5)
    assert remaining == 0, "drain 應該等到跑完"
    assert time.monotonic() - started >= 0.3

    result = await task
    assert "finished" in result.stdout


@pytest.mark.anyio
async def test_drain_is_bounded_and_reports_abandoned(tmp_path: Path):
    """k8s 只給 terminationGracePeriodSeconds，drain 不能無限等。"""
    root = _make_skill(tmp_path, "verylong", "run.py", "import time; time.sleep(30)")
    runner = ScriptRunner(SkillIndex([root]))

    task = asyncio.create_task(runner.run("verylong", "scripts/run.py", timeout=60))
    await asyncio.sleep(0.2)

    started = time.monotonic()
    remaining = await runner.drain(timeout=0.5)
    elapsed = time.monotonic() - started

    assert remaining == 1, "應如實回報還有幾支沒跑完"
    assert elapsed < 2, "不能超出給定的 grace"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@pytest.mark.anyio
async def test_catalog_respects_the_context_budget(tmp_path: Path):
    """曾經的缺口：500 個 skill 的目錄約 23k tokens，等於 30k 視窗的 78%,
    而 list_skills 是唯一沒有套預算的工具。"""
    root = tmp_path / "many"
    for i in range(300):
        skill = root / f"skill-{i:04d}"
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: skill-{i:04d}\ndescription: 這是第 {i} 個技能，"
            f"用於處理某類內部業務流程與資料查詢。\n---\n內文\n"
        )

    async with Client(build_server([root], refresh_interval=0, context_tokens=30_000)) as client:
        result = (await client.call_tool("list_skills", {"limit": 500})).data

    payload = json.dumps(result, ensure_ascii=False)
    assert result["total"] == 300, "總數要如實回報"
    assert result["count"] < 300, "不可能全部塞進預算"
    assert len(payload) // 3 < 30_000 * 0.2
    # 放不下時改為領域總覽，而不是字母序截斷
    assert result["view"] == "overview"
    assert "facets" in result and result["facets"]
    assert "tags=" in result["hint"] or "query=" in result["hint"]


# ==================== 無狀態：唯讀根檔案系統下必須能跑 ======================


@pytest.mark.anyio
async def test_no_state_dir_by_default(tmp_path: Path):
    """服務不提供任何可寫目錄，也不寫任何檔案。

    迴歸重點：曾經會建立 ~/.skill-mcp/state，在 --read-only 的容器裡
    讓**每一支** script 都失敗。整個功能已移除，不是改預設值——
    所有狀態都該留在後端 API。
    """
    root = _make_skill(
        tmp_path, "stateless", "run.py",
        "import os; print(os.environ.get('SKILL_STATE_DIR', 'NONE'))",
    )
    async with Client(build_server([root], refresh_interval=0)) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "stateless", "script": "scripts/run.py"}
            )
        ).data
    assert result["stdout"].strip() == "NONE"
    assert result["status"] == "ok"





@pytest.mark.anyio
async def test_runner_returns_as_soon_as_the_process_exits(tmp_path: Path):
    """呼叫的耗時應該貼著 script 自己的執行時間，沒有額外等待。

    注意語意：等的是「行程結束」，不是「stdout 關閉」——script 印完後若還留著
    做別的事，整個呼叫都會被拖住。
    """
    root = tmp_path / "skills"
    skill = root / "quick"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: quick\ndescription: d\n---\nb\n")
    (skill / "scripts" / "instant.sh").write_text('#!/bin/bash\necho \'{"key":"abc"}\'\n')
    # 印完關閉 stdout，但行程繼續活著
    (skill / "scripts" / "lingers.sh").write_text(
        '#!/bin/bash\necho \'{"key":"abc"}\'\nexec 1>&-\nsleep 1\n'
    )

    runner = ScriptRunner(SkillIndex([root]))
    instant = await runner.run("quick", "scripts/instant.sh", timeout=20)
    lingers = await runner.run("quick", "scripts/lingers.sh", timeout=20)

    assert instant.status == "ok"
    assert instant.duration_ms < 500, "印完就 exit 的 script 不該有額外延遲"
    assert '"key":"abc"' in instant.stdout.replace(" ", "")

    # 關閉 stdout 不會讓呼叫提早返回：runner 等的是行程
    assert lingers.duration_ms >= 900, "行程還活著就必須繼續等"


@pytest.mark.anyio
async def test_api_call_skill_is_the_fast_path(tmp_path: Path):
    """bash + curl 的 api-call 應該遠快於 Python 腳本。

    在資源受限的環境下，直譯器啟動成本會主導整個呼叫。
    """
    import http.server
    import json as _json
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps({"order_no": "SO-001", "status": "shipped"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        runner = ScriptRunner(SkillIndex([SKILLS]))
        result = await runner.run(
            "api-call", "scripts/call.sh",
            ["GET", f"http://127.0.0.1:{port}/orders/SO-001"], timeout=30,
        )
    finally:
        server.shutdown()

    assert result.status == "ok", result.stderr
    assert json.loads(result.stdout)["order_no"] == "SO-001", "回應要原樣輸出"
    assert "GET http://127.0.0.1" in result.stderr, "進度要走 stderr"


@pytest.mark.anyio
async def test_api_call_reports_http_errors_without_raising(tmp_path: Path):
    """非 2xx 要回報成資料，讓模型看得到狀態碼。"""
    result = await ScriptRunner(SkillIndex([SKILLS])).run(
        "api-call", "scripts/call.sh", ["GET", "http://127.0.0.1:1/nope"], timeout=30
    )
    assert result.status == "failed"
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "unreachable"


def test_no_writable_state_api_exists_at_all():
    """「不寫任何檔案」是契約，不是預設值。

    這裡檢查的是整個功能不存在——不是「預設關閉」。任何人想加回一個可寫
    目錄，都會撞到這條測試，必須先想清楚它在唯讀容器裡怎麼活。
    """
    import inspect

    from skill_server import runner as runner_mod
    from skill_server import server as server_mod

    source = inspect.getsource(runner_mod) + inspect.getsource(server_mod)
    for banned in ("SKILL_STATE_DIR", "state_root", "state_dir"):
        assert banned not in source, f"{banned} 應該已完全移除"

    signature = inspect.signature(ScriptRunner.__init__)
    assert "state_root" not in signature.parameters
    assert "--state-dir" not in inspect.getsource(server_mod)


@pytest.mark.anyio
async def test_server_creates_no_files_while_serving(tmp_path: Path):
    """端對端：跑完一輪工具呼叫後，磁碟上不應該多出任何東西。

    這是唯讀根檔案系統能成立的實際保證。
    """
    root = tmp_path / "skills"
    _make_skill(root, "quiet", "run.py", "print('done')")

    def snapshot() -> set[Path]:
        return set(tmp_path.rglob("*"))

    before = snapshot()
    async with Client(build_server([root], refresh_interval=0)) as client:
        await client.call_tool("list_skills", {})
        await client.call_tool("load_skill", {"name": "quiet"})
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "quiet", "script": "scripts/run.py"}
            )
        ).data
    assert result["status"] == "ok"

    created = snapshot() - before
    assert not created, f"服務不該建立任何檔案，卻多出：{created}"


# =========================== 規範符合性（RFC-SKILL-1）=========================


def _run_validator(target: str, extra: list[str] | None = None) -> dict:
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "spec.validate", target, "--format=json", *(extra or [])],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    return json.loads(proc.stdout)


def test_all_skills_pass_spec_l1():
    """本專案的 Skill 必須符合自己發布的規範。

    規範若不能約束提出它的專案，就沒有理由要求其他團隊遵守。
    """
    report = _run_validator("skills/")
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    assert not errors, "違反 L1：" + json.dumps(errors, ensure_ascii=False, indent=2)
    assert report["passed"] is True


def test_validator_detects_known_violations(tmp_path: Path):
    """只會通過的驗證器沒有價值。

    每個案例都對應附錄 A 的一則事故。
    """
    cases = {
        "VAL-010": ("Bad_Name", "描述夠長可以通過長度檢查的測試用文字。", None),
        "VAL-012": ("ok-name", None, None),
        "VAL-013": ("ok-name", "長" * 1100, None),
        "VAL-024": ("ok-name", "描述夠長可以通過長度檢查的測試用文字。",
                    "execution:\n  scripts/a.sh:\n    mode: background\n"),
    }
    for expected, (name, desc, extra) in cases.items():
        root = tmp_path / expected
        skill = root / "s"
        (skill / "scripts").mkdir(parents=True)
        (skill / "scripts" / "a.sh").write_text("#!/bin/bash\nset -e\necho hi\n")
        front = f"name: {name}\n"
        if desc:
            front += f"description: {desc}\n"
        if extra:
            front += extra
        (skill / "SKILL.md").write_text(f"---\n{front}---\n內文\n")

        report = _run_validator(str(root))
        rules = {f["rule"] for f in report["findings"]}
        assert expected in rules, f"未偵測到 {expected}，只找到 {rules}"
        assert report["passed"] is False


def test_validator_detects_security_violations(tmp_path: Path):
    """安全規則必須真的抓得到 —— 對應附錄 A.1 的實際攻擊。"""
    skill = tmp_path / "risky"
    (skill / "scripts").mkdir(parents=True)
    (skill / "references").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: risky\ndescription: 描述夠長可以通過長度檢查的測試用文字。\n---\n內文\n"
    )
    (skill / "scripts" / "fetch.py").write_text('import requests\nrequests.get("http://x")\n')
    (skill / "references" / "leak.txt").symlink_to("/etc/hosts")
    (skill / "helper.py").write_text("print(1)\n")

    report = _run_validator(str(tmp_path))
    rules = {f["rule"] for f in report["findings"]}
    assert {"SEC-001", "SEC-002", "SEC-010"} <= rules, f"只抓到 {rules}"


def test_suppression_requires_a_reason(tmp_path: Path):
    """RFC-055：豁免必須附理由，而且必須在報告中可見。"""
    skill = tmp_path / "suppressed"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: suppressed\ndescription: 描述夠長可以通過長度檢查的測試用文字。\n---\n內文\n"
    )
    (skill / "scripts" / "a.sh").write_text(
        "#!/bin/bash\n# spec:allow LINT-020 需自行處理離開碼\nset -uo pipefail\ncurl --max-time 5 http://x\n"
    )
    report = _run_validator(str(tmp_path))
    findings = {f["rule"]: f["message"] for f in report["findings"]}
    assert "LINT-030" in findings, "豁免必須產生可見的紀錄"
    assert "需自行處理離開碼" in findings["LINT-030"], "理由必須出現在報告中"
    assert "LINT-020" not in findings


def test_validation_report_matches_schema():
    """報告是 CI 的介面。格式錯誤會讓自動化靜默失效。"""
    report = _run_validator("skills/")
    schema = json.loads(
        (Path(__file__).resolve().parent.parent
         / "spec/schemas/validation-report.schema.json").read_text()
    )
    for key in schema["required"]:
        assert key in report, f"報告缺少必要欄位 {key}"
    assert set(report["summary"]) == {"error", "warning", "info"}
    for finding in report["findings"]:
        assert re.match(r"^(VAL|LINT|SEC|PERF)-\d{3}$", finding["rule"])
        assert finding["severity"] in ("error", "warning", "info")


def test_every_schema_is_valid_json():
    """Schema 本身必須是合法 JSON 且宣告 Draft 2020-12。"""
    schema_dir = Path(__file__).resolve().parent.parent / "spec/schemas"
    files = sorted(schema_dir.glob("*.json"))
    assert len(files) >= 5
    for path in files:
        doc = json.loads(path.read_text())
        assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema", path.name
        assert "$id" in doc, path.name


@pytest.mark.anyio
async def test_overview_covers_every_area_instead_of_truncating(tmp_path: Path):
    """目錄放不下時，模型必須仍然知道「有哪些領域」。

    迴歸重點：先前的做法是按字典序截斷。315 個 skill 時，模型只看得到
    前 6 個領域，另外 15 個完全隱形——而且它無從發現自己漏了什麼。
    """
    root = tmp_path / "many"
    areas = ["billing", "delivery", "finance", "inventory", "orders",
             "payment", "shipping", "tax", "vendor", "warehouse", "zone"]
    for area in areas:
        for i in range(15):
            skill = root / f"{area}-task-{i:02d}"
            skill.mkdir(parents=True)
            skill.joinpath("SKILL.md").write_text(
                f"---\nname: {area}-task-{i:02d}\n"
                f"description: 處理{area}領域的第 {i} 類作業，含查詢、更新與狀態追蹤。\n"
                f"tags: [{area}]\n---\n內文\n"
            )

    async with Client(build_server([root], refresh_interval=0, context_tokens=30_000)) as client:
        overview = (await client.call_tool("list_skills", {})).data
        drilled = (await client.call_tool("list_skills", {"tags": ["zone"]})).data

    assert overview["view"] == "overview"
    assert overview["total"] == len(areas) * 15
    # 每個領域都必須出現在 facets 裡，一個都不能漏
    assert set(overview["facets"]) == set(areas), (
        f"有領域對模型隱形：{set(areas) - set(overview['facets'])}"
    )
    assert all(count == 15 for count in overview["facets"].values())
    assert len(json.dumps(overview, ensure_ascii=False)) // 3 < 30_000 * 0.15

    # 總覽指出的路徑必須真的可用
    assert drilled["view"] == "list"
    assert drilled["count"] == 15
    assert all(card["name"].startswith("zone-") for card in drilled["skills"])


@pytest.mark.anyio
async def test_small_catalogue_still_returns_the_full_list(tmp_path: Path):
    """總覽只在放不下時啟用。小目錄必須維持原本的完整清單。"""
    root = tmp_path / "few"
    for i in range(5):
        skill = root / f"tool-{i}"
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: tool-{i}\ndescription: 第 {i} 個工具，用於測試小型目錄的行為。\n---\n內文\n"
        )
    async with Client(build_server([root], refresh_interval=0, context_tokens=30_000)) as client:
        result = (await client.call_tool("list_skills", {})).data
    assert result["view"] == "list"
    assert result["count"] == 5
    assert "facets" not in result


def test_facets_fall_back_to_the_name_prefix(tmp_path: Path):
    """沒有 tags 的 skill 仍要能被分組，否則總覽對它們沒有幫助。"""
    root = tmp_path / "untagged"
    for name in ("order-lookup", "order-refund", "user-profile"):
        skill = root / name
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 測試用的技能，描述長度足以通過檢查。\n---\n內文\n"
        )
    facets = SkillIndex([root]).facets()
    assert facets == {"order": 2, "user": 1}


# ============================ 中文檢索（BM25 + CJK bigram）===================


def _zh_index(tmp_path: Path) -> SkillIndex:
    skills = [
        ("order-lookup", "查詢內部訂單系統的狀態與明細。當使用者問到訂單編號、出貨狀態或退款進度時使用。", "order"),
        ("order-refund", "處理訂單退款申請與退款狀態查詢。涉及金流退回、部分退款與退款失敗重試。", "order"),
        ("inventory-check", "查詢商品庫存數量與倉庫分佈。支援單一 SKU 或批次查詢，含安全庫存判斷。", "inventory"),
        ("shipping-track", "追蹤出貨物流狀態與預計到貨時間。串接黑貓、新竹貨運與郵局的追蹤編號。", "shipping"),
        ("invoice-issue", "開立電子發票與折讓單。處理發票作廢、載具歸戶與捐贈碼。", "finance"),
        ("member-profile", "查詢會員基本資料、等級與點數餘額。含會員合併與資料修改紀錄。", "member"),
        ("payment-status", "查詢金流交易狀態。支援信用卡、ATM 轉帳與行動支付的對帳查詢。", "payment"),
    ]
    for name, desc, tag in skills:
        skill = tmp_path / name
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\ntags: [{tag}]\n---\n內文\n"
        )
    return SkillIndex([tmp_path])


@pytest.mark.parametrize(
    "query,expected",
    [
        ("查訂單", "order-lookup"),
        ("訂單狀態", "order-lookup"),
        ("我要查詢訂單編號", "order-lookup"),
        ("退款", "order-refund"),
        ("庫存還有多少", "inventory-check"),
        ("貨到哪了", "shipping-track"),      # 詞序顛倒：description 是「到貨」
        ("物流追蹤", "shipping-track"),
        ("發票", "invoice-issue"),
        ("開發票給客戶", "invoice-issue"),
        ("會員點數", "member-profile"),
        ("刷卡有沒有成功", "payment-status"),
    ],
)
def test_chinese_query_finds_the_right_skill(tmp_path: Path, query: str, expected: str):
    """中文查詢必須命中。

    迴歸重點：原本用 `query.split()` 加子字串比對，中文沒有空白所以整句
    變成單一 token，而「查訂單」不是「查詢內部訂單系統…」的子字串。
    這 13 個真實查詢當時只命中 1 個（8%）。
    """
    results = [card["name"] for card in _zh_index(tmp_path).catalog(query=query, limit=3)]
    assert results, f"查詢 {query!r} 完全沒有結果"
    assert results[0] == expected, f"{query!r} 的第一名是 {results[0]}，期望 {expected}"


def test_english_search_is_not_regressed(tmp_path: Path):
    """為中文加的切詞不能犧牲英文。"""
    for name, desc in [
        ("csv-profile", "Profile a CSV or TSV file with column types and null counts."),
        ("repo-digest", "Summarise a git repository, recent commits and churn by file."),
        ("api-fetch", "Call an HTTP JSON API with explicit timeouts and retries."),
    ]:
        skill = tmp_path / name
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n內文\n"
        )
    index = SkillIndex([tmp_path])
    for query, expected in [
        ("csv", "csv-profile"),
        ("profile a csv file", "csv-profile"),
        ("git commits", "repo-digest"),
        ("http timeout retries", "api-fetch"),
    ]:
        top = index.catalog(query=query, limit=3)
        assert top and top[0]["name"] == expected, f"{query!r} -> {top}"


def test_mixed_language_description_is_searchable_both_ways(tmp_path: Path):
    """內部 skill 的說明常常中英夾雜，兩種語言都要能檢索。"""
    skill = tmp_path / "sku-lookup"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: sku-lookup\n"
        "description: 查詢 SKU 商品主檔與庫存。Supports batch lookup by SKU code or barcode.\n"
        "---\n內文\n"
    )
    index = SkillIndex([tmp_path])
    for query in ("SKU", "查 SKU 庫存", "barcode", "商品主檔"):
        assert index.catalog(query=query), f"{query!r} 找不到"


def test_bm25_ranks_the_more_specific_skill_first(tmp_path: Path):
    """兩個都提到「退款」時，專門處理退款的那個要排前面。"""
    index = _zh_index(tmp_path)
    ranked = [card["name"] for card in index.catalog(query="退款申請", limit=3)]
    assert ranked[0] == "order-refund", ranked


def test_tokenizer_produces_bigrams_and_unigrams():
    from skill_server.search import tokenize

    tokens = tokenize("訂單狀態")
    assert "訂單" in tokens and "狀態" in tokens, "缺少 bigram"
    assert "訂" in tokens, "缺少 unigram（單字查詢與詞序顛倒需要）"
    assert "sku" in tokenize("查詢 SKU"), "英文要小寫後成詞"


def test_tags_filter_still_applies_with_a_query(tmp_path: Path):
    """BM25 的結果仍要受 tags 過濾。"""
    index = _zh_index(tmp_path)
    both = index.catalog(query="查詢", limit=10)
    filtered = index.catalog(query="查詢", tags=["order"], limit=10)
    assert len(filtered) < len(both)
    assert all(card["tags"] == ["order"] for card in filtered)


def test_search_index_is_rebuilt_on_refresh(tmp_path: Path):
    """新增 skill 後必須立刻可被檢索，否則熱載入等於半殘。"""
    first = tmp_path / "alpha-one"
    first.mkdir(parents=True)
    first.joinpath("SKILL.md").write_text(
        "---\nname: alpha-one\ndescription: 處理第一類作業的查詢與更新。\n---\n內文\n"
    )
    index = SkillIndex([tmp_path])
    assert not index.catalog(query="退貨退款")

    second = tmp_path / "beta-two"
    second.mkdir(parents=True)
    second.joinpath("SKILL.md").write_text(
        "---\nname: beta-two\ndescription: 處理退貨退款與折讓的申請流程。\n---\n內文\n"
    )
    assert index.refresh() is True
    hits = [card["name"] for card in index.catalog(query="退貨退款")]
    assert hits and hits[0] == "beta-two"


# ============================ 索引更新的正確性 ==============================


def test_refresh_still_detects_content_and_structure_changes(tmp_path: Path):
    """refresh 必須抓到三種變動：改內容、新增、刪除。

    背景輪詢預設關閉（skill 在 image 裡不會變），但 `reload_skills` 與本機
    開發都依賴這條路徑正確。
    """
    first = tmp_path / "alpha-one"
    first.mkdir()
    first.joinpath("SKILL.md").write_text(
        "---\nname: alpha-one\ndescription: 第一個技能，用於測試偵測行為。\n---\n第一版\n"
    )
    index = SkillIndex([tmp_path])
    assert index.refresh() is False

    # 1) 改內容
    first.joinpath("SKILL.md").write_text(
        "---\nname: alpha-one\ndescription: 第一個技能，描述已經被修改過了。\n---\n第二版\n"
    )
    assert index.refresh() is True, "改內容沒被偵測到"
    assert "修改過" in index.get("alpha-one").description

    # 2) 新增 skill
    second = tmp_path / "beta-two"
    second.mkdir()
    second.joinpath("SKILL.md").write_text(
        "---\nname: beta-two\ndescription: 第二個技能，用於驗證新增偵測。\n---\n內文\n"
    )
    assert index.refresh() is True, "新增沒被偵測到"
    assert len(index) == 2

    # 3) 刪除 skill
    import shutil

    shutil.rmtree(second)
    assert index.refresh() is True, "刪除沒被偵測到"
    assert len(index) == 1


# ================ 共用函式庫（skill 之間的程式碼重用）=======================


def _shared_lib_tree(root: Path, *, namespaced: bool = False) -> Path:
    """建立 skills/_shared/lib.sh 加一個 skill，可選擇放在命名空間子目錄下。"""
    shared = root / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "lib.sh").write_text(
        '#!/bin/bash\njson_error() { printf \'{"ok":false,"from":"shared"}\\n\'; }\n'
    )
    skill = (root / "finance" / "invoice-issue") if namespaced else (root / "invoice-issue")
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: invoice-issue\ndescription: 開立發票與折讓單的完整流程。當使用者需要開票時使用。\n---\n# 開票\n"
    )
    (skill / "scripts" / "run.sh").write_text(
        '#!/bin/bash\nset -euo pipefail\nsource "$SKILL_ROOT/_shared/lib.sh"\njson_error\n'
    )
    return root


def test_shared_directory_is_not_indexed_as_a_skill(tmp_path: Path):
    """`_shared/` 沒有 SKILL.md，所以不該被當成 skill。

    這是「重用發生在 script 層而非 skill 層」的前提：共用程式碼對模型
    必須完全不可見，否則就變成要多付一次 load_skill 的成本。
    """
    index = SkillIndex([_shared_lib_tree(tmp_path)])
    names = {card["name"] for card in index.catalog(limit=20)}
    assert names == {"invoice-issue"}
    assert index.stats()["rejected"] == [], "_shared 不該被當成壞掉的 skill 回報"


@pytest.mark.parametrize("namespaced", [False, True])
@pytest.mark.anyio
async def test_skill_root_resolves_shared_lib_at_any_depth(tmp_path: Path, namespaced: bool):
    """SKILL_ROOT 讓共用函式庫在任何目錄深度都能被引用。

    迴歸重點：原本文件教的是 `$SKILL_DIR/../_shared/lib.sh`，但在命名空間
    子目錄（skills/<team>/<skill>/）下 `../` 會少算一層，指到 skills/<team>/
    而不是 skills/，source 直接失敗。
    """
    root = _shared_lib_tree(tmp_path, namespaced=namespaced)
    result = await ScriptRunner(SkillIndex([root])).run(
        "invoice-issue", "scripts/run.sh", timeout=30
    )
    assert result.status == "ok", result.stderr
    assert json.loads(result.stdout)["from"] == "shared"


@pytest.mark.anyio
async def test_skill_root_is_exposed_and_points_at_the_owning_root(tmp_path: Path):
    """多個 Skill Root 時，每個 skill 拿到的必須是自己所屬的那一個。"""
    roots = []
    for i in (0, 1):
        root = tmp_path / f"root{i}"
        skill = root / f"team{i}-task"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: team{i}-task\ndescription: 第 {i} 個根目錄下的技能，描述長度足夠。\n---\nx\n"
        )
        (skill / "scripts" / "show.sh").write_text('#!/bin/bash\nset -e\necho "$SKILL_ROOT"\n')
        roots.append(root)

    runner = ScriptRunner(SkillIndex(roots))
    for i, root in enumerate(roots):
        result = await runner.run(f"team{i}-task", "scripts/show.sh")
        assert result.stdout.strip() == str(root.resolve()), (
            f"team{i}-task 拿到的 SKILL_ROOT 不是自己所屬的根目錄"
        )


@pytest.mark.anyio
async def test_caller_cannot_hijack_the_shared_library_via_skill_root(tmp_path: Path):
    """曾經可行：呼叫端設 env={"SKILL_ROOT": 攻擊者目錄} 就能劫持共用函式庫。

    加入 SKILL_ROOT 時漏了把它放進封鎖清單。共用函式庫被每支 script
    source，劫持它等同於在每次呼叫時執行任意程式碼。
    """
    root = _shared_lib_tree(tmp_path / "real")
    evil = tmp_path / "evil"
    (evil / "_shared").mkdir(parents=True)
    (evil / "_shared" / "lib.sh").write_text(
        '#!/bin/bash\njson_error() { printf \'{"ok":false,"from":"PWNED"}\\n\'; }\n'
    )

    runner = ScriptRunner(SkillIndex([root]))
    clean = await runner.run("invoice-issue", "scripts/run.sh", timeout=30)
    assert json.loads(clean.stdout)["from"] == "shared"

    with pytest.raises(ScriptError, match="SKILL_ROOT"):
        await runner.run(
            "invoice-issue", "scripts/run.sh", env={"SKILL_ROOT": str(evil)}, timeout=30
        )


# ==================== description 可區分性（LINT-040）=======================


def _write_skills(root: Path, pairs: list[tuple[str, str]]) -> Path:
    for name, desc in pairs:
        skill = root / name
        skill.mkdir(parents=True)
        skill.joinpath("SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n內文\n"
        )
    return root


VAGUE = [
    ("order-query", "查詢訂單資料。提供訂單的查詢功能。"),
    ("order-detail", "查詢訂單明細。提供訂單明細的查詢功能。"),
    ("order-status", "查詢訂單狀態。提供訂單狀態的查詢功能。"),
    ("refund-query", "查詢退款資料。提供退款的查詢功能。"),
    ("refund-status", "查詢退款狀態。提供退款狀態的查詢功能。"),
]
DISTINCT = [
    ("order-lookup", "依訂單編號查出貨進度與物流單號。當使用者問「我的東西寄到哪」或提供訂單編號時使用。"),
    ("order-items", "列出訂單內的商品明細與單價金額。當使用者問「我買了什麼」「總共多少錢」時使用。"),
    ("refund-apply", "建立退款申請並回傳受理編號。當使用者說「我要退貨」「要退錢」時使用。"),
    ("refund-progress", "查詢已申請退款的處理進度與預計入帳日。當使用者問「錢什麼時候退」時使用。"),
    ("invoice-issue", "開立電子發票或折讓單。當使用者要發票、報帳或改開統編時使用。"),
]


def test_indistinguishable_descriptions_are_flagged(tmp_path: Path):
    """單看每個 description 都合格，擺在一起才看得出問題。

    這是唯一需要全域視角的規則。選錯 skill 是最常見的失敗模式，而且上線後
    難以歸因——模型不會說「我在兩個之間猶豫」，它只會選一個然後答錯。
    """
    report = _run_validator(str(_write_skills(tmp_path, VAGUE)))
    flagged = [f for f in report["findings"] if f["rule"] == "LINT-040"]
    assert flagged, "五個幾乎一樣的 description 應該被標記"
    assert all(f["severity"] == "warning" for f in flagged)
    assert "相似度" in flagged[0]["message"]
    assert "何時使用" in flagged[0]["remediation"], "要指出怎麼改，不只說有問題"


def test_distinct_descriptions_pass_cleanly(tmp_path: Path):
    """誤報比漏報更傷（RFC-175）：寫得好的 description 不能被標記。"""
    report = _run_validator(str(_write_skills(tmp_path, DISTINCT)), ["--level=L2"])
    assert not [f for f in report["findings"] if f["rule"] == "LINT-040"]
    assert report["passed"] is True


def test_similarity_threshold_separates_the_two_populations():
    """門檻必須落在兩個分佈之間，否則不是好標記就是好放行。

    校準資料：照直覺寫的相似度 0.43–0.57，改寫後 0.11–0.14。
    """
    from spec.validate import (
        DESCRIPTION_SIMILARITY_LIMIT,
        _describe_tokens,
        _jaccard,
    )

    def pairs(skills):
        return [
            _jaccard(_describe_tokens(a[1]), _describe_tokens(b[1]))
            for i, a in enumerate(skills)
            for b in skills[i + 1 :]
        ]

    vague, distinct = pairs(VAGUE), pairs(DISTINCT)
    assert max(distinct) < DESCRIPTION_SIMILARITY_LIMIT < max(vague), (
        f"門檻 {DESCRIPTION_SIMILARITY_LIMIT} 沒有落在兩個分佈之間："
        f"好的最高 {max(distinct):.2f}，差的最高 {max(vague):.2f}"
    )


def test_short_descriptions_are_skipped_not_falsely_flagged(tmp_path: Path):
    """token 太少無法可靠比較，跳過而不是誤報。"""
    report = _run_validator(str(_write_skills(tmp_path, [
        ("a-one", "查訂單"), ("a-two", "查庫存"),
    ])))
    assert not [f for f in report["findings"] if f["rule"] == "LINT-040"]


def test_single_skill_never_triggers_similarity(tmp_path: Path):
    """只有一個 skill 時沒有比較對象。"""
    report = _run_validator(str(_write_skills(tmp_path, [
        ("solo-skill", "查詢訂單資料。提供訂單的查詢功能與明細列表。"),
    ])))
    assert not [f for f in report["findings"] if f["rule"] == "LINT-040"]


# ======================= 相容性檢查（spec.compat）===========================


def _manifests(*entries) -> dict:
    """(name, version, tags, scripts, timeout) -> compat 用的 manifest 結構。"""
    out = {}
    for name, version, tags, scripts, timeout in entries:
        manifest = {"name": name, "description": f"{name} 的說明文字，長度足以通過檢查。",
                    "_scripts": list(scripts)}
        if version:
            manifest["version"] = version
        if tags:
            manifest["tags"] = list(tags)
        if timeout is not None:
            manifest["execution"] = {"default": {"timeout": timeout}}
        out[name] = manifest
    return out


def test_compat_classifies_every_change_type():
    """每一類變更都要對應到 RFC-08 §14.2 的正確分類。"""
    from spec.compat import BREAKING, MINOR, CompatReport, compare

    base = _manifests(
        ("kept", "1.0.0", ["a", "b"], ["scripts/x.sh", "scripts/y.sh"], 30),
        ("removed", "1.0.0", [], [], None),
    )
    head = _manifests(
        ("kept", "1.0.0", ["a"], ["scripts/x.sh", "scripts/z.sh"], 10),
        ("added", "1.0.0", [], [], None),
    )
    report = CompatReport()
    compare(base, head, report)
    by_kind = {}
    for change in report.changes:
        by_kind.setdefault(change.kind, []).append(change.what)

    breaking = " | ".join(by_kind[BREAKING])
    assert "skill 被移除" in breaking
    assert "移除 script scripts/y.sh" in breaking
    assert "縮短為 10s" in breaking
    assert "移除 tag" in breaking

    minor = " | ".join(by_kind[MINOR])
    assert "新增 skill" in minor
    assert "新增 script scripts/z.sh" in minor


def test_compat_fails_when_breaking_change_lacks_major_bump():
    """工具唯一會讓 CI 失敗的判準：變更本身可不可接受由人決定，
    但「破壞性變更卻沒 bump major」是客觀錯誤。"""
    from spec.compat import CompatReport, check_versions, compare

    base = _manifests(("svc", "1.2.0", ["x"], ["scripts/a.sh"], 60))
    head = _manifests(("svc", "1.3.0", ["x"], ["scripts/a.sh"], 15))  # 縮短 timeout
    report = CompatReport()
    compare(base, head, report)
    check_versions(base, head, report)

    assert report.passed is False
    assert "未提升 major" in report.version_problems[0]


def test_compat_passes_when_major_is_bumped():
    from spec.compat import CompatReport, check_versions, compare

    base = _manifests(("svc", "1.2.0", ["x"], ["scripts/a.sh"], 60))
    head = _manifests(("svc", "2.0.0", ["x"], ["scripts/a.sh"], 15))
    report = CompatReport()
    compare(base, head, report)
    check_versions(base, head, report)

    assert report.passed is True, report.version_problems
    assert any(c.kind == "breaking" for c in report.changes), "變更仍要被回報"


def test_compat_treats_minor_changes_as_compatible():
    """放寬 timeout、新增 tag、改 description 都不是破壞性的。"""
    from spec.compat import BREAKING, CompatReport, check_versions, compare

    base = _manifests(("svc", "1.0.0", ["x"], ["scripts/a.sh"], 30))
    head = _manifests(("svc", "1.1.0", ["x", "y"], ["scripts/a.sh"], 90))
    head["svc"]["description"] = "改過的說明文字，同樣長度足以通過檢查規則。"
    report = CompatReport()
    compare(base, head, report)
    check_versions(base, head, report)

    assert report.passed is True
    assert not [c for c in report.changes if c.kind == BREAKING]


def test_compat_reads_base_from_git_without_checkout():
    """base 版本必須從 git 讀進記憶體，不 checkout——服務與工具都不寫檔案。"""
    from spec.compat import read_manifests_at

    manifests = read_manifests_at("HEAD", "skills")
    assert "api-call" in manifests
    assert manifests["api-call"]["_scripts"] == ["scripts/call.sh"]


@pytest.mark.anyio
async def test_compat_reports_no_diff_against_itself():
    """與自己比對必須是零差異，否則讀取路徑有 bug。"""
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "spec.compat", "--base=HEAD", "--head=HEAD", "--format=json"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    report = json.loads(proc.stdout)
    assert report["changes"] == []
    assert report["passed"] is True
    assert proc.returncode == 0
