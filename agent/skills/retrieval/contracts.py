"""技能影子检索的不可变数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class ShadowCandidate:
    """回查治理事实后确认仍然有效的影子候选。"""

    rank: int
    skill_id: str
    version: int
    content_hash: str
    score: float
    bm25_score: float
    query_coverage: float
    model_compatible: bool


@dataclass(frozen=True)
class ShadowRun:
    """不改变提示词和执行行为的单次影子检索结果。"""

    run_id: str
    index_generation: str
    candidates: Tuple[ShadowCandidate, ...]
