"""FastMCP server exposing skills with progressive disclosure.

The loading protocol the tools implement, and why it is shaped this way:

    list_skills()            -> ~30 tokens per skill  (name + description)
    load_skill(name)         -> the full body, once the model has chosen
    load_skill(name, section)-> one section of the body
    read_skill_file(...)     -> a reference file, only if the body points at it
    run_skill_script(...)    -> execute, instead of reading code into context

Level 1 is what every session pays for, so it is served from RAM with no I/O
and no per-request allocation beyond the response itself. Levels 2-4 are the
rare path and are allowed to touch the disk, in a worker thread.

Run it:

    uv run skill-mcp --skills ./skills --port 8000

For real throughput, run the ASGI app under multiple workers instead — see
``skill_server/app.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import contextlib
import logging
import os
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.utilities.tasks import TaskConfig
from pydantic import Field
from starlette.responses import JSONResponse, PlainTextResponse

from skill_server import shaping
from skill_server.hooks import HookDenied, HookRunner
from skill_server.index import SkillIndex, SkillLoadError
from skill_server.runner import ScriptError, ScriptRunner

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
MAX_FILE_BYTES = 512 * 1024


class TimingMiddleware(Middleware):
    """Per-tool latency histogram. Costs one ``perf_counter`` pair per call.

    Deliberately not a full metrics stack: this is here so ``skill_server_stats``
    can answer "is the hot path actually hot?" without adding a dependency.
    """

    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self._cap = 2048

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = getattr(context.message, "name", "<unknown>")
        started = time.perf_counter()
        try:
            return await call_next(context)
        except Exception:
            self.errors[name] += 1
            raise
        finally:
            bucket = self.samples[name]
            bucket.append((time.perf_counter() - started) * 1000)
            if len(bucket) > self._cap:
                del bucket[: len(bucket) // 2]

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, samples in self.samples.items():
            if not samples:
                continue
            ordered = sorted(samples)
            out[name] = {
                "calls": len(ordered),
                "errors": self.errors[name],
                "p50_ms": round(ordered[len(ordered) // 2], 3),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
                "max_ms": round(ordered[-1], 3),
            }
        return out


def _extract_section(body: str, section: str) -> str:
    """Return the markdown section whose heading matches ``section``.

    Matching is case-insensitive and prefix-based so the model does not have to
    reproduce a heading byte-for-byte.
    """
    wanted = section.strip().lower().lstrip("#").strip()
    lines = body.splitlines()
    start: int | None = None
    level = 0

    for i, line in enumerate(lines):
        if not line.startswith("#"):
            continue
        hashes = len(line) - len(line.lstrip("#"))
        title = line[hashes:].strip().lower()
        if start is None:
            if title.startswith(wanted):
                start, level = i, hashes
        elif hashes <= level:
            return "\n".join(lines[start:i]).strip()

    if start is None:
        headings = [ln.strip() for ln in lines if ln.startswith("#")][:20]
        raise ToolError(
            f"no section matching {section!r}. Available headings: {headings}"
        )
    return "\n".join(lines[start:]).strip()


def build_server(
    skill_roots: list[Path],
    *,
    refresh_interval: float = 5.0,
    max_script_concurrency: int = 8,
    script_timeout: float = 30.0,
    script_stall_timeout: float = 20.0,
    pass_network_env: bool = True,
    enable_tasks: bool = False,
    context_tokens: int = 128_000,
    context_share: float = 0.25,
    global_hooks_dir: Path | None = None,
    shutdown_grace: float = 25.0,
) -> FastMCP:
    index = SkillIndex(skill_roots)
    # Everything downstream is sized from the client's context window, not from
    # what the disk can produce. On a 30 K local model this is the difference
    # between a usable tool and one that stalls the model on every call.
    output_budget_bytes = shaping.budget_bytes_for(context_tokens, context_share)
    # The catalogue is paid for on *every* session, so it gets a tighter share
    # than a one-off tool result: it must leave room for the work itself.
    catalog_budget_bytes = shaping.budget_bytes_for(context_tokens, context_share / 2)
    runner = ScriptRunner(
        index,
        max_concurrency=max_script_concurrency,
        default_timeout=script_timeout,
        default_stall_timeout=script_stall_timeout,
        pass_network_env=pass_network_env,
        # Hard cap well above the budget: we still want enough captured to shape
        # from, just not unbounded.
        output_cap_bytes=max(output_budget_bytes * 8, 64 * 1024),
        output_budget_bytes=output_budget_bytes,
    )
    hooks = HookRunner(runner, global_hooks_dir)
    timing = TimingMiddleware()

    @contextlib.asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        """Keep the index warm from a background task, never from a request."""

        async def refresher() -> None:
            while True:
                await asyncio.sleep(refresh_interval)
                try:
                    if await asyncio.to_thread(index.refresh):
                        logger.info("skills reindexed -> generation %d", index.generation)
                except Exception:  # pragma: no cover - a bad tree must not kill the loop
                    logger.exception("skill refresh failed")

        task = asyncio.create_task(refresher()) if refresh_interval > 0 else None
        logger.info("serving %d skills from %s", len(index), [str(r) for r in index.roots])
        try:
            yield {"index": index, "runner": runner}
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Let in-flight scripts finish before the process goes away. On a
            # rolling update this is the difference between a submitted job
            # reporting its uuid and that uuid being lost while the work runs on.
            remaining = await runner.drain(shutdown_grace)
            logger.info("shutdown complete (%d script(s) abandoned)", remaining)

    mcp = FastMCP(
        name="skill-server",
        version="0.1.0",
        instructions=(
            "Skill library with progressive disclosure. Always call list_skills first "
            "and read only the descriptions; then call load_skill(name) for the one "
            "skill you actually need. Do not load skills speculatively. If a skill "
            "bundles a script, prefer run_skill_script over reading the script's "
            "source into context."
        ),
        middleware=[timing],
        lifespan=lifespan,
    )

    # ---------------------------------------------------------------- level 1

    @mcp.tool(
        annotations={"readOnlyHint": True, "idempotentHint": True},
        # Served from memory; a thread hop would cost more than the work itself.
        run_in_thread=False,
    )
    async def list_skills(
        query: Annotated[
            str | None,
            Field(description="Optional free-text filter over name, tags and description."),
        ] = None,
        tags: Annotated[
            list[str] | None, Field(description="Only skills carrying all of these tags.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """List available skills as compact cards (name, description, tags, scripts).

        This is the entry point: it never returns skill bodies, so listing 200
        skills costs a few thousand tokens rather than a few hundred thousand.
        Pick one and call load_skill.
        """
        budget = catalog_budget_bytes

        def size(payload: Any) -> int:
            return len(json.dumps(payload, ensure_ascii=False).encode())

        # 無篩選時以**完整目錄**判斷，不能用 limit 截斷後的結果——預設
        # limit=50 會讓它永遠看似放得下，總覽就再也不會啟用。
        cards = index.catalog(
            query=query, tags=tags,
            limit=len(index) if not query and not tags else limit,
        )

        # 沒有篩選條件、而且完整目錄放不下時，改回傳「領域總覽」而不是
        # 字母序的前 N 個。
        #
        # 直接截斷會讓整個領域對模型隱形：實測 315 個 skill 時，模型只
        # 看得到字母序前 6 個領域，另外 15 個完全不知道存在——而且它沒有
        # 辦法發現自己漏了什麼。總覽用差不多的 token 數涵蓋全部領域。
        if not query and not tags and size(cards) > budget:
            facets = index.facets()
            samples = index.sample_per_facet(per=1, limit=40)
            while samples and size(samples) > budget // 2:
                samples.pop()
            return {
                "count": len(samples),
                "total": len(index),
                "view": "overview",
                "facets": facets,
                "skills": samples,
                "hint": (
                    f"目錄有 {len(index)} 個 skill，放不進 context，因此改為領域"
                    f"總覽：facets 是「領域 -> 數量」，skills 是每個領域的一個代表。"
                    f"用 tags=['<領域>'] 或 query='<關鍵字>' 取得該領域的完整清單。"
                ),
            }

        cards = cards[:limit]
        dropped = 0
        while cards and size(cards) > budget:
            cards.pop()
            dropped += 1

        result: dict[str, Any] = {
            "count": len(cards), "total": len(index), "view": "list", "skills": cards,
        }
        if dropped:
            result["truncated"] = {
                "omitted": dropped,
                "hint": "結果超過 context 預算。用更精確的 query 或 tags 縮小範圍，"
                        "不要調大 limit——被省略的 skill 對你完全不可見。",
            }
        return result

    # ---------------------------------------------------------------- level 2

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def load_skill(
        name: Annotated[str, Field(description="Exact skill name from list_skills.")],
        section: Annotated[
            str | None,
            Field(description="Load only this markdown heading's section, e.g. 'Usage'."),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Load a skill's instructions. Cached in memory and revalidated by mtime.

        Pass `section` when you only need one part of a long skill — it returns
        that heading's content instead of the whole document.
        """
        try:
            meta = index.get(name)
            body = await asyncio.to_thread(index.body, name)
        except SkillLoadError as exc:
            raise ToolError(str(exc)) from exc

        if section:
            body = _extract_section(body, section)
        body, body_shape = shaping.shape(body, output_budget_bytes)
        if ctx is not None:
            await ctx.debug(f"loaded skill {name!r} ({len(body)} chars)")

        return {
            "name": meta.name,
            "output": body_shape,
            "allowed_tools": list(meta.allowed_tools),
            "description": meta.description,
            "version": meta.version,
            "tags": list(meta.tags),
            "body": body,
            "scripts": list(meta.scripts),
            "files": list(meta.files),
        }

    # ---------------------------------------------------------------- level 3

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def read_skill_file(
        name: Annotated[str, Field(description="Skill that owns the file.")],
        path: Annotated[
            str, Field(description="Path relative to the skill directory, e.g. 'references/api.md'.")
        ],
        max_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)] = 64 * 1024,
    ) -> dict[str, Any]:
        """Read a file bundled with a skill (references, templates, schemas).

        Only paths inside that skill's own directory are readable.
        """
        try:
            target = index.resolve_file(name, path)
        except SkillLoadError as exc:
            raise ToolError(str(exc)) from exc

        def _read() -> tuple[str, bool, int]:
            size = target.stat().st_size
            with target.open("rb") as fh:
                raw = fh.read(max_bytes)
            return raw.decode("utf-8", "replace"), size > max_bytes, size

        content, truncated, size = await asyncio.to_thread(_read)
        # Same context budget as script output. A 400 KB reference file is
        # ~114k tokens: on a 30k model that is unusable, and it arrives looking
        # like a successful read rather than an error.
        content, shape_info = shaping.shape(content, output_budget_bytes)
        return {
            "skill": name,
            "path": path,
            "bytes": size,
            "truncated": truncated or shape_info.get("shaped", False),
            "output": shape_info,
            "content": content,
        }

    # ---------------------------------------------------------------- level 4

    # Background-task mode lets a long API-calling script outlive the client's
    # own request timeout: the client gets a handle immediately and polls. It is
    # opt-in because it needs `fastmcp[tasks]`, which pulls in pydocket + Redis —
    # too heavy to impose when stall detection already covers the common case.
    _task_config = (
        TaskConfig(mode="optional", poll_interval=timedelta(seconds=2))
        if enable_tasks
        else None
    )

    @mcp.tool(
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        task=_task_config,
    )
    async def run_skill_script(
        name: Annotated[str, Field(description="Skill that owns the script.")],
        script: Annotated[
            str, Field(description="Script path from the skill's `scripts` list, e.g. 'scripts/x.py'.")
        ],
        args: Annotated[list[str] | None, Field(description="Argv passed to the script.")] = None,
        stdin: Annotated[str | None, Field(description="Text piped to the script's stdin.")] = None,
        timeout: Annotated[
            float | None,
            Field(gt=0, le=900, description="Hard ceiling in seconds before the process is killed."),
        ] = None,
        stall_timeout: Annotated[
            float | None,
            Field(
                ge=0,
                le=600,
                description="Kill early after this many seconds with no output at all. "
                "0 disables. Default 20.",
            ),
        ] = None,
        env: Annotated[
            dict[str, str] | None,
            Field(description="Extra environment variables for this run, e.g. {'API_BASE': '...'}."),
        ] = None,
        max_output_tokens: Annotated[
            int | None,
            Field(
                ge=200,
                le=100_000,
                description="Shrink output to roughly this many tokens. JSON is reduced "
                "structurally (kept valid) rather than cut mid-object. Defaults to the "
                "server's context budget.",
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run a script that ships with a skill, and return its output.

        Prefer this over reading a script's source and re-implementing it: the
        script is the tested path, and running it keeps its code out of context.
        Only files under a skill's own `scripts/` directory can be executed;
        there is no shell, so arguments need no quoting or escaping.

        **Whatever the script printed is the answer.** If it fired off a job and
        printed a uuid, that uuid is the result — hand it back to the caller and
        stop; the work continues on its own. If it waited and printed data, that
        data is the result. The server does not interpret either case.

        **Everything printed is returned even when the script is killed.**
        Check `status` rather than `exit_code`:

        - `ok`      — exited 0.
        - `failed`  — exited non-zero; read `stderr`.
        - `stalled` — went silent for `stall_timeout`. Almost always a network
          call with no timeout set on it. `stdout` shows how far it got and
          `hint` says what to do; do not simply retry with a bigger timeout.
        - `timeout` — still working at the ceiling. See `hint`.
        """
        async def stream(which: str, line: str) -> None:
            # Progress, while the script is still running, so a slow call is
            # visibly alive instead of an opaque wait.
            #
            # stderr only: stdout is the result and is returned in full anyway,
            # so streaming it too would duplicate the entire payload into the
            # client's log. This is why the convention is progress->stderr,
            # results->stdout.
            if ctx is not None and which == "stderr" and line.strip():
                await ctx.info(f"[{name}] {line[:500]}")

        try:
            meta = index.get(name)
        except SkillLoadError as exc:
            raise ToolError(str(exc)) from exc

        policy = meta.policy_for(script)
        argv = list(args or ())
        # The skill declares what its endpoint does; the caller may still
        # override, but never has to know in order to get sane defaults.
        effective_timeout = timeout or policy.timeout
        effective_stall = stall_timeout if stall_timeout is not None else policy.stall_timeout
        run_env = dict(env or {})
        hook_notes: list[str] = []

        # Sequenced, never nested: pre-hook, script, post-hook each take the
        # concurrency semaphore in turn (nesting them would deadlock).
        try:
            if hooks.has_hooks(meta):
                decision = await hooks.run_pre(
                    meta, script, argv, ctx.client_id if ctx else None
                )
                run_env.update(decision.env)
                if decision.args is not None:
                    argv = decision.args
                hook_notes.extend(decision.notes)

            result = await runner.run(
                name,
                script,
                argv,
                stdin=stdin,
                timeout=effective_timeout,
                stall_timeout=effective_stall,
                env=run_env or None,
                on_output=stream if ctx is not None else None,
            )
        except HookDenied as exc:
            raise ToolError(str(exc)) from exc
        except (ScriptError, SkillLoadError) as exc:
            raise ToolError(str(exc)) from exc

        if ctx is not None and result.status in ("stalled", "timeout"):
            await ctx.warning(f"{name}/{script} {result.status}: {result.hint}")

        # Fit the result to the context window. This is the step that decides
        # whether a 200 KB REST response is usable or fatal on a small model.
        budget = (
            int(max_output_tokens * shaping.BYTES_PER_TOKEN)
            if max_output_tokens
            else output_budget_bytes
        )
        payload = result.to_dict()
        if hook_notes:
            payload["hook_notes"] = hook_notes

        payload["stdout"], shape_info = shaping.shape(result.stdout, budget)
        # stderr is progress/diagnostics: keep a slice, never the whole log.
        payload["stderr"], _ = shaping.shape(result.stderr, min(budget // 4, 8000))
        payload["output"] = shape_info
        if hooks.has_hooks(meta):
            try:
                payload = await hooks.run_post(meta, script, argv, payload)
            except HookDenied as exc:
                raise ToolError(str(exc)) from exc

        if shape_info.get("shaped") and ctx is not None:
            await ctx.info(
                f"[{name}] output reduced "
                f"{shape_info.get('original_approx_tokens')}→{shape_info['approx_tokens']} tokens"
            )
        return payload

    # --------------------------------------------------------------- operations

    @mcp.tool(annotations={"readOnlyHint": True}, run_in_thread=False)
    async def skill_server_stats() -> dict[str, Any]:
        """Index state, subprocess counters and per-tool latency percentiles."""
        return {
            "index": index.stats(),
            "runner": runner.stats(),
            "tools": timing.snapshot(),
            "hooks": {"global_dir": str(hooks.global_hooks_dir or "")},
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
    async def reload_skills() -> dict[str, Any]:
        """Re-scan the skill directories now, without restarting the server.

        Skills are already picked up automatically within the refresh interval;
        this is for when you have just written one and do not want to wait, or
        when the interval is disabled. Adding, editing or removing a skill --
        including its scripts, hooks and execution policy -- never needs a
        restart.
        """
        before = index.generation
        names_before = {c["name"] for c in index.catalog(limit=1000)}
        changed = await asyncio.to_thread(index.refresh, force=True)
        names_after = {c["name"] for c in index.catalog(limit=1000)}
        return {
            "changed": changed,
            "generation": f"{before} -> {index.generation}",
            "skills": len(index),
            "added": sorted(names_after - names_before),
            "removed": sorted(names_before - names_after),
        }

    # ------------------------------------------------------- operational HTTP
    # Plain HTTP, deliberately outside MCP: a k8s probe cannot speak JSON-RPC,
    # and an operator debugging at 3am should be able to curl this. Note that
    # GET /mcp returns 405 (the MCP endpoint only accepts POST), so pointing a
    # readiness probe at it makes the pod permanently NotReady.

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):  # noqa: ARG001
        """Liveness: the process is up and the event loop is responsive."""
        return JSONResponse({"status": "ok", "skills": len(index)})

    @mcp.custom_route("/ready", methods=["GET"])
    async def ready(request):  # noqa: ARG001
        """Readiness: refuse traffic until at least one skill is actually served.

        An empty index means a misconfigured --skills path or a tree where every
        skill was rejected. Serving that is worse than not serving: the model
        gets an empty catalogue and concludes the tools do not exist.
        """
        rejected = index.stats()["rejected"]
        healthy = len(index) > 0
        return JSONResponse(
            {
                "status": "ready" if healthy else "no skills loaded",
                "skills": len(index),
                "rejected": len(rejected),
                "generation": index.generation,
            },
            status_code=200 if healthy else 503,
        )

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(request):  # noqa: ARG001
        """Prometheus text format. Enough to alert on, not a full metrics stack."""
        lines = [
            "# HELP skill_mcp_skills Number of skills currently served.",
            "# TYPE skill_mcp_skills gauge",
            f"skill_mcp_skills {len(index)}",
            "# HELP skill_mcp_skills_rejected Skills on disk that failed validation.",
            "# TYPE skill_mcp_skills_rejected gauge",
            f"skill_mcp_skills_rejected {len(index.stats()['rejected'])}",
            "# HELP skill_mcp_scripts_total Scripts launched since start.",
            "# TYPE skill_mcp_scripts_total counter",
            f"skill_mcp_scripts_total {runner.launched}",
            "# HELP skill_mcp_scripts_failed_total Scripts that exited non-zero.",
            "# TYPE skill_mcp_scripts_failed_total counter",
            f"skill_mcp_scripts_failed_total {runner.failed}",
            "# HELP skill_mcp_scripts_timeout_total Scripts killed at the ceiling.",
            "# TYPE skill_mcp_scripts_timeout_total counter",
            f"skill_mcp_scripts_timeout_total {runner.timeouts}",
            "# HELP skill_mcp_scripts_stalled_total Scripts killed for going silent.",
            "# TYPE skill_mcp_scripts_stalled_total counter",
            f"skill_mcp_scripts_stalled_total {runner.stalls}",
            "# HELP skill_mcp_script_slots_free Free slots in the concurrency semaphore.",
            "# TYPE skill_mcp_script_slots_free gauge",
            f"skill_mcp_script_slots_free {runner.stats()['slots_available']}",
        ]
        for tool, stat in timing.snapshot().items():
            safe = tool.replace("-", "_")
            lines += [
                f'skill_mcp_tool_calls_total{{tool="{safe}"}} {stat["calls"]}',
                f'skill_mcp_tool_errors_total{{tool="{safe}"}} {stat["errors"]}',
                f'skill_mcp_tool_latency_p50_ms{{tool="{safe}"}} {stat["p50_ms"]}',
                f'skill_mcp_tool_latency_p95_ms{{tool="{safe}"}} {stat["p95_ms"]}',
            ]
        return PlainTextResponse("\n".join(lines) + "\n")

    # ----------------------------------------------------------------- resources
    # Same data as the tools, for clients that prefer attaching resources over
    # spending a tool call.

    @mcp.resource("skill://{name}", mime_type="text/markdown")
    async def skill_resource(name: str) -> str:
        try:
            return await asyncio.to_thread(index.body, name)
        except SkillLoadError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.resource("skill://{name}/files/{path*}", mime_type="text/plain")
    async def skill_file_resource(name: str, path: str) -> str:
        try:
            target = index.resolve_file(name, path)
        except SkillLoadError as exc:
            raise ToolError(str(exc)) from exc
        return await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")

    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="skill-mcp", description=__doc__)
    parser.add_argument(
        "--skills",
        action="append",
        type=Path,
        metavar="DIR",
        help="Skill root directory; repeat for several (default: ./skills)",
    )
    parser.add_argument("--host", default=os.getenv("SKILL_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SKILL_MCP_PORT", "8000")))
    parser.add_argument("--path", default="/mcp", help="HTTP path to mount on")
    parser.add_argument(
        "--stateful",
        action="store_true",
        help="Keep per-session state (needed for sampling/elicitation). Default is "
        "stateless, which is faster and lets you run multiple workers.",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="Stream responses over SSE instead of plain JSON replies.",
    )
    parser.add_argument("--refresh-interval", type=float, default=5.0)
    parser.add_argument("--max-script-concurrency", type=int, default=8)
    parser.add_argument("--script-timeout", type=float, default=30.0)
    parser.add_argument(
        "--script-stall-timeout",
        type=float,
        default=20.0,
        help="Kill a script that produces no output for this long (0 disables). "
        "Catches hung network calls in seconds instead of at the full timeout.",
    )
    parser.add_argument(
        "--no-network-env",
        action="store_true",
        help="Do not forward proxy/TLS variables (HTTPS_PROXY, NO_PROXY, "
        "REQUESTS_CA_BUNDLE, ...) to scripts. They are forwarded by default "
        "because without them outbound API calls hang behind a corporate proxy.",
    )
    parser.add_argument(
        "--enable-tasks",
        action="store_true",
        help="Allow clients to run scripts as background tasks and poll for the "
        "result, for scripts that outlive the client's request timeout. "
        "Requires: uv sync --extra tasks (pulls in pydocket + Redis).",
    )
    parser.add_argument(
        "--hooks-dir",
        type=Path,
        default=None,
        help="Directory holding global pre.py / post.py that run for EVERY skill, "
        "around the skill's own hooks. For org-wide policy: audit logging, "
        "deny-lists, rate limits.",
    )
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=128_000,
        help="Client's context window. Script output is shaped to fit a share "
        "of it. Set this to your model's real limit -- on a 30k local model the "
        "128k default will hand back output the model cannot digest.",
    )
    parser.add_argument(
        "--context-share",
        type=float,
        default=0.25,
        help="Fraction of the context window a single tool result may occupy.",
    )
    parser.add_argument(
        "--shutdown-grace",
        type=float,
        default=25.0,
        help="關機時等待執行中 script 的秒數。要小於 k8s 的 "
        "terminationGracePeriodSeconds，否則 SIGKILL 會先到。",
    )
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    with contextlib.suppress(ImportError):  # optional: ~20-30% more req/s
        import uvloop

        uvloop.install()

    roots = args.skills or [DEFAULT_SKILLS_DIR]
    mcp = build_server(
        roots,
        refresh_interval=args.refresh_interval,
        max_script_concurrency=args.max_script_concurrency,
        script_timeout=args.script_timeout,
        script_stall_timeout=args.script_stall_timeout,
        pass_network_env=not args.no_network_env,
        enable_tasks=args.enable_tasks,
        context_tokens=args.context_tokens,
        context_share=args.context_share,
        global_hooks_dir=args.hooks_dir,
        shutdown_grace=args.shutdown_grace,
    )
    mcp.run(
        transport="http",
        # uvicorn 才是真正決定 in-flight 請求命運的一方：它會在呼叫 lifespan
        # 的 shutdown 之前就取消未完成的請求。所以 runner.drain() 只是最後
        # 防線，真正讓執行中的 script 有機會跑完的是這個設定。
        # 值要小於 k8s 的 terminationGracePeriodSeconds，否則 SIGKILL 會先到。
        uvicorn_config={"timeout_graceful_shutdown": int(args.shutdown_grace)},
        host=args.host,
        port=args.port,
        path=args.path,
        # json_response=True skips the SSE framing on every reply; stateless_http
        # drops the per-session bookkeeping so any worker can serve any request.
        json_response=not args.sse,
        stateless_http=not args.stateful,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
