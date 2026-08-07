"""Pre- and post-execution hooks for skill scripts.

A hook is an ordinary script the skill author drops in the bundle:

    my-skill/
    ├── SKILL.md
    ├── hooks/
    │   ├── pre.py      # gate: may reject the call, or inject env
    │   └── post.py     # audit: may reject or rewrite the result
    └── scripts/

Both speak JSON over stdin/stdout, which keeps them language-agnostic and
runnable under the same sandbox as everything else — a hook gets no more
privilege than the script it guards.

**pre.py** receives ``{skill, script, args, caller}`` and decides:

    exit 0                    -> allow
    exit 0 + {"env": {...}}   -> allow, with extra environment for the script
    exit 0 + {"args": [...]}  -> allow, with rewritten arguments
    exit non-zero             -> deny; stderr (or {"reason"}) explains why

**post.py** receives ``{..., "result": <the script result>}`` and may:

    exit 0                       -> pass the result through unchanged
    exit 0 + {"result": {...}}   -> replace the result
    exit non-zero                -> fail the call; the reason reaches the caller

Cost, stated plainly: each hook is a process, so a skill with both hooks turns
one subprocess into three. That is the price of an enforced check and it is only
paid by skills that declare hooks — `list_skills` and `load_skill` never touch
this path, and skills without a `hooks/` directory are unaffected.

Global hooks (``--hooks-dir``) run for *every* skill, before and after the
skill's own. Use them for org-wide policy: audit logging, deny-lists, rate
limits. A global pre-hook that exits non-zero blocks the call outright.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skill_server.runner import check_arg_limits

logger = logging.getLogger(__name__)

#: Hooks are checks, not work. A hook that needs longer than this is doing
#: something it should not be doing inside a gate.
HOOK_TIMEOUT = 10.0


class HookDenied(Exception):
    """A hook refused the call. Carries the reason back to the caller."""

    def __init__(self, stage: str, source: str, reason: str):
        self.stage = stage
        self.source = source
        self.reason = reason
        super().__init__(f"{stage}-hook ({source}) denied this call: {reason}")


@dataclass(slots=True)
class HookOutcome:
    """What a pre-hook chain decided."""

    env: dict[str, str]
    args: list[str] | None
    notes: list[str]


def _parse(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # A hook that prints something non-JSON is not an error: silence and
        # chatter both mean "allow". Only the exit code decides.
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _reason(stdout: str, stderr: str) -> str:
    parsed = _parse(stdout)
    if isinstance(parsed.get("reason"), str):
        return parsed["reason"]
    return (stderr.strip() or stdout.strip() or "no reason given")[:500]


class HookRunner:
    """Executes hook scripts around a skill script.

    Deliberately holds no state: the resolved hook paths come from the index on
    every call, so hot-reloading a skill picks up a newly added hook without a
    restart, exactly like a changed body does.
    """

    def __init__(self, runner: Any, global_hooks_dir: Path | None = None):
        self._runner = runner
        self.global_hooks_dir = (
            Path(global_hooks_dir).expanduser().resolve() if global_hooks_dir else None
        )

    def _global(self, stage: str) -> Path | None:
        if self.global_hooks_dir is None:
            return None
        candidate = self.global_hooks_dir / f"{stage}.py"
        return candidate if candidate.is_file() else None

    def has_hooks(self, meta: Any) -> bool:
        return bool(meta.hooks) or self.global_hooks_dir is not None

    async def run_pre(
        self, meta: Any, script: str, args: list[str], caller: str | None
    ) -> HookOutcome:
        # _build_argv enforces MAX_ARGS/MAX_ARG_LEN on the caller's args, but
        # only once the script itself is about to run -- which for a skill
        # with hooks is *after* this payload has already been JSON-encoded
        # and shipped to the pre-hook's stdin. Without this, a skill with
        # hooks accepted caller args up to the 4MB stdin cap (checked only
        # after encoding, and before the concurrency semaphore is even
        # acquired) while a hookless skill was capped at 64 args x 4096
        # chars from the start. Apply the same cap here, before payload
        # construction, so both paths bound the caller identically.
        check_arg_limits(args)
        payload = {
            "skill": meta.name,
            "script": script,
            "args": list(args),
            "caller": caller,
            "skill_dir": str(meta.directory),
        }
        outcome = HookOutcome(env={}, args=None, notes=[])

        for source, path, jail in self._chain("pre", meta):
            result = await self._invoke(meta, path, payload, source, jail)
            if result.exit_code != 0:
                raise HookDenied("pre", source, _reason(result.stdout, result.stderr))
            decision = _parse(result.stdout)
            if isinstance(decision.get("env"), dict):
                outcome.env.update({str(k): str(v) for k, v in decision["env"].items()})
            if isinstance(decision.get("args"), list):
                outcome.args = [str(a) for a in decision["args"]]
                payload["args"] = outcome.args
            if isinstance(decision.get("note"), str):
                outcome.notes.append(f"{source}: {decision['note']}")
        return outcome

    async def run_post(
        self, meta: Any, script: str, args: list[str], result: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "skill": meta.name,
            "script": script,
            "args": list(args),
            "skill_dir": str(meta.directory),
            "result": result,
        }
        current = result
        # Skill hook first, then global: org-wide policy gets the last word and
        # sees whatever the skill's own hook produced.
        for source, path, jail in self._chain("post", meta, skill_first=True):
            payload["result"] = current
            hook_result = await self._invoke(meta, path, payload, source, jail)
            if hook_result.exit_code != 0:
                raise HookDenied("post", source, _reason(hook_result.stdout, hook_result.stderr))
            decision = _parse(hook_result.stdout)
            if isinstance(decision.get("result"), dict):
                current = decision["result"]
        return current

    def _chain(
        self, stage: str, meta: Any, skill_first: bool = False
    ) -> list[tuple[str, Path, Path]]:
        """Returns (source, hook path, jail the hook must resolve inside)."""
        chain: list[tuple[str, Path, Path]] = []
        skill_hook = meta.hooks.get(stage)
        skill_entry = (
            [("skill", meta.directory / skill_hook, meta.directory)] if skill_hook else []
        )
        global_entry = (
            [("global", g, self.global_hooks_dir)] if (g := self._global(stage)) else []
        )
        # pre: global gates first (cheapest rejection, org policy wins early).
        # post: skill shapes, then global audits what actually went out.
        chain.extend(skill_entry + global_entry if skill_first else global_entry + skill_entry)
        return chain

    async def _invoke(
        self, meta: Any, path: Path, payload: dict[str, Any], source: str, jail: Path
    ):
        return await self._runner.run_path(
            path,
            cwd=meta.directory,
            skill=meta.name,
            jail=jail,
            stdin=json.dumps(payload, ensure_ascii=False, default=str),
            timeout=HOOK_TIMEOUT,
            stall_timeout=0,  # a hook is short; the timeout alone is enough
            label=f"{source}-hook",
        )
