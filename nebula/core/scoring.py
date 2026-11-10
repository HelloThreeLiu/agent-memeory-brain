"""两因子打分（P0 只用相关性 + 重要度，§9.4）。

相关性**不拿 bm25 归一**：bm25 的绝对值跨查询不可比，硬塞进 0–1 会让
"相关性 < 0.10 丢弃"这条阈值变成拍脑袋。这里用 2-gram 的**查询覆盖率**当分值
（查询里有多少比例的 token 在这条记忆里出现），bm25 只负责同分时的排序。
代价是丢了词频与长度惩罚——P0 的 200 条量级不值得为它引入调参负担。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

#: 因子权重（P0 只开前两项；时近性/权重/置信在 P1 逐步开）
W_RELEVANCE = 0.40
W_IMPORTANCE = 0.22

#: 层系数：L4（semantic/core）×1.0，L3（episodic）×0.7
LAYER_COEF: dict[str, float] = {"core": 1.0, "semantic": 1.0, "episodic": 0.7}
DEFAULT_LAYER_COEF = 0.7

#: 置顶加成：钉住的条目应该稳定出现
PINNED_BONUS = 0.25

#: 相关性下限：低于它且未钉住的直接丢弃
MIN_RELEVANCE = 0.10


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def coverage(query_tokens: Sequence[str], document_tokens: Collection[str]) -> float:
    """查询 token 命中比例，0–1。空查询返回 0。"""
    if not query_tokens:
        return 0.0
    hits = sum(1 for token in query_tokens if token in document_tokens)
    return hits / len(query_tokens)


def score(*, relevance: float, importance: float, layer: str, pinned: bool) -> float:
    base = W_RELEVANCE * _clamp(relevance) + W_IMPORTANCE * _clamp(importance)
    coef = LAYER_COEF.get(layer, DEFAULT_LAYER_COEF)
    total = base * coef
    if pinned:
        total += PINNED_BONUS
    return round(total, 6)
