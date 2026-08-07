"""RFC-06 錯誤模型的迴歸測試。

重點不是「有沒有丟出錯誤」，而是丟出來的東西對呼叫端（一個 LLM）是否可
判讀：能不能知道該不該重試、下一步做什麼，以及訊息裡有沒有洩漏它不該看
到的東西。

每個測試都對照 spec/schemas/error.schema.json 驗證，所以規範和實作不會
各走各的——這正是先前 spec 宣稱 L2、實際上連 L1 都沒達成的原因。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from skill_server.errors import CATALOG, SkillError, internal_error
from skill_server.server import build_server

SPEC_ROOT = Path(__file__).resolve().parent.parent / "spec"
ERROR_SCHEMA = json.loads((SPEC_ROOT / "schemas" / "error.schema.json").read_text())


def _payload_of(exc: ToolError) -> dict:
    """工具錯誤的內容是一份 JSON。解析不了就是實作沒有遵守錯誤模型。"""
    return json.loads(str(exc))


def _assert_matches_schema(payload: dict) -> None:
    """不引入 jsonschema 依賴，直接檢查 schema 的必要條件。

    這個專案刻意只依賴 fastmcp 與 pyyaml；為了測試多拉一個套件進來，
    會讓 0.5 core 的映像檔為了驗證多背一份執行期依賴。
    """
    for key in ERROR_SCHEMA["required"]:
        assert key in payload, f"缺少必要欄位 {key}"
    allowed = set(ERROR_SCHEMA["properties"])
    assert set(payload) <= allowed, f"多了 schema 未定義的欄位：{set(payload) - allowed}"
    assert payload["code"] in CATALOG
    assert payload["category"] in ERROR_SCHEMA["properties"]["category"]["enum"]
    assert isinstance(payload["retryable"], bool)
    assert payload["user_message"]


def _write_skill(root: Path, name: str) -> Path:
    skill = root / name
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    skill.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的說明。\n---\n內文\n\n## Usage\n用法\n"
    )
    (skill / "scripts" / "run.py").write_text("print('ok')")
    return skill


def test_every_catalogued_code_is_self_consistent():
    """RFC-100：每個錯誤都必須標示 retryable，而且同一個碼語意固定。

    retryable 寫在總表而不是各個 raise 點，就是為了讓這件事成立。
    """
    for code, spec in CATALOG.items():
        assert code.startswith("ERR-") and len(code) == 7
        assert isinstance(spec.retryable, bool)
        assert spec.recovery, f"{code} 沒有復原指引，模型會不知道下一步"
        # 只有伺服器端的暫時性問題可以重試。參數錯誤重試一百次還是錯的，
        # 而讓模型去重試不可重試的錯誤正是 RFC-100 要防的浪費。
        assert spec.retryable == (spec.category in {"internal", "resource"}), code


@pytest.mark.anyio
async def test_unknown_skill_is_a_structured_not_found(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("load_skill", {"name": "does-not-exist"})

    payload = _payload_of(caught.value)
    _assert_matches_schema(payload)
    assert payload["code"] == "ERR-404"
    assert payload["category"] == "not_found"
    # 重試不會讓一個不存在的 skill 出現。
    assert payload["retryable"] is False


@pytest.mark.anyio
async def test_missing_section_is_err_406(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("load_skill", {"name": "alpha", "section": "Nonexistent"})

    payload = _payload_of(caught.value)
    _assert_matches_schema(payload)
    assert payload["code"] == "ERR-406"


@pytest.mark.anyio
async def test_path_escape_is_err_401(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "read_skill_file", {"name": "alpha", "path": "../../etc/passwd"}
            )

    payload = _payload_of(caught.value)
    _assert_matches_schema(payload)
    assert payload["code"] == "ERR-401"
    assert payload["retryable"] is False


@pytest.mark.anyio
async def test_forbidden_caller_env_is_err_410(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "run_skill_script",
                {"name": "alpha", "script": "scripts/run.py", "env": {"PATH": "/tmp/mine"}},
            )

    payload = _payload_of(caught.value)
    _assert_matches_schema(payload)
    assert payload["code"] == "ERR-410"


@pytest.mark.anyio
async def test_oversized_stdin_is_err_411(tmp_path: Path):
    root = tmp_path / "skills"
    _write_skill(root, "alpha")

    async with Client(build_server([root], refresh_interval=0)) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool(
                "run_skill_script",
                {"name": "alpha", "script": "scripts/run.py", "stdin": "A" * (5 * 1024 * 1024)},
            )

    payload = _payload_of(caught.value)
    _assert_matches_schema(payload)
    assert payload["code"] == "ERR-411"


def test_internal_message_never_reaches_the_caller():
    """RFC-102：internal_message 只進 log。

    這是靠結構保證的：payload() 根本不序列化這個欄位，所以呼叫點忘記處理
    也不會外洩。絕對路徑與例外文字都歸在這裡。
    """
    err = SkillError(
        code="ERR-500",
        user_message="讀取失敗，請重試一次。",
        internal_message="FileNotFoundError: /app/skills/secret/SKILL.md",
    )
    payload = err.payload()
    _assert_matches_schema(payload)
    assert "internal_message" not in payload
    assert "/app/skills" not in json.dumps(payload, ensure_ascii=False)


def test_unexpected_exception_does_not_leak_its_message():
    """預期外的例外訊息幾乎一定帶著容器內的絕對路徑。

    ERR-500 因此不轉述原始訊息，只保留型別名稱給 log。
    """
    err = internal_error(FileNotFoundError(2, "No such file", "/app/skills/x/SKILL.md"))
    payload = err.payload()
    _assert_matches_schema(payload)
    assert "/app/skills" not in json.dumps(payload, ensure_ascii=False)
    assert payload["retryable"] is True  # 內部錯誤值得重試一次
    assert "/app/skills" in err.internal_message  # ...但細節有被保留下來


@pytest.mark.anyio
async def test_script_failure_is_not_an_error_code(tmp_path: Path):
    """RFC-103：script 的非零離開碼是資料，不是協定錯誤。

    把它變成 ERR- 碼會讓呼叫端看不到 stderr，也就無從判斷下一步——這是
    整個錯誤模型裡最容易搞錯、後果也最大的一條界線。
    """
    root = tmp_path / "skills"
    skill = _write_skill(root, "alpha")
    (skill / "scripts" / "boom.py").write_text(
        "import sys; print('partial output'); sys.stderr.write('it broke\\n'); sys.exit(3)"
    )

    async with Client(build_server([root], refresh_interval=0)) as client:
        result = (
            await client.call_tool(
                "run_skill_script", {"name": "alpha", "script": "scripts/boom.py"}
            )
        ).data

    assert result["status"] == "failed"
    assert result["exit_code"] == 3
    assert "partial output" in result["stdout"]
    assert "it broke" in result["stderr"]
