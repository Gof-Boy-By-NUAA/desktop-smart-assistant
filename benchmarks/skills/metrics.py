"""技能选择的集合质量、注入成本和延迟指标。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple

from benchmarks.retrieval.metrics import percentile

from .dataset import SkillSelectionCase


@dataclass(frozen=True)
class SelectionObservation:
    """单个样本的冻结标签、预测、提示词和选择延迟。"""

    case: SkillSelectionCase
    selected_skill_names: Tuple[str, ...]
    prompt: str
    latency_ms: float


def calculate_selection_metrics(
    observations: Iterable[SelectionObservation],
) -> Dict[str, object]:
    """计算微平均集合指标与确定性提示词体积估算。"""

    rows = tuple(observations)
    if not rows:
        raise ValueError("技能选择评测结果不能为空")
    true_positive = 0
    false_positive = 0
    false_negative = 0
    exact_matches = 0
    negative_count = 0
    negative_injections = 0
    selected_count = 0
    prompt_characters = 0
    prompt_tokens = 0
    latencies = []

    for row in rows:
        selected = tuple(row.selected_skill_names)
        if selected != tuple(sorted(set(selected))):
            raise ValueError("选中技能必须排序且去重")
        if row.latency_ms < 0 or not math.isfinite(row.latency_ms):
            raise ValueError("选择延迟必须是非负有限数")
        expected_set = set(row.case.expected_skill_names)
        selected_set = set(selected)
        true_positive += len(expected_set & selected_set)
        false_positive += len(selected_set - expected_set)
        false_negative += len(expected_set - selected_set)
        exact_matches += int(expected_set == selected_set)
        if not expected_set:
            negative_count += 1
            negative_injections += int(bool(selected_set))
        selected_count += len(selected)
        prompt_characters += len(row.prompt)
        prompt_tokens += _estimate_prompt_tokens(row.prompt)
        latencies.append(row.latency_ms)

    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = _ratio(2.0 * precision * recall, precision + recall)
    count = len(rows)
    return {
        "case_count": count,
        "micro_true_positive": true_positive,
        "micro_false_positive": false_positive,
        "micro_false_negative": false_negative,
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
        "exact_set_accuracy": exact_matches / float(count),
        "negative_case_count": negative_count,
        "negative_false_injection_count": negative_injections,
        "negative_false_injection_rate": _ratio(
            negative_injections, negative_count
        ),
        "average_selected_skills": selected_count / float(count),
        "total_prompt_characters": prompt_characters,
        "average_prompt_characters": prompt_characters / float(count),
        "total_prompt_tokens_estimate": prompt_tokens,
        "average_prompt_tokens_estimate": prompt_tokens / float(count),
        "token_estimation_method": "ceil(utf8_bytes/4)_per_case",
        "latency_ms_mean": sum(latencies) / float(count),
        "latency_ms_p95": percentile(latencies, 95),
    }


def _estimate_prompt_tokens(prompt: str) -> int:
    if not prompt:
        return 0
    return int(math.ceil(len(prompt.encode("utf-8")) / 4.0))


def _ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)
