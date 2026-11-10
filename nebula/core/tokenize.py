"""中文 2-gram 分词（零依赖，不引 jieba）。

P0–P1.5 不用向量（§14）：FTS5 + 中文 2-gram 已经够万级数据用。中文没有空格，
按字切 1-gram 噪音太大、按词切要词典，2-gram 是"零依赖 + 可解释"的折中。

两条规则：
- **建索引时保留全部 token**（含停用片段），否则查询词与索引词会对不上；
- **查询时丢停用片段**，只留"的/了/是/什么/怎么"这类不携带信息的 2-gram，
  否则覆盖率被稀释，"接口用什么方法"和"接口用 POST"会被判成不相关。
"""

from __future__ import annotations

import re
import unicodedata

_CJK_RANGES = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_RUN_RE = re.compile(rf"[{_CJK_RANGES}]+|[a-z0-9_]+")

#: 单字停用：含有这些字的 2-gram 在**查询侧**丢弃
_QUERY_STOP_CHARS = frozenset("的了是吗呢么什怎哪")

#: 整词停用：本身不携带信息的 2-gram
_QUERY_STOP_TOKENS = frozenset({"如何", "是否", "多少", "一个", "这个", "那个", "可以"})


def normalize(text: str) -> str:
    """NFKC + 小写：全角字母数字与半角统一，避免"ＰＯＳＴ"搜不到"POST"。"""
    return unicodedata.normalize("NFKC", text).lower()


def tokenize(text: str) -> list[str]:
    """建索引用的全量 token（去重前的原始序列，保留词频给 bm25 用）。"""
    tokens: list[str] = []
    for run in _RUN_RE.findall(normalize(text)):
        if run[0].isascii():
            tokens.append(run)
            continue
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def index_text(text: str) -> str:
    """写进 ``memories_fts.content_2gram`` 的词串。"""
    return " ".join(tokenize(text))


def query_tokens(text: str) -> list[str]:
    """查询侧 token：去停用、去重、保持首次出现顺序（覆盖率的分母就是它）。"""
    seen: dict[str, None] = {}
    for token in tokenize(text):
        if len(token) == 2 and any(char in _QUERY_STOP_CHARS for char in token):
            continue
        if token in _QUERY_STOP_TOKENS:
            continue
        seen.setdefault(token, None)
    return list(seen)


def is_cjk(token: str) -> bool:
    return bool(token) and not token[0].isascii()
