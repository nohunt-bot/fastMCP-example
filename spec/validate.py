#!/usr/bin/env python3
"""規範驗證器 —— 讓 RFC 可被機器執行。

這支程式是規範的**可執行形式**。文件描述規則，這裡實作偵測邏輯，兩者
以規則 ID 對應。任何 Skill 都可以在不了解本專案的情況下被驗證：

    uv run python -m spec.validate skills/api-call
    uv run python -m spec.validate skills/ --recursive
    uv run python -m spec.validate skills/ --format=json --level=L2

設計約束（規範本身也要遵守自己的原則）：

* **零外部依賴**（除了 PyYAML）。驗證器必須能在 CI 的最小映像裡跑。
* **不寫任何檔案。** 報告走 stdout。
* **每個發現都指向一條規則與一條 RFC**，讓修正動作是明確的。
* **離開碼有語意**：0 通過、1 有 error、2 驗證器本身出錯。

輸出符合 `schemas/validation-report.schema.json`。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 PyYAML: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

SPEC_VERSION = "1.0.0"

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
SCRIPT_PATH_RE = re.compile(r"^scripts/[A-Za-z0-9_.-]+\.(sh|py|js)$")

MAX_NAME = 64
MAX_DESCRIPTION = 1024
FRONTMATTER_MAX_BYTES = 16 * 1024
BODY_WARN_BYTES = 32 * 1024
RUNNABLE_SUFFIXES = {".sh", ".py", ".js"}
#: 由呼叫端設定會改變「執行什麼」的變數。SEC-007。
DANGEROUS_ENV = {
    "PATH", "SHELL", "IFS", "BASH_ENV", "ENV", "CDPATH", "GLOBIGNORE",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "NODE_PATH",
    "PERL5LIB", "RUBYOPT",
}
DANGEROUS_ENV_PREFIXES = ("LD_", "DYLD_")


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    message: str
    rfc: str = ""
    autofixable: bool = False
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", False)}


@dataclass
class Report:
    spec_version: str = SPEC_VERSION
    conformance_level: str = "L1"
    target: str = ""
    findings: list[Finding] = field(default_factory=list)

    def add(self, **kwargs: Any) -> None:
        self.findings.append(Finding(**kwargs))

    @property
    def summary(self) -> dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    def passed(self, level: str) -> bool:
        counts = self.summary
        if counts["error"]:
            return False
        return not (level in ("L2", "L3") and counts["warning"])

    def to_dict(self, level: str) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "conformance_level": level,
            "passed": self.passed(level),
            "target": self.target,
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
        }


# ----------------------------------------------------------------- 解析

def parse_frontmatter(path: Path) -> tuple[dict[str, Any], str, list[Finding]]:
    """回傳 (manifest, body, findings)。只讀檔案開頭來找 frontmatter。"""
    findings: list[Finding] = []
    raw = path.read_bytes()

    if raw.startswith(b"\xef\xbb\xbf"):
        findings.append(Finding(
            rule="VAL-002", severity="error", path=str(path),
            message="檔案含 UTF-8 BOM，frontmatter 不會被解析",
            rfc="RFC-021", autofixable=True,
            remediation="以無 BOM 的 UTF-8 重新儲存",
        ))
        raw = raw[3:]

    if not raw.startswith(b"---"):
        findings.append(Finding(
            rule="VAL-003", severity="error", path=str(path),
            message="缺少 frontmatter：第一行必須是 ---",
            rfc="RFC-029",
            remediation="在檔案開頭加上以 --- 圍住的 YAML 區塊",
        ))
        return {}, raw.decode("utf-8", "replace"), findings

    head = raw[:FRONTMATTER_MAX_BYTES]
    newline = head.find(b"\n")
    end = head.find(b"\n---", newline) if newline != -1 else -1
    if end == -1:
        findings.append(Finding(
            rule="VAL-004", severity="error", path=str(path),
            message="frontmatter 未以 --- 結束（或超過 16 KB）",
            rfc="RFC-029",
        ))
        return {}, "", findings

    try:
        manifest = yaml.safe_load(head[newline + 1 : end].decode("utf-8", "replace"))
    except yaml.YAMLError as exc:
        findings.append(Finding(
            rule="VAL-005", severity="error", path=str(path),
            message=f"frontmatter 不是合法 YAML：{exc}",
            rfc="RFC-030",
            remediation="檢查縮排是否使用空白而非 tab",
        ))
        return {}, "", findings

    if not isinstance(manifest, dict):
        findings.append(Finding(
            rule="VAL-006", severity="error", path=str(path),
            message="frontmatter 必須是 YAML 對映（key: value）",
            rfc="RFC-030",
        ))
        return {}, "", findings

    closing = raw.find(b"\n", end + 1)
    body = raw[closing + 1 :].decode("utf-8", "replace") if closing != -1 else ""
    return manifest, body, findings


# ----------------------------------------------------------------- 各章驗證

def check_manifest(manifest: dict[str, Any], directory: Path, report: Report) -> None:
    where = str(directory / "SKILL.md")

    name = manifest.get("name", directory.name)
    if not isinstance(name, str) or not NAME_RE.match(name):
        report.add(
            rule="VAL-010", severity="error", path=where,
            message=f"name {name!r} 不合規：只允許小寫字母、數字、連字號",
            rfc="RFC-031",
            remediation="改成 kebab-case，例如 order-lookup",
        )
    elif len(name) > MAX_NAME:
        report.add(
            rule="VAL-011", severity="error", path=where,
            message=f"name 長度 {len(name)} 超過 {MAX_NAME}",
            rfc="RFC-031",
        )

    description = manifest.get("description")
    if not description or not isinstance(description, str) or not description.strip():
        report.add(
            rule="VAL-012", severity="error", path=where,
            message="缺少 description：模型沒有它就無法判斷何時使用此 Skill",
            rfc="RFC-034",
        )
    elif len(description) > MAX_DESCRIPTION:
        report.add(
            rule="VAL-013", severity="error", path=where,
            message=f"description 長度 {len(description)} 超過 {MAX_DESCRIPTION}",
            rfc="RFC-034",
            remediation="細節移到 Body，description 只留路由判斷用的一句話",
        )
    elif len(description) < 20:
        report.add(
            rule="LINT-002", severity="warning", path=where,
            message="description 過短，可能只是標題而非路由依據",
            rfc="RFC-035",
            remediation="說明「做什麼」與「什麼時候使用」",
        )

    version = manifest.get("version")
    if version is not None and not SEMVER_RE.match(str(version)):
        report.add(
            rule="LINT-001", severity="warning", path=where,
            message=f"version {version!r} 不是 Semantic Versioning",
            rfc="RFC-030", autofixable=False,
        )

    tags = manifest.get("tags", [])
    if tags and not isinstance(tags, list):
        report.add(rule="VAL-014", severity="error", path=where,
                   message="tags 必須是陣列", rfc="RFC-030")
    elif isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, str) or not NAME_RE.match(tag):
                report.add(rule="LINT-004", severity="warning", path=where,
                           message=f"tag {tag!r} 不是 kebab-case", rfc="RFC-030")

    for key in manifest:
        if key.startswith("x-"):
            continue
        if key not in {"name", "description", "version", "tags", "license",
                       "allowed-tools", "allowed_tools", "execution"}:
            report.add(
                rule="LINT-005", severity="info", path=where,
                message=f"未知屬性 {key!r} 會被忽略；自訂擴充請加 x- 前綴",
                rfc="RFC-046",
            )


def check_execution(manifest: dict[str, Any], directory: Path,
                    scripts: set[str], report: Report) -> None:
    where = str(directory / "SKILL.md")
    execution = manifest.get("execution")
    if execution is None:
        return
    if not isinstance(execution, dict):
        report.add(rule="VAL-020", severity="error", path=where,
                   message="execution 必須是對映", rfc="RFC-036")
        return

    for key, policy in execution.items():
        if key != "default" and not key.startswith("scripts/"):
            report.add(
                rule="VAL-021", severity="error", path=where,
                message=f"execution 的鍵 {key!r} 必須是 'default' 或 scripts/ 路徑",
                rfc="RFC-036",
            )
            continue
        if key != "default" and key not in scripts:
            report.add(
                rule="VAL-022", severity="error", path=where,
                message=f"execution 指向不存在的 script：{key}",
                rfc="RFC-043",
                remediation="修正路徑，或移除這段 execution",
            )
        if not isinstance(policy, dict):
            report.add(rule="VAL-023", severity="error", path=where,
                       message=f"execution.{key} 必須是對映", rfc="RFC-036")
            continue

        for banned in ("mode", "key_field", "key_fields"):
            if banned in policy:
                report.add(
                    rule="VAL-024", severity="error", path=where,
                    message=f"execution.{key}.{banned} 已禁止："
                            "Server 不解讀輸出語意，宣告它只會讓 Server 猜錯",
                    rfc="RFC-036",
                    remediation="移除此欄位；用 description 以自然語言說明即可",
                )

        timeout = policy.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                report.add(rule="VAL-025", severity="error", path=where,
                           message=f"execution.{key}.timeout 必須是正數", rfc="RFC-037")
            elif timeout > 900:
                report.add(rule="LINT-006", severity="warning", path=where,
                           message=f"timeout {timeout} 超過 900 秒上限，將被鉗制",
                           rfc="RFC-037")

        stall = policy.get("stall_timeout")
        if stall is not None and (not isinstance(stall, (int, float)) or stall < 0):
            report.add(rule="VAL-026", severity="error", path=where,
                       message=f"execution.{key}.stall_timeout 必須 >= 0", rfc="RFC-036")

        for extra in set(policy) - {"timeout", "stall_timeout", "description"}:
            if extra not in ("mode", "key_field", "key_fields"):
                report.add(rule="LINT-007", severity="warning", path=where,
                           message=f"execution.{key} 有未知屬性 {extra!r}", rfc="RFC-036")


def check_filesystem(directory: Path, report: Report) -> set[str]:
    """回傳 scripts 的相對路徑集合。"""
    scripts: set[str] = set()
    base = directory.resolve()

    for entry in sorted(directory.rglob("*")):
        rel = entry.relative_to(directory).as_posix()
        if entry.is_symlink():
            try:
                target = entry.resolve()
                inside = target.is_relative_to(base)
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                report.add(
                    rule="SEC-001", severity="error", path=str(entry),
                    message=f"符號連結指向 Bundle 之外：{rel}",
                    rfc="RFC-028",
                    remediation="刪除該連結；Bundle 必須自我完備",
                )
        if not entry.is_file():
            continue

        top = rel.split("/")[0]
        if top == "scripts":
            if entry.suffix not in RUNNABLE_SUFFIXES:
                report.add(
                    rule="VAL-030", severity="error", path=str(entry),
                    message=f"scripts/ 下的 {entry.suffix or '(無副檔名)'} 無法執行",
                    rfc="RFC-023",
                    remediation=f"改用 {'/'.join(sorted(RUNNABLE_SUFFIXES))}，或移出 scripts/",
                )
            elif SCRIPT_PATH_RE.match(rel):
                scripts.add(rel)
            else:
                report.add(rule="VAL-031", severity="error", path=str(entry),
                           message=f"script 路徑不合規：{rel}（不得有子目錄）",
                           rfc="RFC-022")
        elif top == "hooks":
            if entry.name.split(".")[0] not in ("pre", "post"):
                report.add(rule="LINT-008", severity="warning", path=str(entry),
                           message=f"hooks/ 下只有 pre.* 與 post.* 會被執行：{rel}",
                           rfc="RFC-026")
        elif rel != "SKILL.md" and top not in ("references", "assets"):
            if entry.suffix in RUNNABLE_SUFFIXES:
                report.add(
                    rule="SEC-002", severity="error", path=str(entry),
                    message=f"可執行副檔名出現在 scripts/ 之外：{rel}",
                    rfc="RFC-022",
                    remediation="移到 scripts/，或改成非可執行的副檔名",
                )
            else:
                report.add(rule="LINT-009", severity="info", path=str(entry),
                           message=f"檔案不在標準目錄內：{rel}", rfc="RFC-020")
    return scripts


#: 檔案層級的豁免標記。必須附理由——沒有理由的豁免等於關閉規則。
SUPPRESS_RE = re.compile(r"#\s*spec:allow\s+([A-Z]+-\d{3})\s+(\S.*)$", re.M)


def strip_comments(source: str, suffix: str) -> str:
    """移除註解後再做模式比對。

    直接對原始碼比對會把說明文字當成程式碼——實測中，一段解釋
    「為什麼不要用 | head」的註解被判定為違規。誤報比漏報更傷規範，
    因為它訓練使用者忽略輸出。
    """
    if suffix in (".sh", ".py"):
        return re.sub(r"(?m)(?<!\$)#.*$", "", source)
    if suffix == ".js":
        return re.sub(r"//.*$|/\*.*?\*/", "", source, flags=re.M | re.S)
    return source


def suppressions(source: str) -> dict[str, str]:
    """回傳 {規則ID: 理由}。"""
    return {rule: reason.strip() for rule, reason in SUPPRESS_RE.findall(source)}


def check_scripts(directory: Path, scripts: set[str], report: Report) -> None:
    """靜態檢查 Script 是否遵守執行契約。

    所有比對都在**移除註解後**的內容上進行，並支援
    `# spec:allow <RULE> <理由>` 的檔案層級豁免。
    """
    for rel in sorted(scripts):
        path = directory / rel
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        allowed = suppressions(source)
        code = strip_comments(source, path.suffix)

        def flag(rule: str, severity: str, message: str, rfc: str, fix: str = "") -> None:
            if rule in allowed:
                report.add(rule="LINT-030", severity="info", path=str(path),
                           message=f"{rule} 已豁免：{allowed[rule]}", rfc=rfc)
                return
            report.add(rule=rule, severity=severity, path=str(path),
                       message=message, rfc=rfc, remediation=fix)

        if path.suffix == ".py":
            if re.search(r"requests\.(get|post|put|delete|patch)\((?![^)]*timeout)", code):
                flag("SEC-010", "error",
                     "requests 呼叫沒有 timeout：requests 完全沒有預設值，會永遠等下去",
                     "RFC-050", "加上 timeout=(連線, 讀取)")
            if re.search(r"urlopen\((?![^)]*timeout)", code):
                flag("SEC-011", "error",
                     "urlopen 沒有 timeout：預設是 OS 層級的數分鐘",
                     "RFC-050", "加上 timeout=10")

        if path.suffix == ".sh":
            if not re.search(r"^set -[a-z]*e", code, re.M):
                flag("LINT-020", "warning",
                     "shell script 未設定 set -e",
                     "RFC-051",
                     "加上 set -e；若刻意要自行處理離開碼，用 "
                     "# spec:allow LINT-020 <理由> 豁免")
            # 只看實際的 pipeline，不看註解；且 head -c 不會像 head -n 那樣提前關閉
            if re.search(r"\|\s*head\s+-n?\s*\d", code) and "pipefail" in code:
                flag("LINT-021", "warning",
                     "pipefail 搭配 | head 會因 SIGPIPE 產生離開碼 141",
                     "RFC-051", "改用產生端的限制（如 git -n）或 awk 'NR<=N'")
            # curl 的選項常放在陣列變數裡，因此以整份檔案為範圍判斷
            if "curl" in code and "--max-time" not in code:
                flag("SEC-012", "error",
                     "curl 沒有 --max-time：讀取沒有上限",
                     "RFC-050", "加上 --max-time 與 --connect-timeout")

        for name in DANGEROUS_ENV:
            if path.suffix == ".sh" and re.search(rf'^\s*(export\s+)?{name}=', code, re.M):
                flag("SEC-013", "warning",
                     f"script 覆寫 {name}，會改變後續執行的對象", "RFC-052")
                break

        if re.search(r"SKILL_STATE_DIR|mkdir\s+-p\s+/(var|etc|opt)", code):
            flag("VAL-040", "error",
                 "script 嘗試使用可寫狀態目錄；服務不提供任何可寫位置",
                 "RFC-041", "狀態送到後端 API；handle 由呼叫端保管（RFC-042）")


def check_body(body: str, directory: Path, report: Report) -> None:
    where = str(directory / "SKILL.md")
    if len(body.encode()) > BODY_WARN_BYTES:
        report.add(
            rule="LINT-011", severity="warning", path=where,
            message=f"Body 為 {len(body.encode()) // 1024} KB，建議把細節移到 references/",
            rfc="RFC-040",
        )
    if not body.strip():
        report.add(rule="LINT-012", severity="warning", path=where,
                   message="Body 是空的：模型載入後得不到任何指示", rfc="RFC-040")

    for ref in re.findall(r"`(references/[A-Za-z0-9_./-]+)`", body):
        if not (directory / ref).is_file():
            report.add(
                rule="VAL-050", severity="error", path=where,
                message=f"Body 指向不存在的檔案：{ref}",
                rfc="RFC-025", remediation="建立該檔案或移除引用",
            )


# ----------------------------------------------------------------- 進入點

def validate_skill(directory: Path, report: Report) -> None:
    skill_md = directory / "SKILL.md"
    if not skill_md.is_file():
        report.add(rule="VAL-001", severity="error", path=str(directory),
                   message="缺少 SKILL.md", rfc="RFC-021")
        return

    manifest, body, findings = parse_frontmatter(skill_md)
    report.findings.extend(findings)
    if not manifest:
        return

    scripts = check_filesystem(directory, report)
    check_manifest(manifest, directory, report)
    check_execution(manifest, directory, scripts, report)
    check_scripts(directory, scripts, report)
    check_body(body, directory, report)


def discover(root: Path, recursive: bool) -> list[Path]:
    if (root / "SKILL.md").is_file():
        return [root]
    if not recursive:
        return [p for p in sorted(root.iterdir()) if (p / "SKILL.md").is_file()]
    return sorted({p.parent for p in root.rglob("SKILL.md")})


def render_text(report: Report, level: str) -> str:
    colour = sys.stdout.isatty()
    red = "\033[31m" if colour else ""
    yellow = "\033[33m" if colour else ""
    green = "\033[32m" if colour else ""
    dim = "\033[2m" if colour else ""
    off = "\033[0m" if colour else ""
    icons = {"error": f"{red}error{off}", "warning": f"{yellow}warn {off}",
             "info": f"{dim}info {off}"}

    lines = [f"規範驗證 v{SPEC_VERSION}  目標：{report.target}  等級：{level}", "─" * 70]
    if not report.findings:
        lines.append(f"  {green}沒有任何問題{off}")
    for finding in report.findings:
        lines.append(f"  [{icons[finding.severity]}] {finding.rule}  {finding.message}")
        lines.append(f"          {dim}{finding.path}{off}")
        if finding.rfc:
            lines.append(f"          {dim}依據 {finding.rfc}{off}")
        if finding.remediation:
            lines.append(f"          → {finding.remediation}")

    counts = report.summary
    lines += ["─" * 70,
              f"  error {counts['error']}  warning {counts['warning']}  info {counts['info']}"]
    if report.passed(level):
        lines.append(f"\n{green}通過 {level}{off}")
    else:
        lines.append(f"\n{red}未通過 {level}{off}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spec.validate", description="依 RFC-SKILL-1 驗證 MCP Skill")
    parser.add_argument("target", type=Path, help="Skill 目錄或 Skill Root")
    parser.add_argument("--recursive", action="store_true", help="遞迴搜尋 SKILL.md")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--level", choices=["L1", "L2", "L3"], default="L1",
                        help="L1 只看 error；L2 以上 warning 也算失敗")
    args = parser.parse_args(argv)

    if not args.target.exists():
        print(f"找不到路徑：{args.target}", file=sys.stderr)
        return 2

    report = Report(target=str(args.target), conformance_level=args.level)
    targets = discover(args.target, args.recursive)
    if not targets:
        report.add(rule="VAL-000", severity="error", path=str(args.target),
                   message="找不到任何 SKILL.md", rfc="RFC-020")
    for skill_dir in targets:
        validate_skill(skill_dir, report)

    if args.format == "json":
        print(json.dumps(report.to_dict(args.level), ensure_ascii=False, indent=2))
    else:
        print(render_text(report, args.level))
    return 0 if report.passed(args.level) else 1


if __name__ == "__main__":
    sys.exit(main())
