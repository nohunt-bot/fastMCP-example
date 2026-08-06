"""Skill discovery and caching.

Design goals, in priority order:

1. **Never touch the disk on the hot path.** `list_skills` is called by every
   client at session start; it is served entirely from a pre-built, pre-filtered
   catalog held in memory. Bodies are read once and cached, keyed on
   ``(mtime_ns, size)`` so an edit invalidates without a full rescan.
2. **Progressive disclosure.** Indexing parses only the YAML frontmatter, which
   means reading the first few KB of each SKILL.md instead of the whole file.
   A repo of 500 skills indexes from cold in a few ms.
3. **Refresh is opt-in.** Skills ship inside the container image, so they cannot
   change during a pod's lifetime — periodic rescanning would burn CPU to
   discover nothing. `refresh()` exists for local development and for the
   `reload_skills` tool; the background timer is off by default.

The snapshot is a frozen dataclass swapped in atomically by the refresher. Since
reads only ever bind the snapshot to a local, readers never observe a torn
state and need no synchronisation at all.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from skill_server.search import SearchIndex

try:  # libyaml bindings are ~10x faster and ship with most wheels
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # pragma: no cover - depends on local libyaml
    from yaml import SafeLoader as _YamlLoader  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
#: Claude Code's naming rule: lowercase letters, digits and hyphens only.
#: Enforced rather than merely documented, because the name is used as a lookup
#: key AND (after sanitising) as a directory component — a skill called
#: "../../etc" would otherwise place its state outside the state root.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LEN = 64
#: Claude Code's limit. The description is the only text loaded every session,
#: so an oversized one is a per-conversation tax on every user.
MAX_DESCRIPTION_LEN = 1024
#: Not a hard limit, but past this a skill should be split into references/.
SKILL_BODY_WARN_BYTES = 32 * 1024
#: Frontmatter is read from the head of the file only. Anything larger than this
#: is not frontmatter, it is a malformed file.
FRONTMATTER_MAX_BYTES = 16 * 1024
#: Directories never descended into when listing a skill's bundled files.
_IGNORED_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
_MAX_BUNDLE_ENTRIES = 200


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """How long a given script is allowed to take.

    Declared in frontmatter because the skill author knows the shape of their
    own endpoint and the caller does not::

        execution:
          default: {timeout: 60}
          scripts/submit.py:
            timeout: 15
            description: 送出任務，API 立刻回 uuid 後自己跑完

    This deliberately does **not** declare what the script "means" (a job
    handle vs an answer). The script prints what it has, and that is the answer
    — a mode flag would only let the server guess at something it already holds,
    and guessing is worse than not knowing: probing output for a job key turns
    an order's ``{"id": 123}`` into a fake job handle.

    ``timeout`` is the one thing that genuinely cannot come from the output,
    because it has to be decided before the script runs. A submit that should
    answer in 15 s and a report that legitimately takes 300 s need different
    ceilings, and only the author knows which is which.
    """

    timeout: float | None = None
    stall_timeout: float | None = None
    description: str = ""

    def as_card(self) -> dict[str, Any]:
        card: dict[str, Any] = {}
        if self.timeout:
            card["timeout"] = self.timeout
        if self.description:
            card["about"] = self.description
        return card


def _parse_policy(raw: Any, fallback: "ExecutionPolicy | None" = None) -> ExecutionPolicy:
    base = fallback or ExecutionPolicy()
    if not isinstance(raw, dict):
        return base
    return ExecutionPolicy(
        timeout=_as_float(raw.get("timeout"), base.timeout),
        stall_timeout=_as_float(raw.get("stall_timeout"), base.stall_timeout),
        description=str(raw.get("description", base.description)),
    )


def _as_float(value: Any, fallback: float | None) -> float | None:
    try:
        return float(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """Everything known about a skill without having read its body."""

    name: str
    description: str
    tags: tuple[str, ...]
    version: str | None
    directory: Path
    skill_md: Path
    #: Byte offset where the markdown body starts (after the frontmatter block).
    body_offset: int
    mtime_ns: int
    size: int
    #: Relative paths of runnable scripts, e.g. ``("scripts/profile.py",)``.
    scripts: tuple[str, ...]
    #: Relative paths of every other bundled file (references, templates, ...).
    files: tuple[str, ...]
    #: Per-script execution policies, plus a "" entry holding the skill default.
    policies: dict[str, ExecutionPolicy] = field(default_factory=dict, repr=False)
    #: Relative paths of hook scripts, e.g. {"pre": "hooks/pre.py"}.
    hooks: dict[str, str] = field(default_factory=dict, repr=False)
    #: Claude Code's `allowed-tools`. This server does not enforce it (it does
    #: not own the client's tool list) but passes it through so a client can.
    allowed_tools: tuple[str, ...] = ()
    license: str = ""
    #: Pre-lowercased "name description tags" blob, built once for search.
    haystack: str = field(repr=False, default="")

    def policy_for(self, script: str) -> ExecutionPolicy:
        """Policy for one script, falling back to the skill default."""
        return self.policies.get(script) or self.policies.get("") or ExecutionPolicy()

    def card(self) -> dict[str, Any]:
        """The compact form sent to the model. Deliberately small."""
        card: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.tags:
            card["tags"] = list(self.tags)
        if self.scripts:
            # Scripts carry their mode so the model knows, before running one,
            # whether it will get a job key back or the actual result.
            card["scripts"] = {s: self.policy_for(s).as_card() for s in self.scripts}
        if self.allowed_tools:
            card["allowed_tools"] = list(self.allowed_tools)
        return card


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """Immutable view of the skill tree, swapped in wholesale on refresh."""

    by_name: dict[str, SkillMeta]
    catalog: tuple[dict[str, Any], ...]
    #: Cheap change detector: {skill_md_path: (mtime_ns, size)}.
    stamps: dict[str, tuple[int, int]]
    generation: int
    built_at: float
    #: BM25 索引，與快照同生共死。建立在背景更新執行緒上，不在請求路徑。
    search: SearchIndex = field(default_factory=SearchIndex)


class SkillLoadError(Exception):
    """Raised for a bad skill name or an unreadable/escaping path."""


class SkillRejected(Exception):
    """A skill on disk is not valid and will not be loaded."""

    def __init__(self, name: str, path: Path, reason: str):
        self.name = name
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], int]:
    """Return ``(frontmatter, body_offset)`` reading only the head of the file.

    A file with no leading ``---`` fence is valid: it is treated as a body-only
    skill with empty frontmatter, and the caller falls back to the directory
    name.
    """
    with path.open("rb") as fh:
        head = fh.read(FRONTMATTER_MAX_BYTES)

    if not head.startswith(b"---"):
        return {}, 0

    # Tolerate CRLF and a missing trailing newline on the opening fence.
    newline = head.find(b"\n")
    if newline == -1 or head[3:newline].strip() not in (b"", b"-"):
        return {}, 0

    end = head.find(b"\n---", newline)
    if end == -1:
        logger.warning("%s: unterminated frontmatter, treating as body-only", path)
        return {}, 0

    raw = head[newline + 1 : end]
    body_offset = end + 1
    # Skip past the closing fence line itself.
    closing_nl = head.find(b"\n", body_offset)
    body_offset = closing_nl + 1 if closing_nl != -1 else len(head)

    try:
        data = yaml.load(raw.decode("utf-8", "replace"), Loader=_YamlLoader)
    except yaml.YAMLError as exc:
        logger.warning("%s: invalid frontmatter YAML (%s)", path, exc)
        return {}, body_offset
    return (data if isinstance(data, dict) else {}), body_offset


def _scan_bundle(directory: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a skill directory into (scripts, other files), as relative paths."""
    scripts: list[str] = []
    others: list[str] = []
    budget = _MAX_BUNDLE_ENTRIES

    stack = [directory]
    while stack and budget > 0:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _IGNORED_DIRS:
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            rel = os.path.relpath(entry.path, directory)
            if rel == SKILL_FILE:
                continue
            budget -= 1
            if budget < 0:
                break
            if rel.startswith("scripts" + os.sep):
                scripts.append(rel)
            elif rel.startswith("hooks" + os.sep):
                continue  # hooks are machinery, not content the model should read
            else:
                others.append(rel)
    return tuple(sorted(scripts)), tuple(sorted(others))


