"""召回打分（§9.4）。

相关性**不拿 bm25 归一**：bm25 的绝对值跨查询不可比，硬塞进 0–1 会让
"相关性 < 0.10 丢弃"这条阈值变成拍脑袋。这里用 2-gram 的**查询覆盖率**当分值
（查询里有多少比例的 token 在这条记忆里出现），bm25 只负责同分时的排序。

P0 只开相关性与重要度；P1 加权重（merge 累积的强化）与置信度。**时近性没开**：
它会惩罚长期有效的 decision，与"同一事实不问第二遍"正面冲突，而且
``last_recalled`` 这轮才开始有数据。等 P1.5 的真实召回日志再定。

代价是丢了词频与长度惩罚——2000 条量级不值得为它引入调参负担。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

#: 因子权重。P1 起用四项；时近性的 0.14 有意留空不分配。
W_RELEVANCE = 0.40
W_IMPORTANCE = 0.22
W_WEIGHT = 0.10
W_CONFIDENCE = 0.08

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


def normalize_weight(weight: float) -> float:
    """把无上界的 weight 压进 0–1。

    merge 每次 +0.08 且不封顶，直接用原值会让"被合并过 20 次"的条目吃掉整个
    分数。``w / (1 + w)`` 单调、有界，且新写入的基准值 1.0 落在 0.5——
    中性条目不会因为这条因子被系统性抬高或压低。
    """
    if weight <= 0:
        return 0.0
    return weight / (1.0 + weight)


def score(
    *,
    relevance: float,
    importance: float,
    layer: str,
    pinned: bool,
    weight: float = 1.0,
    confidence: float = 0.6,
) -> float:
    base = (
        W_RELEVANCE * _clamp(relevance)
        + W_IMPORTANCE * _clamp(importance)
        + W_WEIGHT * normalize_weight(weight)
        + W_CONFIDENCE * _clamp(confidence)
    )
    coef = LAYER_COEF.get(layer, DEFAULT_LAYER_COEF)
    total = base * coef
    if pinned:
        total += PINNED_BONUS
    return round(total, 6)
