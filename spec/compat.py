#!/usr/bin/env python3
"""相容性檢查 —— 比對兩個版本的 Skill，找出未被標記的破壞性變更。

    uv run python -m spec.compat --base=origin/main
    uv run python -m spec.compat --base=v1.2.0 --format=json

破壞性變更靜默發生是最糟的失敗模式：呼叫端在下一次部署後突然壞掉，而
沒有任何一份文件說過會壞。300 個 Skill 的規模下，人工比對不可能可靠。

判準來自 RFC-08 §14.2。工具只回報**事實**（什麼變了、屬於哪一類），
是否可接受由人決定——但它會堅持「破壞性變更必須反映在版本號上」。

設計約束（與 validate.py 相同）：

* **零外部依賴**（PyYAML 除外）。
* **不寫任何檔案。** base 版本以 ``git show`` 讀進記憶體，不 checkout。
* **離開碼有語意**：0 相容或已正確標記、1 有未標記的破壞性變更、
  2 工具本身出錯。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

SPEC_VERSION = "1.0.0"
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

#: RFC-08 §14.2 的分類。順序即嚴重度。
BREAKING, MINOR, PATCH = "breaking", "minor", "patch"


@dataclass
class Change:
    skill: str
    kind: str
    what: str
    rfc: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class CompatReport:
    spec_version: str = SPEC_VERSION
    base: str = ""
    changes: list[Change] = field(default_factory=list)
    version_problems: list[str] = field(default_factory=list)

    def add(self, **kwargs: Any) -> None:
        self.changes.append(Change(**kwargs))

    @property
    def summary(self) -> dict[str, int]:
        counts = {BREAKING: 0, MINOR: 0, PATCH: 0}
        for change in self.changes:
            counts[change.kind] += 1
        return counts

    @property
    def passed(self) -> bool:
        return not self.version_problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "base": self.base,
            "passed": self.passed,
            "changes": [c.to_dict() for c in self.changes],
            "version_problems": self.version_problems,
            "summary": self.summary,
        }


# ------------------------------------------------------------------ git 讀取

def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} 失敗")
    return result.stdout


def read_manifests_at(ref: str, root: str) -> dict[str, dict[str, Any]]:
    """讀取某個 git ref 下所有 Skill 的 manifest，不 checkout 到磁碟。"""
    try:
        listing = git("ls-tree", "-r", "--name-only", ref, root)
    except RuntimeError as exc:
        raise RuntimeError(f"無法讀取 {ref}：{exc}") from exc

    manifests: dict[str, dict[str, Any]] = {}
    for path in listing.splitlines():
        if not path.endswith("/SKILL.md"):
            continue
        try:
            content = git("show", f"{ref}:{path}")
        except RuntimeError:
            continue
        manifest = parse_frontmatter(content)
        if manifest is None:
            continue
        directory = path.rsplit("/", 1)[0]
        name = manifest.get("name") or directory.rsplit("/", 1)[-1]
        manifest["_scripts"] = sorted(
            p[len(directory) + 1 :]
            for p in listing.splitlines()
            if p.startswith(directory + "/scripts/")
        )
        manifests[str(name)] = manifest
    return manifests


def read_manifests_from_disk(root: Path) -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for skill_md in sorted(root.rglob("SKILL.md")):
        manifest = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        if manifest is None:
            continue
        name = manifest.get("name") or skill_md.parent.name
        scripts_dir = skill_md.parent / "scripts"
        manifest["_scripts"] = sorted(
            p.relative_to(skill_md.parent).as_posix()
            for p in scripts_dir.rglob("*") if p.is_file()
        ) if scripts_dir.is_dir() else []
        manifests[str(name)] = manifest
    return manifests


def parse_frontmatter(content: str) -> dict[str, Any] | None:
    if not content.startswith("---"):
        return None
    lines = content.split("\n")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    try:
        parsed = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ------------------------------------------------------------------ 比對

def _timeout_of(manifest: dict[str, Any], script: str) -> float | None:
    execution = manifest.get("execution")
    if not isinstance(execution, dict):
        return None
    for key in (script, "default"):
        policy = execution.get(key)
        if isinstance(policy, dict) and policy.get("timeout") is not None:
            try:
                return float(policy["timeout"])
            except (TypeError, ValueError):
                return None
    return None


def compare(base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]],
            report: CompatReport) -> None:
    """依 RFC-08 §14.2 分類每一項變更。"""
    for name in sorted(set(base) - set(head)):
        report.add(
            skill=name, kind=BREAKING, what="skill 被移除",
            rfc="RFC-091",
            detail="呼叫端若還在用這個名稱會直接失敗；改名等同於移除加新增",
        )
    for name in sorted(set(head) - set(base)):
        report.add(skill=name, kind=MINOR, what="新增 skill", rfc="RFC-045")

    for name in sorted(set(base) & set(head)):
        before, after = base[name], head[name]

        removed = set(before.get("_scripts", [])) - set(after.get("_scripts", []))
        for script in sorted(removed):
            report.add(
                skill=name, kind=BREAKING, what=f"移除 script {script}",
                rfc="RFC-091", detail="呼叫端可能已在使用這支 script",
            )
        for script in sorted(set(after.get("_scripts", [])) - set(before.get("_scripts", []))):
            report.add(skill=name, kind=MINOR, what=f"新增 script {script}")

        for script in sorted(set(before.get("_scripts", [])) & set(after.get("_scripts", []))):
            old_t, new_t = _timeout_of(before, script), _timeout_of(after, script)
            if old_t and new_t and new_t < old_t:
                report.add(
                    skill=name, kind=BREAKING,
                    what=f"{script} 的 timeout 由 {old_t:g}s 縮短為 {new_t:g}s",
                    rfc="RFC-091",
                    detail="原本能跑完的呼叫可能開始逾時",
                )
            elif old_t and new_t and new_t > old_t:
                report.add(skill=name, kind=MINOR,
                           what=f"{script} 的 timeout 由 {old_t:g}s 放寬為 {new_t:g}s")

        if before.get("description") != after.get("description"):
            report.add(skill=name, kind=MINOR, what="description 變更", rfc="RFC-034")

        old_tags = set(before.get("tags") or ())
        new_tags = set(after.get("tags") or ())
        if old_tags - new_tags:
            report.add(
                skill=name, kind=BREAKING,
                what=f"移除 tag {sorted(old_tags - new_tags)}",
                rfc="RFC-091",
                detail="依 tag 過濾的呼叫端會找不到這個 skill",
            )
        if new_tags - old_tags:
            report.add(skill=name, kind=MINOR, what=f"新增 tag {sorted(new_tags - old_tags)}")


def check_versions(base: dict[str, dict[str, Any]], head: dict[str, dict[str, Any]],
                   report: CompatReport) -> None:
    """破壞性變更必須反映在版本號上（RFC-130/131）。

    這是本工具唯一會讓 CI 失敗的檢查：變更本身是否可接受由人決定，
    但「發生了破壞性變更卻沒有 bump major」是客觀錯誤。
    """
    breaking_by_skill: dict[str, list[str]] = {}
    for change in report.changes:
        if change.kind == BREAKING:
            breaking_by_skill.setdefault(change.skill, []).append(change.what)

    for name, reasons in sorted(breaking_by_skill.items()):
        if name not in head:
            continue  # 整個 skill 被移除，版本號無從標記
        old_v = str(base.get(name, {}).get("version", "") or "")
        new_v = str(head[name].get("version", "") or "")
        old_m, new_m = SEMVER_RE.match(old_v), SEMVER_RE.match(new_v)
        if not old_m or not new_m:
            report.version_problems.append(
                f"{name}: 有破壞性變更（{reasons[0]}）但缺少可比較的 version"
            )
        elif int(new_m.group(1)) <= int(old_m.group(1)):
            report.version_problems.append(
                f"{name}: 有破壞性變更（{reasons[0]}）但 version "
                f"{old_v} -> {new_v} 未提升 major"
            )


# ------------------------------------------------------------------ 輸出

def render(report: CompatReport) -> str:
    colour = sys.stdout.isatty()
    red = "\033[31m" if colour else ""
    yellow = "\033[33m" if colour else ""
    green = "\033[32m" if colour else ""
    dim = "\033[2m" if colour else ""
    off = "\033[0m" if colour else ""
    label = {BREAKING: f"{red}breaking{off}", MINOR: f"{yellow}minor   {off}",
             PATCH: f"{dim}patch   {off}"}

    lines = [f"相容性檢查 v{SPEC_VERSION}  基準：{report.base}", "─" * 70]
    if not report.changes:
        lines.append(f"  {green}與基準版本沒有差異{off}")
    for change in report.changes:
        lines.append(f"  [{label[change.kind]}] {change.skill}：{change.what}")
        if change.detail:
            lines.append(f"              {dim}{change.detail}{off}")

    counts = report.summary
    lines += ["─" * 70,
              f"  breaking {counts[BREAKING]}  minor {counts[MINOR]}  patch {counts[PATCH]}"]

    if report.version_problems:
        lines.append(f"\n{red}破壞性變更未反映在版本號上：{off}")
        lines += [f"  · {problem}" for problem in report.version_problems]
        lines.append(f"\n{red}未通過{off} —— 提升該 skill 的 major 版本，或撤銷破壞性變更")
    elif counts[BREAKING]:
        lines.append(f"\n{yellow}有 {counts[BREAKING]} 項破壞性變更，但版本號已正確標記{off}")
        lines.append("  請確認呼叫端已知悉，並依 RFC-139 提供遷移指南")
    else:
        lines.append(f"\n{green}通過{off}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spec.compat", description="比對兩個版本的 Skill，找出未標記的破壞性變更")
    parser.add_argument("--base", default="origin/main", help="比對基準的 git ref")
    parser.add_argument("--skills", default="skills", help="Skill Root（相對於 repo 根）")
    parser.add_argument("--head", default=None,
                        help="比對對象的 git ref。省略時使用工作區的檔案")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    report = CompatReport(base=args.base)
    try:
        base = read_manifests_at(args.base, args.skills)
        head = (read_manifests_at(args.head, args.skills) if args.head
                else read_manifests_from_disk(repo_root / args.skills))
    except RuntimeError as exc:
        print(f"讀取失敗：{exc}", file=sys.stderr)
        return 2

    compare(base, head, report)
    check_versions(base, head, report)

    if args.format == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
