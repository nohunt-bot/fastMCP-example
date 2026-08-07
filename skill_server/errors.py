"""結構化錯誤模型（RFC-06 的實作）。

為什麼需要這個：呼叫端是 LLM，不是人。字串錯誤訊息對人來說夠用，對模型
不夠——它無從判斷「這個錯誤重試會不會有用」。實測上這會變成兩種浪費：
不可重試的錯誤被反覆重試燒掉 context，或可重試的暫時性錯誤被當成永久
失敗而直接放棄。``retryable`` 這一個布林值就是給模型看的（RFC-100）。

傳輸方式：MCP 的工具錯誤只帶得動字串，所以結構化內容以 JSON 字串放進
``ToolError``。呼叫端解析得到欄位；解析不了的呼叫端至少還看得到
``user_message``，因為它排在最前面。

兩條紅線：

* ``user_message`` MUST NOT 含絕對路徑、堆疊追蹤或環境變數值（RFC-101）。
  這裡的分工是：路徑類細節一律留在 ``internal_message``。
* ``internal_message`` MUST NOT 回傳給呼叫端（RFC-102）。它只進 log。
  ``as_tool_error()`` 是唯一的出口，且它不會序列化這個欄位——這是靠
  結構保證，不是靠呼叫端自律。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorSpec:
    """錯誤碼總表（RFC-06 §12.2）的一列。"""

    code: str
    category: str
    retryable: bool
    http_status: int
    recovery: str


#: 錯誤碼總表。``retryable`` 與 ``recovery`` 是給模型看的，不是給人看的：
#: 前者決定它要不要重試，後者決定它下一步做什麼。兩者都寫死在這裡而不是
#: 在各個 raise 點各寫一份，是為了讓「同一個碼永遠有同樣的語意」這件事
#: 由資料結構保證。
CATALOG: dict[str, ErrorSpec] = {
    spec.code: spec
    for spec in (
        ErrorSpec("ERR-400", "validation", False, 400, "依 schema 修正參數"),
        ErrorSpec("ERR-401", "validation", False, 400, "使用 Bundle 內的相對路徑"),
        ErrorSpec("ERR-402", "validation", False, 400, "改用 .sh / .py / .js"),
        ErrorSpec("ERR-403", "permission", False, 403, "讀 user_message 的理由"),
        ErrorSpec("ERR-404", "not_found", False, 404, "先呼叫 list_skills"),
        ErrorSpec("ERR-405", "not_found", False, 404, "檢查 Card 的 scripts"),
        ErrorSpec("ERR-406", "not_found", False, 404, "使用錯誤訊息附的標題清單"),
        ErrorSpec("ERR-410", "validation", False, 400, "只傳資料類環境變數"),
        ErrorSpec("ERR-411", "validation", False, 413, "分批，或改由 script 自行取得"),
        ErrorSpec("ERR-412", "validation", False, 400, "減少參數數量或長度"),
        ErrorSpec("ERR-500", "internal", True, 500, "重試一次；持續失敗請回報"),
        ErrorSpec("ERR-503", "resource", True, 503, "等待就緒；檢查 skill 路徑設定"),
    )
}


@dataclass
class SkillError(Exception):
    """帶錯誤碼的例外。

    ``code`` 決定 category / retryable / http_status，呼叫點只需要說明
    「哪一種錯」與「對使用者怎麼講」，不必每次重複判斷可否重試——那正是
    先前每個 raise 點各自為政時最容易寫錯的部分。
    """

    code: str
    user_message: str
    internal_message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in CATALOG:  # pragma: no cover - 開發期才會踩到
            raise KeyError(f"未定義的錯誤碼 {self.code!r}")
        super().__init__(self.user_message)

    @property
    def spec(self) -> ErrorSpec:
        return CATALOG[self.code]

    def payload(self) -> dict[str, Any]:
        """回傳給呼叫端的內容。刻意不含 ``internal_message``（RFC-102）。"""
        spec = self.spec
        body: dict[str, Any] = {
            # user_message 放第一個：解析不了 JSON 的呼叫端至少讀得到它。
            "user_message": self.user_message,
            "code": spec.code,
            "category": spec.category,
            "severity": "error",
            "retryable": spec.retryable,
            "recovery": spec.recovery,
            "http_status": spec.http_status,
        }
        if self.details:
            body["details"] = self.details
        return body

    def as_tool_error(self) -> ToolError:
        """轉成 MCP 的錯誤，並把只能進 log 的部分寫進 log。"""
        if self.internal_message:
            # 這是絕對路徑、例外文字這類東西唯一該出現的地方。
            logger.warning("%s %s | %s", self.code, self.user_message, self.internal_message)
        return ToolError(json.dumps(self.payload(), ensure_ascii=False))


def internal_error(exc: BaseException) -> SkillError:
    """把預期外的例外收斂成 ERR-500，不洩漏它的訊息。

    預期外例外的訊息是最容易挾帶絕對路徑與內部細節的地方（``FileNotFoundError``
    幾乎一定帶著容器內的完整路徑），所以這裡不轉述它，只留型別名稱給 log。
    """
    return SkillError(
        code="ERR-500",
        user_message="伺服器內部錯誤，這次呼叫沒有完成。",
        internal_message=f"{type(exc).__name__}: {exc}",
    )
