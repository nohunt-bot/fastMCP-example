"""Regression tests for the runner/hooks hardening pass.

One test per defect from the audit: a stdin write that used to bypass the
deadline loop and leak a bare OSError, missing resource limits on the child,
a hook payload that skipped the argv caps the script path already enforced,
PS4 missing from the forbidden-env list, run_path not clamping its timeout,
and caller env *values* never being validated. Each test was verified to
genuinely fail against the pre-fix source (by stashing the runner.py/hooks.py
changes and re-running) and to pass after.

Style follows tests/test_server.py: direct ScriptRunner/SkillIndex/HookRunner
construction rather than going through the MCP client, since these are
runner-layer concerns, not tool-dispatch ones.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from skill_server.hooks import HookRunner
from skill_server.index import SkillIndex
from skill_server.runner import MAX_ARG_LEN, MAX_ARGS, ScriptError, ScriptRunner


def _make_skill(tmp_path: Path, name: str, script: str, code: str) -> Path:
    """Same shape as test_server.py's helper -- one script, one skill."""
    skill = tmp_path / name
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
    (skill / "scripts" / script).write_text(code)
    return tmp_path


# --------------------------------------------------------- defect 1: stdin


@pytest.mark.anyio
async def test_stdin_write_does_not_block_the_deadline_loop(tmp_path: Path):
    """Two failure modes reproduced against the pre-fix code:

    1. A child that never reads stdin and runs long (sleeper.sh, 60s) used to
       block the sequential ``write()`` + ``await drain()`` until the child
       exited on its own -- the 2s ``timeout`` never got a chance to fire,
       because the deadline loop it lives in had not even started yet.
    2. A child that exits immediately without reading stdin (quick.sh) then
       raised BrokenPipeError once the write end broke -- a bare OSError that
       propagated straight out of run(), past server.py's
       ``except (ScriptError, SkillLoadError)``, discarding the captured
       output along the way.

    Neither is a bug in the child: most shipped skills never read stdin at
    all. The fix runs the write as a task the deadline loop supervises, and
    swallows BrokenPipeError/ConnectionResetError as the ordinary case they
    are.
    """
    root = _make_skill(tmp_path, "stdin-ignorer", "sleeper.sh", "#!/bin/bash\nsleep 60\n")
    (root / "stdin-ignorer" / "scripts" / "quick.sh").write_text(
        "#!/bin/bash\necho done\nexit 0\n"
    )

    runner = ScriptRunner(SkillIndex([root]))
    big_stdin = "A" * 200_000  # well over the ~64KiB pipe buffer

    started = time.monotonic()
    result = await runner.run(
        "stdin-ignorer", "scripts/sleeper.sh", stdin=big_stdin, timeout=2.0, stall_timeout=0
    )
    elapsed = time.monotonic() - started
    assert result.status == "timeout"
    assert elapsed < 6.0, (
        f"timeout=2s but took {elapsed:.2f}s -- the stdin write blocked the deadline loop "
        "instead of running concurrently with it"
    )

    started = time.monotonic()
    result = await runner.run(
        "stdin-ignorer", "scripts/quick.sh", stdin=big_stdin, timeout=5.0
    )
    elapsed = time.monotonic() - started
    assert result.status == "ok", "a child ignoring stdin must not be treated as an error"
    assert result.stdout.strip() == "done", "captured output must survive"
    assert elapsed < 2.0, "a child that exits immediately must not wait on the broken pipe"


# ------------------------------------------------- defect 2: resource limits


