"""检索质量与延迟指标。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from .dataset import RetrievalQuery


@dataclass(frozen=True)
class QueryEvaluation:
    """单个问题的排序结果和延迟。"""

    query: RetrievalQuery
    ranked_document_ids: Sequence[str]
    latency_ms: float


def calculate_metrics(evaluations: Iterable[QueryEvaluation]) -> Dict[str, float]:
    """计算 Recall@K、MRR@10、空结果率和延迟分位数。"""

    rows = list(evaluations)
    if not rows:
        raise ValueError("评测结果不能为空")

    hit_counts = {1: 0, 5: 0, 10: 0}
    reciprocal_rank_sum = 0.0
    empty_count = 0
    latencies: List[float] = []

    for row in rows:
        relevant = set(row.query.relevant_document_ids)
        ranked = list(row.ranked_document_ids)
        if not ranked:
            empty_count += 1
        for cutoff in hit_counts:
            if relevant.intersection(ranked[:cutoff]):
                hit_counts[cutoff] += 1
        for rank, document_id in enumerate(ranked[:10], start=1):
            if document_id in relevant:
                reciprocal_rank_sum += 1.0 / rank
                break
        latencies.append(row.latency_ms)

    total = float(len(rows))
    return {
        "query_count": int(total),
        "recall_at_1": hit_counts[1] / total,
        "recall_at_5": hit_counts[5] / total,
        "recall_at_10": hit_counts[10] / total,
        "mrr_at_10": reciprocal_rank_sum / total,
        "empty_result_rate": empty_count / total,
        "latency_ms_mean": sum(latencies) / total,
        "latency_ms_p50": percentile(latencies, 50),
        "latency_ms_p95": percentile(latencies, 95),
    }


def percentile(values: Sequence[float], percentage: float) -> float:
    """使用线性插值计算确定性分位数。"""

    if not values:
        raise ValueError("分位数输入不能为空")
    if percentage < 0 or percentage > 100:
        raise ValueError("percentage 必须位于 0 到 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentage / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
