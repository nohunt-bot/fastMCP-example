"""Skill 檢索：CJK 友善的切詞與 BM25 排序。

為什麼需要這個模組：原本的檢索是 `query.split()` 加子字串比對。中文沒有
空白，整句會變成單一 token，而「查訂單」並不是「查詢內部訂單系統的狀態與
明細」的子字串——實測 13 個真實中文查詢只命中 1 個（8%）。

設計約束：

* **零外部依賴。** 不用 jieba 之類的詞典分詞器：它們需要載入詞庫（啟動
  成本，在 0.1 core 上很貴），而且對內部術語與商品代號的切分不見得比
  n-gram 好。
* **熱路徑。** 檢索在 `list_skills` 上，必須維持毫秒級。
* **中英混合。** 內部 skill 的 description 常常中英夾雜（「查詢 SKU 庫存」），
  兩種語言必須同時可檢索。

做法是 **CJK bigram + 英文詞** 建倒排索引，再以 BM25 排序：

    「訂單狀態」  -> 訂單, 單狀, 狀態  (bigram)
                    訂, 單, 狀, 態      (unigram，低權重)
    description   -> …訂單…狀態…       -> 兩個 bigram 都命中

bigram 抓詞序（「訂單」≠「單訂」），unigram 補救詞序顛倒的情況
（「貨到」vs「到貨」）。停用詞不需要清單：BM25 的 IDF 會讓「的」「與」
這類到處都出現的字自然趨近於零權重。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

#: 拉丁字母、數字、底線構成的詞。中日韓文字之外的一切都走這條。
_WORD_RE = re.compile(r"[a-z0-9_]+")
#: 中日韓統一表意文字 + 日文假名 + 韓文。以字元為單位切 n-gram。
_CJK_RE = re.compile(
    r"[一-鿿㐀-䶿぀-ヿ가-힯]+"
)

#: unigram 的權重。bigram 表達詞序，比較可信；unigram 只是補救詞序顛倒
#: 與單字查詢，因此壓低。
UNIGRAM_WEIGHT = 0.3

# BM25 參數。k1 控制詞頻飽和，b 控制長度正規化。
# 這裡的「文件」是 name + description + tags，長度差異不大，因此 b 取偏低值：
# 較長的 description 不該只因為字多就被懲罰。
BM25_K1 = 1.2
BM25_B = 0.5

#: 欄位權重。名稱命中最可信，其次是標籤。
FIELD_WEIGHTS = {"name": 3.0, "tags": 2.0, "description": 1.0}


def tokenize(text: str) -> list[str]:
    """把文字切成可檢索的 token。

    中文以 bigram 為主、unigram 為輔；英文以詞為單位。單一 CJK 字元的
    輸入（例如查詢「貨」）會保留成 unigram，否則短查詢會完全無法命中。
    """
    tokens: list[str] = []
    lowered = text.lower()

    for match in _CJK_RE.finditer(lowered):
        run = match.group()
        # unigram：補救詞序顛倒與單字查詢
        tokens.extend(run)
        # bigram：主力，帶詞序資訊
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))

    # CJK 以外的部分交給詞切分
    tokens.extend(_WORD_RE.findall(_CJK_RE.sub(" ", lowered)))
    return tokens


def _weight(token: str) -> float:
    """單一 CJK 字元的 token 是 unigram，權重較低。"""
    return UNIGRAM_WEIGHT if len(token) == 1 and _CJK_RE.match(token) else 1.0


@dataclass(slots=True)
class Document:
    """一個 skill 的可檢索表示。"""

    key: str
    #: token -> 加權後的出現次數（已計入欄位權重）
    weights: dict[str, float]
    length: float


@dataclass(slots=True)
class SearchIndex:
    """BM25 倒排索引。與 skill 快照一起建立，之後唯讀。

    建立成本與 skill 數成線性，實測 500 個 skill 約數毫秒，而且發生在
    背景更新執行緒上，不在請求路徑。
    """

    documents: dict[str, Document] = field(default_factory=dict)
    #: token -> 含有該 token 的 document key
    postings: dict[str, set[str]] = field(default_factory=dict)
    average_length: float = 0.0

    @classmethod
    def build(cls, entries: Iterable[tuple[str, dict[str, str]]]) -> "SearchIndex":
        """entries 為 (key, {欄位: 文字}) 序列。"""
        index = cls()
        for key, fields in entries:
            weights: Counter[str] = Counter()
            for field_name, text in fields.items():
                boost = FIELD_WEIGHTS.get(field_name, 1.0)
                for token in tokenize(text):
                    weights[token] += boost * _weight(token)
            if not weights:
                continue
            index.documents[key] = Document(
                key=key, weights=dict(weights), length=sum(weights.values())
            )
            for token in weights:
                index.postings.setdefault(token, set()).add(key)

        if index.documents:
            index.average_length = sum(
                doc.length for doc in index.documents.values()
            ) / len(index.documents)
        return index

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        """回傳 (key, 分數)，分數由高到低。分數為 0 的不回傳。

        只走查詢 token 的 postings，因此成本與「命中的候選數」成正比，
        而不是與 skill 總數成正比。
        """
        query_tokens = tokenize(query)
        if not query_tokens or not self.documents:
            return []

        total = len(self.documents)
        scores: dict[str, float] = {}

        for token in set(query_tokens):
            candidates = self.postings.get(token)
            if not candidates:
                continue
            # BM25 IDF。「的」「與」這類到處都有的字，df 接近 N，
            # idf 自然趨近 0——不需要停用詞清單。
            df = len(candidates)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            if idf <= 0:
                continue
            for key in candidates:
                doc = self.documents[key]
                freq = doc.weights[token]
                norm = 1 - BM25_B + BM25_B * (doc.length / (self.average_length or 1))
                scores[key] = scores.get(key, 0.0) + idf * (
                    freq * (BM25_K1 + 1) / (freq + BM25_K1 * norm)
                )

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:limit]