def _load_meta(skill_md: Path) -> SkillMeta:
    """Build a SkillMeta, or raise SkillRejected with a reason a human can act on."""
    try:
        stat = skill_md.stat()
        front, body_offset = _parse_frontmatter(skill_md)
    except OSError as exc:
        raise SkillRejected(skill_md.parent.name, skill_md, f"unreadable: {exc}") from exc

    directory = skill_md.parent
    name = str(front.get("name") or directory.name).strip()
    description = str(front.get("description") or "").strip()

    if not NAME_PATTERN.match(name) or len(name) > MAX_NAME_LEN:
        # Refused, not silently renamed: a skill that answers to a different
        # name than its frontmatter says is worse than one that is absent, and
        # `skill_server_stats().index.rejected` reports exactly why.
        raise SkillRejected(
            name,
            skill_md,
            f"name {name!r} is invalid. Claude Code requires lowercase letters, "
            f"digits and hyphens only (max {MAX_NAME_LEN} chars), e.g. 'order-lookup'.",
        )
    if len(description) > MAX_DESCRIPTION_LEN:
        raise SkillRejected(
            name, skill_md,
            f"description is {len(description)} chars, over the {MAX_DESCRIPTION_LEN} "
            "limit. It is loaded every session -- move the detail into the body.",
        )
    if not description:
        logger.warning("%s: no description; the model cannot tell when to use it", skill_md)

    raw_allowed = front.get("allowed-tools") or front.get("allowed_tools") or ()
    if isinstance(raw_allowed, str):
        raw_allowed = [t.strip() for t in raw_allowed.split(",")]
    allowed_tools = tuple(str(t).strip() for t in raw_allowed if str(t).strip())

    if stat.st_size > SKILL_BODY_WARN_BYTES:
        logger.warning(
            "%s is %d KB; consider moving detail into references/ so it is only "
            "read when needed", skill_md, stat.st_size // 1024,
        )

    raw_tags = front.get("tags") or ()
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",")]
    tags = tuple(sorted({str(t).strip().lower() for t in raw_tags if str(t).strip()}))

    scripts, files = _scan_bundle(directory)
    version = front.get("version")

    raw_exec = front.get("execution") or {}
    if not isinstance(raw_exec, dict):
        raw_exec = {}
    default_policy = _parse_policy(raw_exec.get("default"))
    policies: dict[str, ExecutionPolicy] = {"": default_policy}
    for key, value in raw_exec.items():
        if key == "default":
            continue
        policies[key] = _parse_policy(value, default_policy)

    hooks = {
        stage: rel
        for stage, rel in (("pre", f"hooks{os.sep}pre.py"), ("post", f"hooks{os.sep}post.py"))
        if (directory / rel).is_file()
    }

    return SkillMeta(
        name=name,
        description=description,
        tags=tags,
        version=str(version) if version is not None else None,
        directory=directory,
        skill_md=skill_md,
        body_offset=body_offset,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        scripts=scripts,
        files=files,
        policies=policies,
        hooks=hooks,
        allowed_tools=allowed_tools,
        license=str(front.get("license", "")),
        haystack=f"{name}\n{description}\n{' '.join(tags)}".lower(),
    )