@pytest.mark.anyio
async def test_child_resource_limits_are_enforced(tmp_path: Path):
    """Before the fix there was no setrlimit anywhere: on a 512Mi pod a script
    doing ``x=[0]*10**9`` or a fork bomb OOM-kills the whole pod instead of
    just failing its own call, and a busy loop with no output evades
    stall_timeout entirely (it never goes silent, it's just never done).

    RLIMIT_AS/RLIMIT_NPROC behave differently enough on macOS (setrlimit on
    RLIMIT_AS raises ValueError outright there) that this test does not rely
    on them for its assertions -- the per-limit try/except degrading them
    gracefully is exactly the point, and is covered implicitly by every other
    test in this file still passing on this platform. What is asserted
    directly, cross-platform:

    * the constructor accepts these keyword arguments at all -- pre-fix,
      ``ScriptRunner(...)`` below raises TypeError immediately, since none of
      these parameters existed;
    * RLIMIT_CPU actually kills a silent CPU-bound busy loop well before the
      wall-clock timeout -- this is the case stall_timeout cannot catch,
      because the script never goes quiet, it is simply never finished;
    * RLIMIT_FSIZE=0 actually blocks a real file write, not just a large one.
    """
    root = _make_skill(tmp_path, "burner", "burn.py", "x = 0\nwhile True:\n    x += 1\n")
    (root / "writer" / "scripts").mkdir(parents=True)
    (root / "writer" / "SKILL.md").write_text("---\nname: writer\ndescription: d\n---\nb\n")
    (root / "writer" / "scripts" / "w.py").write_text(
        "import sys\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write('x' * 10_000)\n"
        "    print('wrote')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )

    # Pre-fix, this line alone raises TypeError: unexpected keyword argument.
    runner = ScriptRunner(
        SkillIndex([root]),
        max_address_space_bytes=512 * 1024 * 1024,
        max_child_processes=256,
        max_file_size_bytes=0,
        max_cpu_seconds=1,
    )

    started = time.monotonic()
    result = await runner.run("burner", "scripts/burn.py", timeout=8.0, stall_timeout=0)
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, (
        "a 1s CPU rlimit must kill a busy loop long before an 8s wall-clock timeout; "
        f"took {elapsed:.2f}s -- no CPU limit is being applied"
    )
    assert result.status == "failed"
    assert result.exit_code is not None and result.exit_code < 0, (
        "killed by a signal (SIGXCPU), not a normal exit"
    )

    target = tmp_path / "should-stay-unwritten.txt"
    result = await runner.run("writer", "scripts/w.py", [str(target)])
    assert result.stdout.strip() == "blocked", "RLIMIT_FSIZE=0 must stop the write outright"


# ---------------------------------------------------------- defect 3: hooks


@pytest.mark.anyio
async def test_hook_payload_enforces_the_same_arg_caps_as_scripts(tmp_path: Path):
    """_build_argv enforces MAX_ARGS/MAX_ARG_LEN for the script path, but
    hooks.py used to put caller-supplied args verbatim into the pre-hook
    payload and json.dumps() it before any size check applied -- so a skill
    WITH hooks accepted much larger args than a hookless skill did, from the
    same caller.
    """
    skill = tmp_path / "hooked"
    (skill / "scripts").mkdir(parents=True)
    (skill / "hooks").mkdir()
    (skill / "SKILL.md").write_text("---\nname: hooked\ndescription: d\n---\nb\n")
    (skill / "scripts" / "run.py").write_text("print(1)")
    (skill / "hooks" / "pre.py").write_text('import json; print(json.dumps({}))')

    index = SkillIndex([tmp_path])
    hooks = HookRunner(ScriptRunner(index))
    meta = index.get("hooked")

    with pytest.raises(ScriptError, match="too many arguments"):
        await hooks.run_pre(meta, "scripts/run.py", ["x"] * (MAX_ARGS + 1), None)

    with pytest.raises(ScriptError, match="argument longer than"):
        await hooks.run_pre(meta, "scripts/run.py", ["x" * (MAX_ARG_LEN + 1)], None)

    # within the caps, the hook still runs normally
    outcome = await hooks.run_pre(meta, "scripts/run.py", ["ok"], None)
    assert outcome.args is None


# --------------------------------------------------- defect 4: forbidden env


@pytest.mark.parametrize("variable", ["PS4", "SHELLOPTS", "BASHOPTS", "HOME", "TMPDIR"])
@pytest.mark.anyio
async def test_ps4_and_identity_vars_are_forbidden(tmp_path: Path, variable: str):
    """PS4 used to be missing from _CALLER_FORBIDDEN_ENV: any .sh script that
    runs `set -x` expands PS4 via command substitution on every traced line,
    so env={"PS4": "$(...)"} is arbitrary execution the moment the script
    traces itself. SHELLOPTS/BASHOPTS are the sibling vectors -- bash reads
    them from the environment to switch shell options (including tracing) on
    before the script's own `set` calls run.

    HOME/TMPDIR cover the "Also" question in the brief: the server means HOME
    to be the skill's own directory (see _child_env), so a caller overriding
    it can redirect where a library the script shells out to (git, ssh, npm)
    resolves its own config from. TMPDIR travels with it for the same
    identity reason.
    """
    root = _make_skill(tmp_path, "envtest", "run.py", "print('x')")
    runner = ScriptRunner(SkillIndex([root]))
    with pytest.raises(ScriptError, match="cannot be set by the caller"):
        await runner.run("envtest", "scripts/run.py", env={variable: "/tmp/attacker"})


# ---------------------------------------------------- defect 5: run_path clamp


@pytest.mark.anyio
async def test_run_path_clamps_timeout_to_max_timeout(tmp_path: Path):
    """run() does ``min(timeout, self.max_timeout)`` plus a <=0 rejection;
    run_path() (hooks' entry point) used to take timeout and pass it straight
    through uncapped. The one real caller (HookRunner) hardcodes 10.0 today,
    so this was harmless in practice, but "no caller can exceed max_timeout"
    should be an invariant of the function, not a convention upheld by its
    only call site.
    """
    skill = tmp_path / "slowhook"
    (skill / "scripts").mkdir(parents=True)
    (skill / "hooks").mkdir()
    (skill / "SKILL.md").write_text("---\nname: slowhook\ndescription: d\n---\nb\n")
    hook = skill / "hooks" / "pre.py"
    hook.write_text("import time; time.sleep(30)")

    runner = ScriptRunner(SkillIndex([tmp_path]), max_timeout=1.0)
    started = time.monotonic()
    result = await runner.run_path(
        hook, cwd=skill, skill="slowhook", jail=skill, timeout=30.0, stall_timeout=0
    )
    elapsed = time.monotonic() - started
    assert result.status == "timeout"
    assert elapsed < 5.0, (
        f"timeout=30 was requested but max_timeout=1 must win; took {elapsed:.2f}s"
    )


# --------------------------------------------------- defect 6: env values


@pytest.mark.anyio
async def test_caller_env_values_reject_control_characters(tmp_path: Path):
    """check_caller_env only ever looked at names, not values. A value
    containing a NUL byte reaches create_subprocess_exec and raises a bare
    ValueError there -- server.py catches (ScriptError, SkillLoadError)
    around the call into the runner, so that ValueError used to crash the
    tool call outright instead of coming back as an ordinary error result.
    """
    root = _make_skill(tmp_path, "envval", "run.py", "print('x')")
    runner = ScriptRunner(SkillIndex([root]))

    with pytest.raises(ScriptError, match="control character"):
        await runner.run("envval", "scripts/run.py", env={"X": "a\x00b"})

    # The cap must not be so tight it breaks ordinary multi-line data (a PEM
    # cert, a JSON blob) -- only real control characters are rejected.
    result = await runner.run("envval", "scripts/run.py", env={"X": "line1\nline2"})
    assert result.exit_code == 0
