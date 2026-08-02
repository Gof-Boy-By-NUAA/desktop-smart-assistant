"""运行真实中文数据集上的 SmartAssistant 检索基线。"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from .smart_assistant_baseline import SmartAssistantKeywordBaseline
from .dataset import RetrievalQuery, ensure_cmrc2018_dev, load_cmrc2018_dev
from .improved_engine import ImprovedLexicalEngine
from .metrics import QueryEvaluation, calculate_metrics


def run_baseline(
    dataset_path: Path,
    max_queries: Optional[int] = None,
) -> Dict[str, object]:
    """运行 SmartAssistant 原始关键词检索基线。"""

    return run_engine(dataset_path, SmartAssistantKeywordBaseline(), max_queries)


def run_improved(
    dataset_path: Path,
    max_queries: Optional[int] = None,
    candidate_limit: int = 40,
) -> Dict[str, object]:
    """运行新的租户化中文词法检索。"""

    return run_engine(
        dataset_path,
        ImprovedLexicalEngine(candidate_limit=candidate_limit),
        max_queries,
    )


def run_engine(
    dataset_path: Path,
    engine: object,
    max_queries: Optional[int] = None,
) -> Dict[str, object]:
    """使用统一协议建立索引并执行确定性评测。"""

    try:
        implementation_sha256 = _read_engine_implementation_sha256(engine)
        dataset = load_cmrc2018_dev(dataset_path)
        queries = _select_queries(dataset.queries, max_queries)
        index_started = time.perf_counter()
        engine.index(dataset.documents)
        index_latency_ms = (time.perf_counter() - index_started) * 1000.0

        evaluations = []
        for query in queries:
            started = time.perf_counter()
            ranked_ids = engine.search(query.text, limit=10)
            latency_ms = (time.perf_counter() - started) * 1000.0
            evaluations.append(
                QueryEvaluation(
                    query=query,
                    ranked_document_ids=ranked_ids,
                    latency_ms=latency_ms,
                )
            )

        metrics = calculate_metrics(evaluations)
        if _read_engine_implementation_sha256(engine) != implementation_sha256:
            raise RuntimeError("评测运行期间引擎实现指纹发生变化")
        return {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": {
                "id": engine.engine_id,
                "implementation_sha256": implementation_sha256,
                "capabilities": engine.capabilities,
            },
            "dataset": {
                "id": dataset.source_id,
                "sha256": dataset.source_sha256,
                "document_count": len(dataset.documents),
                "available_query_count": len(dataset.queries),
                "evaluated_query_count": len(queries),
                "query_selection_sha256": _query_selection_hash(queries),
            },
            "environment": {
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
            },
            "index_latency_ms": index_latency_ms,
            "metrics": metrics,
        }
    finally:
        engine.close()


def _read_engine_implementation_sha256(engine: object) -> str:
    """读取并严格校验引擎提供的 SHA-256 实现指纹。"""

    value = getattr(engine, "implementation_sha256", None)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("引擎 implementation_sha256 必须是小写 SHA-256")
    return value


def _select_queries(
    queries: Sequence[RetrievalQuery], max_queries: Optional[int]
) -> Sequence[RetrievalQuery]:
    """按问题标识哈希排序，生成与源文件顺序无关的稳定子集。"""

    ordered = sorted(
        queries,
        key=lambda query: hashlib.sha256(query.query_id.encode("utf-8")).hexdigest(),
    )
    if max_queries is None:
        return ordered
    if max_queries <= 0:
        raise ValueError("max_queries 必须大于零")
    return ordered[:max_queries]


def _query_selection_hash(queries: Sequence[RetrievalQuery]) -> str:
    """计算本次问题集合的稳定指纹。"""

    raw = "\n".join(query.query_id for query in queries).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="运行 SmartAssistant 中文检索基线")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--engine",
        choices=("baseline", "improved"),
        default="baseline",
    )
    parser.add_argument("--candidate-limit", type=int, default=40)
    args = parser.parse_args()

    dataset_path = args.dataset or ensure_cmrc2018_dev()
    if args.engine == "baseline":
        report = run_baseline(dataset_path, max_queries=args.max_queries)
    else:
        report = run_improved(
            dataset_path,
            max_queries=args.max_queries,
            candidate_limit=args.candidate_limit,
        )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