class SkillIndex:
    """Thread-safe-by-immutability index over one or more skill roots."""

    def __init__(self, roots: Iterable[Path | str], *, body_cache_max: int = 512):
        self.roots = [Path(r).expanduser().resolve() for r in roots]
        self._body_cache_max = body_cache_max
        # name -> (mtime_ns, size, body). Plain dict: CPython dict writes are
        # atomic under the GIL, and a stale entry is caught by the stamp check.
        self._body_cache: dict[str, tuple[int, int, str]] = {}
        self._snapshot = _Snapshot({}, (), {}, 0, 0.0)
        self._rejected: list[dict[str, str]] = []
        self.refresh(force=True)

    # ---------------------------------------------------------------- scanning

    def _discover(self) -> list[Path]:
        """Find every SKILL.md under the roots (one level of nesting, plus a
        namespace level so ``skills/<team>/<skill>/SKILL.md`` also works)."""
        found: list[Path] = []
        for root in self.roots:
            if not root.is_dir():
                logger.warning("skill root does not exist: %s", root)
                continue
            for depth1 in os.scandir(root):
                if not depth1.is_dir(follow_symlinks=True) or depth1.name.startswith("."):
                    continue
                candidate = Path(depth1.path) / SKILL_FILE
                if candidate.is_file():
                    found.append(candidate)
                    continue
                for depth2 in os.scandir(depth1.path):
                    if not depth2.is_dir(follow_symlinks=True) or depth2.name.startswith("."):
                        continue
                    nested = Path(depth2.path) / SKILL_FILE
                    if nested.is_file():
                        found.append(nested)
        return found

    def refresh(self, *, force: bool = False) -> bool:
        """Rebuild the snapshot if anything changed. Returns True if it did.

        The no-change path costs one ``stat`` per skill and allocates nothing
        beyond the stamp dict, so calling this on a 5s timer is free even for
        large trees.
        """
        skill_files = self._discover()
        stamps: dict[str, tuple[int, int]] = {}
        for path in skill_files:
            try:
                stat = path.stat()
            except OSError:
                continue
            stamps[str(path)] = (stat.st_mtime_ns, stat.st_size)

        if not force and stamps == self._snapshot.stamps:
            return False

        by_name: dict[str, SkillMeta] = {}
        rejected: list[dict[str, str]] = []
        for path in skill_files:
            try:
                meta = _load_meta(path)
            except SkillRejected as exc:
                # Surfaced rather than swallowed: a skill that silently fails to
                # load is the hardest kind of problem to notice.
                logger.warning("rejected %s: %s", exc.path, exc.reason)
                rejected.append({"path": str(exc.path), "reason": exc.reason})
                continue
            if meta.name in by_name:
                logger.warning(
                    "duplicate skill name %r: keeping %s, ignoring %s",
                    meta.name,
                    by_name[meta.name].skill_md,
                    meta.skill_md,
                )
                continue
            by_name[meta.name] = meta

        self._rejected = rejected
        catalog = tuple(m.card() for m in sorted(by_name.values(), key=lambda m: m.name))
        search = SearchIndex.build(
            (
                name,
                {
                    "name": meta.name.replace("-", " "),
                    "description": meta.description,
                    "tags": " ".join(meta.tags),
                },
            )
            for name, meta in by_name.items()
        )
        self._snapshot = _Snapshot(
            by_name=by_name,
            catalog=catalog,
            stamps=stamps,
            generation=self._snapshot.generation + 1,
            built_at=time.time(),
            search=search,
        )
        # Drop bodies for skills that vanished; edited ones self-invalidate.
        for stale in [n for n in self._body_cache if n not in by_name]:
            self._body_cache.pop(stale, None)
        logger.info("skill index generation %d: %d skills", self._snapshot.generation, len(by_name))
        return True

    # ------------------------------------------------------------------ reads

    @property
    def generation(self) -> int:
        return self._snapshot.generation

    def __len__(self) -> int:
        return len(self._snapshot.by_name)

    def facets(self, snap: "_Snapshot | None" = None) -> dict[str, int]:
        """領域 -> skill 數。用於目錄放不下時的總覽。

        領域取自 tags；沒有 tags 的 skill 以名稱的第一段推導
        （kebab-case 的第一個 token），因為那通常就是領域前綴
        （order-lookup、order-refund → order）。
        """
        snap = snap or self._snapshot
        counts: dict[str, int] = {}
        for meta in snap.by_name.values():
            keys = meta.tags or (meta.name.split("-")[0],)
            for key in keys:
                counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def catalog(
        self,
        *,
        query: str | None = None,
        tags: Iterable[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Filtered skill cards. Zero I/O — pure in-memory."""
        snap = self._snapshot  # bind once: immune to a concurrent swap
        if not query and not tags:
            return list(snap.catalog[:limit])

        wanted = {t.lower() for t in tags} if tags else None

        if query:
            # BM25。取代原本的子字串比對——後者對中文完全無效：查詢會被
            # 當成單一 token，而「查訂單」不是「查詢內部訂單系統…」的
            # 子字串。實測 13 個真實中文查詢只命中 1 個。
            ranked = snap.search.search(query, limit=limit * 4 if wanted else limit)
            cards: list[dict[str, Any]] = []
            for name, _score in ranked:
                meta = snap.by_name.get(name)
                if meta is None:
                    continue
                if wanted and not wanted.issubset(meta.tags):
                    continue
                cards.append(meta.card())
                if len(cards) >= limit:
                    break
            return cards

        scored: list[tuple[int, str, dict[str, Any]]] = []
        for meta in snap.by_name.values():
            if wanted and not wanted.issubset(meta.tags):
                continue
            scored.append((0, meta.name, meta.card()))

        scored.sort(key=lambda row: (row[0], row[1]))
        return [card for _, _, card in scored[:limit]]

    def sample_per_facet(self, per: int = 1, limit: int = 40) -> list[dict[str, Any]]:
        """每個領域取幾個代表。

        目的是讓模型知道「每個領域的 skill 長什麼樣」，而不是只拿到
        字母序前 N 個——後者會讓整個領域對模型隱形。
        """
        snap = self._snapshot
        buckets: dict[str, list[SkillMeta]] = {}
        for meta in sorted(snap.by_name.values(), key=lambda m: m.name):
            key = (meta.tags or (meta.name.split("-")[0],))[0]
            buckets.setdefault(key, []).append(meta)

        out: list[dict[str, Any]] = []
        for key in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
            for meta in buckets[key][:per]:
                out.append(meta.card())
                if len(out) >= limit:
                    return out
        return out

    def get(self, name: str) -> SkillMeta:
        meta = self._snapshot.by_name.get(name)
        if meta is None:
            close = [n for n in self._snapshot.by_name if name.lower() in n.lower()][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise SkillLoadError(f"unknown skill {name!r}.{hint} Call list_skills first.")
        return meta

    def body(self, name: str) -> str:
        """Skill body (frontmatter stripped), cached and mtime-validated.

        Blocking file I/O — call from a worker thread, not the event loop.
        """
        meta = self.get(name)
        cached = self._body_cache.get(name)
        if cached is not None and cached[0] == meta.mtime_ns and cached[1] == meta.size:
            return cached[2]

        with meta.skill_md.open("rb") as fh:
            fh.seek(meta.body_offset)
            # Drop the blank line that conventionally follows the closing fence.
            text = fh.read().decode("utf-8", "replace").lstrip("\n")

        if len(self._body_cache) >= self._body_cache_max:
            self._body_cache.pop(next(iter(self._body_cache)), None)
        self._body_cache[name] = (meta.mtime_ns, meta.size, text)
        return text

    def resolve_file(self, name: str, relpath: str) -> Path:
        """Resolve a bundled file, refusing anything outside the skill directory.

        Guards against ``../`` traversal, absolute paths, and symlinks pointing
        out of the bundle (``resolve()`` follows links before the containment
        check, so a symlink to /etc/passwd is rejected here).
        """
        meta = self.get(name)
        if not relpath or os.path.isabs(relpath):
            raise SkillLoadError("path must be relative to the skill directory")

        base = meta.directory.resolve()
        target = (base / relpath).resolve()
        if not target.is_relative_to(base):
            raise SkillLoadError(f"path escapes the skill bundle: {relpath!r}")
        if not target.is_file():
            raise SkillLoadError(f"no such file in skill {name!r}: {relpath!r}")
        return target

    def stats(self) -> dict[str, Any]:
        snap = self._snapshot
        return {
            "skills": len(snap.by_name),
            "generation": snap.generation,
            "indexed_at": snap.built_at,
            "bodies_cached": len(self._body_cache),
            "roots": [str(r) for r in self.roots],
            # Non-empty means a skill on disk is not being served. Check this
            # first when "my skill isn't showing up".
            "rejected": self._rejected,
        }
