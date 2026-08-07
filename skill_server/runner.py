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
import resource
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
#:
#: HOME and TMPDIR are in the same identity bucket, not the RCE bucket: the
#: server means HOME to be the skill's own directory (see ``_child_env``), and
#: a caller that overrides it can redirect wherever a library resolves its own
#: config from (git, ssh, npm, ... all consult $HOME). TMPDIR travels with it
#: for the same reason -- both are "where does this script think it lives",
#: which is the server's call to make, not the caller's.
#:
#: The interpreters actually reachable here are python3 (-I, so most PYTHON*
#: vars are already neutralized -- PYTHONBREAKPOINT is listed anyway as
#: defence in depth against that flag ever being dropped), bash and sh.
#: PS4/SHELLOPTS/BASHOPTS are the bash-specific addition: `set -x` expands PS4
#: with command substitution on every traced line, so env={"PS4": "$(...)"} is
#: arbitrary execution the moment a script traces itself, and SHELLOPTS/
#: BASHOPTS are how a caller would otherwise switch tracing (or other
#: exec-relevant options) on from the environment before the script's own
#: `set` calls run.
_CALLER_FORBIDDEN_ENV = frozenset({
    "PATH", "SHELL", "IFS", "BASH_ENV", "ENV", "CDPATH", "GLOBIGNORE",
    "PS4", "SHELLOPTS", "BASHOPTS",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
    "PYTHONWARNINGS", "PYTHONINSPECT", "PYTHONBREAKPOINT",
    "NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE",
    "PERL5LIB", "PERL5OPT", "RUBYOPT", "RUBYLIB",
    "SKILL_NAME", "SKILL_DIR", "SKILL_ROOT", "SKILL_OUTPUT_BUDGET_BYTES",
    "HOME", "TMPDIR",
})
#: Any variable starting with one of these is refused: the dynamic-linker
#: families are the classic code-injection vector (LD_PRELOAD, DYLD_INSERT_LIBRARIES).
#: BASH_FUNC_ is the Shellshock (CVE-2014-6271) vector -- bash imports a
#: function definition from any environment variable named
#: ``BASH_FUNC_<name>%%``, executed the moment that bash process starts, i.e.
#: before our own script has run a single line.
_CALLER_FORBIDDEN_PREFIXES = ("LD_", "DYLD_", "_RLD", "BASH_FUNC_")

#: stdin is buffered in memory before being written to the child, so it needs a
#: ceiling for the same reason output does.
MAX_STDIN_BYTES = 4 * 1024 * 1024

#: A caller env value cannot legitimately need more than this -- it exists so
#: a pathological value cannot balloon the child's environment block for no
#: functional reason. Multi-line values (a PEM cert, a JSON blob) are fine;
#: this is just a ceiling, not a shape restriction.
MAX_ENV_VALUE_LEN = 32 * 1024

#: Control characters forbidden in a caller-supplied env *value*. NUL is the
#: hard case: it terminates a C string, so a NUL inside a value reaches
#: create_subprocess_exec and raises a bare ValueError that server.py does not
#: catch -- see check_caller_env_values. Tab/newline/CR are excluded: they are
#: ordinary bytes in a legitimate multi-line value, not a control channel.
_FORBIDDEN_VALUE_CHARS = frozenset(chr(c) for c in range(0x20) if c not in (0x09, 0x0A, 0x0D))


def check_arg_limits(args: Sequence[str]) -> None:
    """Reject an argv list that is too long or contains an oversized argument.

    Shared by the script path (``_build_argv``) and the hook path
    (``hooks.py``). The hook path used to skip this entirely: it JSON-encodes
    the same caller-supplied args into the pre-hook payload *before*
    ``_build_argv`` ever runs, so without this check here too, a skill with
    hooks accepted arbitrarily large args (up to the 4 MB stdin cap, applied
    only after the JSON was already built) while a hookless skill was capped
    at MAX_ARGS x MAX_ARG_LEN from the start.
    """
    if len(args) > MAX_ARGS:
        raise ScriptError(f"too many arguments ({len(args)} > {MAX_ARGS})")
    for arg in args:
        if len(str(arg)) > MAX_ARG_LEN:
            raise ScriptError(f"argument longer than {MAX_ARG_LEN} chars")


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


