"""Smoke + security tests. Run: uv run --extra dev pytest -q

Uses FastMCP's in-memory transport (`Client(mcp)`), which exercises the real
tool dispatch, validation and serialisation path without opening a socket.
"""

from __future__ import annotations

import asyncio
import json
import os
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


@pytest.mark.anyio
async def test_state_dir_is_exposed_and_persists_between_calls(tmp_path: Path):
    root = _make_skill(
        tmp_path / "skills",
        "stateful",
        "touch.py",
        "import os, pathlib\n"
        "p = pathlib.Path(os.environ['SKILL_STATE_DIR']) / 'counter'\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n)); print(n)\n",
    )
    runner = ScriptRunner(SkillIndex([root]), state_root=tmp_path / "state")
    assert (await runner.run("stateful", "scripts/touch.py")).stdout.strip() == "1"
    assert (await runner.run("stateful", "scripts/touch.py")).stdout.strip() == "2"


@pytest.mark.anyio
async def test_state_dirs_are_isolated_per_skill(tmp_path: Path):
    code = "import os; print(os.environ['SKILL_STATE_DIR'])"
    root = _make_skill(tmp_path / "s", "alpha", "show.py", code)
    _make_skill(tmp_path / "s", "beta", "show.py", code)
    runner = ScriptRunner(SkillIndex([root]), state_root=tmp_path / "state")
    a = (await runner.run("alpha", "scripts/show.py")).stdout.strip()
    b = (await runner.run("beta", "scripts/show.py")).stdout.strip()
    assert a != b and a.endswith("alpha") and b.endswith("beta")


@pytest.mark.anyio
async def test_submitted_keys_survive_in_the_ledger(tmp_path: Path):
    """The failure this guards is not a lost job -- the job completes either way --
    it is losing the only handle that can retrieve the result."""
    import http.server
    import json as _json
    import threading
    import uuid as _uuid

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = _json.dumps({"key": str(_uuid.uuid4()), "status": "accepted"}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runner = ScriptRunner(SkillIndex([SKILLS]), state_root=tmp_path / "state")
    url = f"http://127.0.0.1:{port}/api/jobs"

    try:
        submitted = []
        for label in ("q3 report", "nightly rebuild"):
            result = await runner.run(
                "async-job", "scripts/job.py",
                ["submit", url, "--body", '{"x":1}', "--label", label],
                timeout=30,
            )
            assert result.status == "ok", result.stderr
            payload = json.loads(result.stdout)
            assert payload["key"] and payload["ledger"]
            # submit must not block: the job runs to completion by design
            assert result.duration_ms < 5_000
            submitted.append((payload["key"], label))

        listed = await runner.run("async-job", "scripts/job.py", ["list", "--limit", "10"])
        jobs = json.loads(listed.stdout)["jobs"]
        assert {j["key"] for j in jobs} == {k for k, _ in submitted}
        assert {j["label"] for j in jobs} == {lbl for _, lbl in submitted}

        # A label is what makes the ledger usable once the uuid means nothing.
        found = await runner.run("async-job", "scripts/job.py", ["list", "--grep", "q3"])
        matches = json.loads(found.stdout)["jobs"]
        assert len(matches) == 1 and matches[0]["label"] == "q3 report"
    finally:
        server.shutdown()


@pytest.mark.anyio
async def test_ledger_is_append_only_under_concurrent_submits(tmp_path: Path):
    """Two submits at once must not lose each other's key."""
    root = _make_skill(
        tmp_path / "skills",
        "appender",
        "add.py",
        "import json, os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['SKILL_STATE_DIR']) / 'jobs.jsonl'\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        "with p.open('a') as fh: fh.write(json.dumps({'key': sys.argv[1]}) + '\\n')\n"
        "print(sys.argv[1])\n",
    )
    runner = ScriptRunner(SkillIndex([root]), state_root=tmp_path / "state")
    await asyncio.gather(
        *(runner.run("appender", "scripts/add.py", [f"key-{i}"]) for i in range(12))
    )
    lines = (tmp_path / "state" / "appender" / "jobs.jsonl").read_text().strip().splitlines()
    assert {json.loads(l)["key"] for l in lines} == {f"key-{i}" for i in range(12)}


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
            "--state-dir", "/tmp/state",
            "--hooks-dir", "/tmp/hooks",
            "--context-tokens", "30000",
            "--script-stall-timeout", "10",
            "--no-network-env",
        ]
    )
    assert args.port == 9001
    assert str(args.state_dir) == "/tmp/state"
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
        with pytest.raises(ToolError, match="escapes its directory"):
            await client.call_tool(
                "run_skill_script", {"name": "hooked", "script": "scripts/run.py"}
            )


@pytest.mark.parametrize(
    "variable",
    ["PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "BASH_ENV",
     "NODE_OPTIONS", "SKILL_STATE_DIR", "SKILL_DIR"],
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
