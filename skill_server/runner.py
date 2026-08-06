"""Sandboxed execution of scripts bundled inside a skill.

Threat model: the MCP client is semi-trusted (it is an LLM that can be talked
into things), the skill bundles on disk are trusted. So the job here is to make
sure a caller can only ever run *a script that a skill author put in that
skill's own scripts/ directory*, and that a misbehaving script cannot take the
server down.

Controls, in the order they apply:

* **Path jail** — the script must resolve inside its own skill directory
  (see :meth:`SkillIndex.resolve_file`) and live under ``scripts/``.
* **Interpreter allowlist** — dispatch on suffix; no shell, ever. Arguments are
  passed as an argv list, so quoting/injection is not a category that exists.
* **Writes nothing** — the server creates no files and hands scripts no writable
  directory, so it runs unchanged on a read-only root filesystem.
* **Curated environment** — the child gets an allowlist, not ``os.environ``, so
  API keys in the server's environment are not inherited. Proxy and TLS trust
  variables are passed through by default because without them every outbound
  API call in a corporate network hangs until its timeout.
* **Timeout + process group kill** — ``start_new_session=True`` means we can
  kill the whole tree, not just the direct child that may have forked.
* **Stall detection** — a script that stops producing output for
  ``stall_timeout`` is killed early, because a hung API call looks exactly like
  a slow one until you notice nothing has been written for 20 s.
* **Output cap with continued draining** — we stop *keeping* output at the cap
  but keep reading, so a chatty script cannot deadlock on a full pipe.
* **Concurrency semaphore** — bounds how many subprocesses exist at once, which
  is what actually protects throughput under load.

**Output survives every exit path.** Whatever the script printed before it was
killed is captured and returned. A timeout that reports nothing tells you only
that something is wrong; a timeout that reports "last line: fetching /v1/orders,
silent for 18 s" tells you *where*.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from skill_server.index import SkillIndex, SkillLoadError

logger = logging.getLogger(__name__)

#: Suffix -> interpreter argv prefix. Anything not listed here cannot be run,
#: including files with the executable bit set. Add entries deliberately.
INTERPRETERS: dict[str, list[str]] = {
    ".py": [sys.executable, "-I"],  # -I: isolated, ignores PYTHON* env and cwd
    ".sh": ["/bin/bash"],
}
if (_node := shutil.which("node")) is not None:
    INTERPRETERS[".js"] = [_node]

SCRIPTS_SUBDIR = "scripts"
MAX_ARGS = 64
MAX_ARG_LEN = 4096

#: Always forwarded: the child needs these to run at all.
_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")

#: Forwarded unless ``pass_network_env=False``.
#:
#: Stripping these is the single most common cause of "the script just hangs":
#: behind a corporate proxy, an outbound connection with no ``HTTPS_PROXY`` set
#: does not fail fast, it blocks until the connect timeout — which for many HTTP
#: clients is the OS default of ~2 minutes, i.e. longer than our own timeout. The
#: script then looks like *it* is broken when the environment is.
_NETWORK_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

#: Environment variables the *caller* may never set through the tool's `env`
#: argument.
#:
#: The distinction that matters: `env` is meant to carry data a script needs
#: (an API token, a base URL). These variables instead change *what gets
#: executed* — they turn "run this script" into "run whatever I point you at".
#: Since the caller is an LLM that can be talked into things by any document it
#: reads, `env={"PATH": "/tmp/mine"}` is a remote-code-execution primitive, not
#: a configuration option.
#:
#: The SKILL_* entries are here for a different reason: they are the server's
#: own statements about identity, and a caller must not be able to forge them.
_CALLER_FORBIDDEN_ENV = frozenset({
    "PATH", "SHELL", "IFS", "BASH_ENV", "ENV", "CDPATH", "GLOBIGNORE",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
    "PYTHONWARNINGS", "PYTHONINSPECT",
    "NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE",
    "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB",
    "SKILL_NAME", "SKILL_DIR", "SKILL_ROOT", "SKILL_OUTPUT_BUDGET_BYTES",
})
#: Any variable starting with one of these is refused: the dynamic-linker
#: families are the classic code-injection vector (LD_PRELOAD, DYLD_INSERT_LIBRARIES).
_CALLER_FORBIDDEN_PREFIXES = ("LD_", "DYLD_", "_RLD")

#: stdin is buffered in memory before being written to the child, so it needs a
#: ceiling for the same reason output does.
MAX_STDIN_BYTES = 4 * 1024 * 1024


def check_caller_env(env: dict[str, str]) -> None:
    """Reject caller-supplied environment that would change what is executed.

    Raises ScriptError naming the offending variable, so a legitimate caller can
    see immediately why their call was refused.
    """
    for key in env:
        upper = key.upper()
        if upper in _CALLER_FORBIDDEN_ENV or upper.startswith(_CALLER_FORBIDDEN_PREFIXES):
            raise ScriptError(
                f"environment variable {key!r} cannot be set by the caller: it changes "
                "which program runs, not how it behaves. Pass data (tokens, URLs) "
                "instead, or set it in the skill's own execution policy."
            )


class ScriptError(Exception):
    """Raised when a script cannot be run (never for a non-zero exit code)."""


@dataclass(slots=True)
class ScriptResult:
    skill: str
    script: str
    #: "ok" | "failed" | "timeout" | "stalled". Prefer this over exit_code:
    #: a killed process has no meaningful exit code.
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool
    stalled: bool
    truncated: bool
    #: Seconds between the script's last byte of output and its death. Large on
    #: a stall, ~0 on a script that was chatty right up to the timeout.
    silent_for_s: float | None = None
    #: Populated on timeout/stall: what to actually do about it.
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _child_env(
    skill_dir: Path, skill_name: str, skill_root: Path | None = None,
    *, pass_network_env: bool = True,
) -> dict[str, str]:
    """A curated environment. Allowlist, not denylist."""
    keys = _BASE_ENV + (_NETWORK_ENV if pass_network_env else ())
    env = {k: os.environ[k] for k in keys if os.environ.get(k)}
    env.setdefault("PATH", "/usr/bin:/bin")
    env.setdefault("HOME", str(skill_dir))
    env.update(
        {
            "SKILL_NAME": skill_name,
            "SKILL_DIR": str(skill_dir),
            "PYTHONUNBUFFERED": "1",  # so we see output as it happens, not at exit
        }
    )
    if skill_root is not None:
        # 讓 script 能穩定引用共用函式庫：$SKILL_DIR/../ 在命名空間子目錄
        # （skills/<team>/<skill>/）下會少算一層。
        env["SKILL_ROOT"] = str(skill_root)
    return env


@dataclass(slots=True)
class _Capture:
    """Buffers owned by the runner, not by the reader tasks.

    This is the whole trick behind partial output: if the buffers lived inside
    the reader coroutines, cancelling them on timeout would discard everything
    read so far. Here the reader only appends to state we hold, so a kill loses
    nothing that already arrived.
    """

    cap: int
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    last_line: str = ""

    def feed(self, which: str, chunk: bytes) -> None:
        self.last_activity = time.monotonic()
        buf = self.stdout if which == "stdout" else self.stderr
        room = self.cap - len(buf)
        if room > 0:
            buf += chunk[:room]
        if len(chunk) > room:
            self.truncated = True
        text = chunk.decode("utf-8", "replace").strip()
        if text:
            self.last_line = text.splitlines()[-1][:200]


async def _pump(
    stream: asyncio.StreamReader | None,
    which: str,
    state: _Capture,
    on_output: Callable[[str, str], Awaitable[None]] | None,
) -> None:
    """Drain a pipe to EOF, recording into ``state``.

    Reads by line so progress can be streamed to the client as it happens;
    falls back to a raw read for output with no newlines (progress bars).
    """
    if stream is None:
        return
    while True:
        try:
            chunk = await stream.readline()
        except (asyncio.LimitOverrunError, ValueError):
            chunk = await stream.read(64 * 1024)  # absurdly long line
        if not chunk:
            return
        state.feed(which, chunk)
        if on_output is not None:
            try:
                await on_output(which, chunk.decode("utf-8", "replace").rstrip("\n"))
            except Exception:  # a broken client must not kill the script
                logger.debug("on_output callback failed", exc_info=True)


class ScriptRunner:
    """Runs skill-bundled scripts with bounded concurrency."""

    def __init__(
        self,
        index: SkillIndex,
        *,
        max_concurrency: int = 8,
        default_timeout: float = 30.0,
        max_timeout: float = 900.0,
        default_stall_timeout: float = 20.0,
        output_cap_bytes: int = 256 * 1024,
        output_budget_bytes: int | None = None,
        pass_network_env: bool = True,
    ):
        self.index = index
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.default_stall_timeout = default_stall_timeout
        self.output_cap_bytes = output_cap_bytes
        #: Advertised to scripts so they can page at the source. Producing less
        #: is always better than producing a lot and reducing it afterwards.
        self.output_budget_bytes = output_budget_bytes
        self.pass_network_env = pass_network_env
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self.launched = 0
        self.failed = 0
        self.timeouts = 0
        self.stalls = 0
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def drain(self, timeout: float = 25.0) -> int:
        """Wait for running scripts to finish. Returns how many were still going.

        Called on shutdown. Without it, a rolling update SIGKILLs scripts
        mid-flight: for a fire-and-forget submit that has already reached the
        API but not yet printed its uuid, the job runs to completion server-side
        while the handle is lost forever.

        Bounded, because k8s only grants terminationGracePeriodSeconds before it
        sends SIGKILL anyway.
        """
        deadline = time.monotonic() + timeout
        while self._in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if self._in_flight:
            logger.warning(
                "shutting down with %d script(s) still running; their output is lost",
                self._in_flight,
            )
        return self._in_flight

    def _build_argv(self, skill: str, script: str, args: Sequence[str]) -> tuple[list[str], Path]:
        try:
            target = self.index.resolve_file(skill, script)
        except SkillLoadError as exc:
            raise ScriptError(str(exc)) from exc

        rel = target.relative_to(self.index.get(skill).directory.resolve())
        if rel.parts[0] != SCRIPTS_SUBDIR:
            raise ScriptError(
                f"only files under {SCRIPTS_SUBDIR}/ are runnable; {script!r} is not"
            )

        interpreter = INTERPRETERS.get(target.suffix)
        if interpreter is None:
            raise ScriptError(
                f"no interpreter registered for {target.suffix!r}; "
                f"allowed: {', '.join(sorted(INTERPRETERS))}"
            )

        if len(args) > MAX_ARGS:
            raise ScriptError(f"too many arguments ({len(args)} > {MAX_ARGS})")
        argv = [*interpreter, str(target)]
        for arg in args:
            text = str(arg)
            if len(text) > MAX_ARG_LEN:
                raise ScriptError(f"argument longer than {MAX_ARG_LEN} chars")
            argv.append(text)
        return argv, target

    async def run(
        self,
        skill: str,
        script: str,
        args: Sequence[str] = (),
        *,
        stdin: str | None = None,
        timeout: float | None = None,
        stall_timeout: float | None = None,
        env: dict[str, str] | None = None,
        on_output: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> ScriptResult:
        """Run a script, capturing everything it prints even if it is killed.

        Args:
            timeout: hard wall-clock ceiling.
            stall_timeout: kill early after this many seconds with no output at
                all. ``0`` disables. This is what turns "hung on an API call"
                from a full-timeout wait into a fast, specific failure.
            on_output: awaited per output line, for streaming progress to the
                client while the script is still running.
        """
        argv, target = self._build_argv(skill, script, args)
        meta = self.index.get(skill)
        limit = min(timeout or self.default_timeout, self.max_timeout)
        if limit <= 0:
            raise ScriptError("timeout must be positive")
        stall = self.default_stall_timeout if stall_timeout is None else stall_timeout

        return await self._execute(
            argv,
            cwd=meta.directory,
            skill=skill,
            script_label=str(target.relative_to(meta.directory.resolve())),
            skill_root=meta.root,
            stdin=stdin,
            timeout=limit,
            stall_timeout=stall,
            env=env,
            on_output=on_output,
        )

    async def run_path(
        self,
        path: Path,
        *,
        cwd: Path,
        skill: str,
        jail: Path,
        stdin: str | None = None,
        timeout: float = 10.0,
        stall_timeout: float = 0.0,
        label: str = "hook",
    ) -> ScriptResult:
        """Run a script by path, for hooks.

        Bypasses the ``scripts/`` restriction (a hook lives in ``hooks/``) but
        keeps every other control: interpreter allowlist, no shell, curated
        environment, timeout and process-group kill.

        ``jail`` is the directory the hook must resolve inside — the skill's own
        directory for a skill hook, the global hooks directory for a global one.
        Without it, a ``hooks/pre.py`` symlinked at any path on the box would be
        executed on every call to that skill.

        Callers must invoke this *outside* :meth:`run`, never nested inside it —
        both acquire the same semaphore, so nesting would deadlock. The server
        sequences pre-hook, script, post-hook one after another for that reason.
        """
        resolved = path.resolve()
        # Same jail as scripts. Hooks reach us as `skill_dir / "hooks/pre.py"`,
        # which resolve() follows through symlinks -- so without this check a
        # hooks/pre.py symlinked at /anything would execute that instead. Hooks
        # are more dangerous than scripts, not less: they run on every call.
        base = Path(jail).resolve()
        if not resolved.is_relative_to(base):
            raise ScriptError(f"hook escapes its directory ({base}): {path}")
        if not resolved.is_file():
            raise ScriptError(f"hook not found: {path}")
        interpreter = INTERPRETERS.get(resolved.suffix)
        if interpreter is None:
            raise ScriptError(f"no interpreter for hook {resolved.name}")
        return await self._execute(
            [*interpreter, str(resolved)],
            cwd=cwd,
            skill=skill,
            script_label=label,
            skill_root=None,
            stdin=stdin,
            timeout=timeout,
            stall_timeout=stall_timeout,
            env=None,
            on_output=None,
        )

    async def _execute(
        self,
        argv: list[str],
        *,
        cwd: Path,
        skill: str,
        script_label: str,
        skill_root: Path | None,
        stdin: str | None,
        timeout: float,
        stall_timeout: float,
        env: dict[str, str] | None,
        on_output: Callable[[str, str], Awaitable[None]] | None,
    ) -> ScriptResult:
        limit = timeout
        stall = stall_timeout
        meta_directory = cwd
        child_env = _child_env(
            meta_directory, skill, skill_root,
            pass_network_env=self.pass_network_env,
        )
        if self.output_budget_bytes:
            # A script that pages at the source beats one that dumps everything
            # and gets reduced afterwards: the reduction can only guess which
            # rows mattered, the script's own filter cannot.
            child_env["SKILL_OUTPUT_BUDGET_BYTES"] = str(self.output_budget_bytes)
        if env:
            for key in env:
                if not key.replace("_", "").isalnum():
                    raise ScriptError(f"invalid environment variable name {key!r}")
            check_caller_env(env)
            child_env.update({k: str(v) for k, v in env.items()})

        if stdin is not None and len(stdin.encode()) > MAX_STDIN_BYTES:
            raise ScriptError(
                f"stdin is {len(stdin.encode()):,} bytes, over the "
                f"{MAX_STDIN_BYTES:,} limit. Send it to your API in chunks, or "
                "have the script fetch it from a URL instead of receiving it inline."
            )

        state = _Capture(cap=self.output_cap_bytes)

        async with self._semaphore:
            self.launched += 1
            self._in_flight += 1
            started = time.monotonic()
            # _in_flight must come back down even when this coroutine is
            # cancelled -- which is exactly what uvicorn does to in-flight
            # requests on shutdown. Decrementing at the end of the happy path
            # leaked one per cancelled call, and a leaked counter makes drain()
            # wait out its full timeout on every subsequent shutdown.
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(meta_directory),
                    env=child_env,
                    start_new_session=True,  # own process group, so we can kill the tree
                )

                pumps = [
                    asyncio.create_task(_pump(proc.stdout, "stdout", state, on_output)),
                    asyncio.create_task(_pump(proc.stderr, "stderr", state, on_output)),
                ]
                waiter = asyncio.create_task(proc.wait())
                timed_out = stalled = False
                deadline = started + limit

                try:
                    if stdin is not None and proc.stdin is not None:
                        proc.stdin.write(stdin.encode())
                        await proc.stdin.drain()
                        proc.stdin.close()

                    while True:
                        now = time.monotonic()
                        slices = [deadline - now]
                        if stall > 0:
                            slices.append(state.last_activity + stall - now)
                        nap = min(slices)

                        if nap <= 0:
                            # Whichever budget expired first decides the verdict.
                            if now >= deadline:
                                timed_out = True
                            else:
                                stalled = True
                            break

                        done, _ = await asyncio.wait({waiter}, timeout=nap)
                        if done:
                            break
                finally:
                    if proc.returncode is None:
                        self._kill_tree(proc)
                        with_timeout = asyncio.wait_for(asyncio.shield(waiter), timeout=5)
                        try:
                            await with_timeout
                        except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                            logger.error("process %s survived SIGKILL", proc.pid)
                    # Readers normally finish on EOF. But EOF is not guaranteed:
                    # a script that spawns a grandchild without redirecting its
                    # output leaves that grandchild holding our stdout pipe, so the
                    # pipe stays open after our own child has exited. Waiting a
                    # fixed period here let such a call outlive the timeout the
                    # caller was promised (2 s requested, 5 s actual).
                    #
                    # So the wait is capped by whatever budget is left, with a small
                    # floor to let already-buffered output drain.
                    grace = max(0.25, min(2.0, deadline - time.monotonic()))
                    await asyncio.wait(pumps, timeout=grace)
                    for pump in pumps:
                        pump.cancel()

            finally:
                self._in_flight -= 1
            duration_ms = (time.monotonic() - started) * 1000
            silent_for = time.monotonic() - state.last_activity

        if timed_out:
            self.timeouts += 1
            status = "timeout"
        elif stalled:
            self.stalls += 1
            status = "stalled"
        elif proc.returncode == 0:
            status = "ok"
        else:
            self.failed += 1
            status = "failed"

        return ScriptResult(
            skill=skill,
            script=script_label,
            status=status,
            exit_code=proc.returncode,
            stdout=state.stdout.decode("utf-8", "replace"),
            stderr=state.stderr.decode("utf-8", "replace"),
            duration_ms=round(duration_ms, 2),
            timed_out=timed_out,
            stalled=stalled,
            truncated=state.truncated,
            silent_for_s=round(silent_for, 2) if (timed_out or stalled) else None,
            hint=self._diagnose(status, state, limit, stall) if status in ("timeout", "stalled") else None,
        )

    @staticmethod
    def _diagnose(status: str, state: _Capture, limit: float, stall: float) -> str:
        """Turn a kill into an actionable sentence, not just 'it timed out'."""
        where = (
            f" Last output: {state.last_line!r}."
            if state.last_line
            else " It produced no output at all."
        )
        if status == "stalled":
            return (
                f"Killed after {stall:.0f}s with no output.{where} "
                "A script that goes silent is almost always blocked on a network "
                "call with no timeout set on it. Set an explicit connect+read "
                "timeout on the HTTP client, and print a line before each call "
                "so the next failure says which endpoint. If the endpoint is "
                "reachable from a shell but not from here, check that the proxy "
                "variables the server forwards (HTTPS_PROXY, NO_PROXY) are set."
            )
        # A short ceiling is the author saying "this should answer immediately".
        # Overrunning it means the endpoint is doing the work before replying,
        # which a bigger timeout hides rather than fixes.
        if limit <= 30:
            return (
                f"Hit its {limit:.0f}s ceiling.{where} That ceiling says this script "
                "was expected to answer straight away -- typically it fires off work "
                "and returns a handle. Overrunning it usually means the endpoint "
                "finishes the work before replying, so the async boundary is on the "
                "wrong side. Raising the timeout would hide that, not fix it."
            )
        return (
            f"Hit the {limit:.0f}s ceiling while still producing output.{where} "
            "This one is genuinely slow rather than hung: raise `timeout`, or "
            "have the script page/stream its work instead of doing it in one call."
        )

    @staticmethod
    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def stats(self) -> dict[str, Any]:
        return {
            "launched": self.launched,
            "failed": self.failed,
            "timeouts": self.timeouts,
            "stalls": self.stalls,
            "max_concurrency": self._max_concurrency,
            "in_flight": self._in_flight,
            "slots_available": self._semaphore._value,  # noqa: SLF001 - diagnostics only
            "interpreters": sorted(INTERPRETERS),
            "network_env_forwarded": self.pass_network_env,
        }