def check_caller_env_values(env: dict[str, str]) -> None:
    """Reject caller-supplied environment *values* that cannot safely reach exec().

    ``check_caller_env`` only looks at names. A value containing a NUL byte
    reaches ``create_subprocess_exec`` and raises a bare ``ValueError`` --
    server.py catches ``(ScriptError, SkillLoadError)`` around the call into
    the runner, so that ``ValueError`` used to crash the tool call outright
    instead of coming back as an ordinary error result.
    """
    for key, value in env.items():
        text = str(value)
        if len(text) > MAX_ENV_VALUE_LEN:
            raise ScriptError(
                f"environment variable {key!r} value is {len(text):,} chars, over "
                f"the {MAX_ENV_VALUE_LEN:,} limit"
            )
        if any(ch in _FORBIDDEN_VALUE_CHARS for ch in text):
            raise ScriptError(
                f"environment variable {key!r} contains a control character "
                "(e.g. NUL), which is not valid in a subprocess environment"
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


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    """Write caller stdin to the child, as a task the deadline loop supervises.

    This used to be a plain sequential ``write()`` + ``await drain()`` before
    the deadline loop even started. Past the ~64KiB pipe buffer, ``drain()``
    does not return until the child actually reads the data -- and most
    shipped skills never read stdin at all, since there is no reason for a
    script to expect it unless its own docs say so. So a large stdin payload
    sent to a script that ignores it used to block here regardless of
    ``timeout``, and once the child eventually exited, the broken write end
    raised ``BrokenPipeError`` straight out of ``run()`` -- a bare ``OSError``
    that server.py never catches, discarding whatever output had already been
    captured in the process.

    Running this as a supervised task fixes the sequencing; this function's
    job is to make sure the failure modes are sane on their own terms too:

    * A child that never reads stdin is not a bug in the child -- it is not
      an error, and the run must be judged on the child's own exit status and
      captured output, nothing else. BrokenPipeError/ConnectionResetError
      while writing are exactly that case and are swallowed here.
    * Anything else (a genuinely unexpected OSError) is re-raised as
      ScriptError -- the type server.py already catches around the call into
      the runner -- so it can never again escape as a bare OSError.
    """
    assert proc.stdin is not None
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except OSError as exc:
        raise ScriptError(f"failed writing stdin to the child: {exc}") from exc
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _build_preexec_fn(
    max_address_space_bytes: int | None,
    max_child_processes: int | None,
    max_file_size_bytes: int | None,
    max_cpu_seconds: int | None,
) -> Callable[[], None] | None:
    """Build the ``preexec_fn`` that caps the child's resources, or ``None``
    if every limit is disabled (all four ``None``).

    Why ``preexec_fn`` at all: it is the only in-process way to apply
    ``setrlimit`` to a child before it execs. The alternative -- shelling out
    to a wrapper like ``prlimit`` -- is an extra interpreter spawn on every
    single call, which is not acceptable at 0.5 CPU.

    Why that is risky, and how this contains the risk: ``preexec_fn`` runs
    after ``fork()``, in a copy of a process that has other threads (this
    server uses ``asyncio.to_thread`` elsewhere). Only the one thread that
    called ``fork()`` exists in the child; if any other thread held a lock
    at the moment of the fork -- the allocator arena lock, the import lock,
    the logging module's lock -- that lock is never released in the child,
    and anything in ``preexec_fn`` that needs it (an allocation, an import, a
    ``print``/``logger`` call, even an f-string building a new str object)
    can hang the child forever, silently, in a way that will not reproduce
    locally. So the function returned below calls ``resource.setrlimit`` and
    nothing else: no logging, no allocation beyond what the call itself
    needs, no f-strings. Do not add a print() here "just for debugging" --
    that is exactly the kind of change that turns an occasional flaky
    deadlock into a permanent one.

    Why per-limit try/except: RLIMIT_AS and RLIMIT_NPROC are Linux-only in
    practice -- macOS's kernel refuses to set RLIMIT_AS at all (raises
    ValueError even for generous values) and treats RLIMIT_NPROC as a
    per-user rather than per-process budget. A script must still run on a
    developer's laptop, so each limit is independent and best-effort: one
    platform refusing one limit must not stop the others from applying, and
    must never stop the child from running.
    """
    limits: list[tuple[int, int]] = []
    if max_address_space_bytes is not None:
        limits.append((resource.RLIMIT_AS, max_address_space_bytes))
    if max_child_processes is not None:
        limits.append((resource.RLIMIT_NPROC, max_child_processes))
    if max_file_size_bytes is not None:
        limits.append((resource.RLIMIT_FSIZE, max_file_size_bytes))
    if max_cpu_seconds is not None:
        limits.append((resource.RLIMIT_CPU, max_cpu_seconds))
    if not limits:
        return None

    def _preexec() -> None:
        # ASYNC-SIGNAL-SAFE ZONE -- see the docstring above. setrlimit calls
        # only. `limits` is already built; nothing here allocates beyond what
        # the loop and the call itself need, and nothing here can block on a
        # lock this (single-threaded, post-fork) process does not hold.
        for res, value in limits:
            try:
                resource.setrlimit(res, (value, value))
            except (OSError, ValueError):
                pass

    return _preexec


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
        # Resource limits applied to every child via preexec_fn (see
        # _build_preexec_fn). None disables that specific limit. Defaults are
        # sized for a 0.5 CPU / 512Mi pod running several scripts at once --
        # generous enough for real work, tight enough that one runaway script
        # cannot OOM-kill the whole pod instead of just failing its own call.
        #
        # RLIMIT_NPROC note: this is a per-*user*, not per-process, budget --
        # it counts every process (and on Linux, every thread) owned by the
        # server's UID, system-wide, not just this call's descendants. A
        # freshly started k8s pod's UID owns almost nothing else, so a modest
        # cap is a real, tight guard there. A shared dev machine or CI runner
        # can easily have several hundred processes under one UID already
        # (this repo's own test suite fails under a cap of 64 on an ordinary
        # laptop, for exactly that reason: every git subprocess the
        # repo-digest skill shells out to needs to fork, and so does the
        # test's own `subprocess.Popen` grandchild), and a script that
        # legitimately forks a handful of helpers (git, a grandchild it
        # deliberately detaches) needs headroom above whatever the box's
        # ambient count is, not above zero. So this default trades tightness
        # for portability: it still turns an unbounded fork bomb into a
        # bounded one instead of a pod-wide OOM, but it is not a tight cap on
        # a shared host. Set it low deliberately (with max_concurrency and
        # the host's typical ambient process count in mind) if the runtime
        # environment is a dedicated, freshly started container.
        # RLIMIT_AS caps *virtual* address space, not resident memory, and the
        # gap between the two is large: a bare CPython process on Linux
        # x86-64 maps a few hundred MB of VSZ before running a line of user
        # code (glibc reserves a 64 MB malloc arena per thread, plus the
        # shared libraries). So this cannot be set to "the memory a script
        # ought to need" -- at 192 MiB the interpreter would fail to start at
        # all, and the failure would be invisible during development because
        # macOS refuses to set RLIMIT_AS and silently skips it.
        #
        # 512 MiB is therefore a blast-radius bound, not a memory quota. What
        # it buys: `x = [0] * 10**9` wants ~8 GB and now dies as one failed
        # call instead of an OOM-kill that takes the server down with it.
        # What it does NOT buy: precise per-script accounting. Two concurrent
        # scripts can each sit under 512 MiB and still push a 512Mi pod into
        # OOM, so the pod's own memory limit remains the real backstop and
        # this is the cheap guard in front of it.
        #
        # UNVERIFIED ON LINUX: every rlimit here was exercised on macOS,
        # where RLIMIT_AS is not applied. Confirm a Python skill still runs
        # under this cap on the target image before trusting it in
        # production; lower it only with a measured VSZ figure in hand.
        max_address_space_bytes: int | None = 512 * 1024 * 1024,
        max_child_processes: int | None = 4096,
        # 0, not None: scripts have no legitimate reason to write a file in
        # this deployment (read-only root filesystem, no writable volume
        # handed to them), so this enforces "the server writes nothing" at
        # the kernel level instead of by convention. Verified against every
        # shipped skill under skills/ before picking 0 -- none of them write.
        max_file_size_bytes: int | None = 0,
        # None here means "derive from max_timeout" (see below), not
        # "disabled" -- unlike the other three, its sane default depends on a
        # sibling argument, so it cannot be a plain literal.
        max_cpu_seconds: int | None = None,
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

        self.max_address_space_bytes = max_address_space_bytes
        self.max_child_processes = max_child_processes
        self.max_file_size_bytes = max_file_size_bytes
        # RLIMIT_CPU is a second line of defence behind the wall-clock
        # timeout (it catches a busy-loop that is somehow deaf to SIGKILL's
        # process-group delivery) so it must never be the *first* thing to
        # fire for a call that is legitimately using the full timeout budget.
        # Deriving it from max_timeout keeps that true even if an operator
        # raises max_timeout without knowing this value exists; +30 is slack
        # for CPU accounting granularity, not a meaningful policy choice.
        self.max_cpu_seconds = (
            max_cpu_seconds if max_cpu_seconds is not None else int(max_timeout) + 30
        )
        self._preexec_fn = _build_preexec_fn(
            self.max_address_space_bytes,
            self.max_child_processes,
            self.max_file_size_bytes,
            self.max_cpu_seconds,
        )

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

        check_arg_limits(args)
        argv = [*interpreter, str(target)]
        for arg in args:
            argv.append(str(arg))
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
        # Same clamp as run(): "no caller can exceed max_timeout" is an
        # invariant of the runner, not a convention its one call site has to
        # remember. Today HOOK_TIMEOUT (10.0) is hardcoded and well under any
        # sane max_timeout, so this is a no-op in practice -- it is here so
        # that stays true if that ever changes.
        limit = min(timeout, self.max_timeout)
        if limit <= 0:
            raise ScriptError("timeout must be positive")
        return await self._execute(
            [*interpreter, str(resolved)],
            cwd=cwd,
            skill=skill,
            script_label=label,
            skill_root=None,
            stdin=stdin,
            timeout=limit,
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
            # Names were checked above; values were not -- a NUL byte in a
            # value reaches create_subprocess_exec and raises a bare
            # ValueError that server.py does not catch. Reject it here, as a
            # ScriptError, before it gets anywhere near exec().
            check_caller_env_values(env)
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
                    preexec_fn=self._preexec_fn,  # RLIMIT_* -- see _build_preexec_fn
                )

                pumps = [
                    asyncio.create_task(_pump(proc.stdout, "stdout", state, on_output)),
                    asyncio.create_task(_pump(proc.stderr, "stderr", state, on_output)),
                ]
                waiter = asyncio.create_task(proc.wait())
                timed_out = stalled = False
                deadline = started + limit

                # The stdin write runs as a task the deadline loop below
                # supervises, not as a sequential await before it starts. Past
                # the ~64KiB pipe buffer, drain() does not return until the
                # child reads it -- and most shipped skills never touch
                # stdin, so writing a large payload to one of them used to
                # block here until the child exited on its own, timeout be
                # damned (a 2s timeout measured a 60s overrun in practice).
                stdin_task: asyncio.Task[None] | None = None
                if stdin is not None and proc.stdin is not None:
                    stdin_task = asyncio.create_task(_feed_stdin(proc, stdin.encode()))

                try:
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

                        waitset = {waiter}
                        if stdin_task is not None and not stdin_task.done():
                            waitset.add(stdin_task)
                        done, _ = await asyncio.wait(waitset, timeout=nap)

                        if stdin_task in done:
                            # The write finished (or failed) -- on its own that
                            # is not a verdict, the child may well still be
                            # running. A broken pipe was already swallowed
                            # inside _feed_stdin (that is the normal case: a
                            # child that never reads stdin). Anything else
                            # stored on the task is a genuinely unexpected
                            # OSError that _feed_stdin re-raised as
                            # ScriptError; surface it the same way a build_argv
                            # failure would be surfaced.
                            exc = stdin_task.exception()
                            if exc is not None:
                                raise exc
                            done = done - {stdin_task}
                        if waiter in done:
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
                    drain_targets = list(pumps)
                    if stdin_task is not None and not stdin_task.done():
                        drain_targets.append(stdin_task)
                    await asyncio.wait(drain_targets, timeout=grace)
                    for pump in pumps:
                        pump.cancel()
                    if stdin_task is not None:
                        stdin_task.cancel()

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
            "resource_limits": {
                "max_address_space_bytes": self.max_address_space_bytes,
                "max_child_processes": self.max_child_processes,
                "max_file_size_bytes": self.max_file_size_bytes,
                "max_cpu_seconds": self.max_cpu_seconds,
            },
        }
