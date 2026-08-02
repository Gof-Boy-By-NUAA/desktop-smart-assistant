"""用官方 CMRC 2018 文档验证治理记忆派生任务的收敛性。"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from agent.memory.config import MemoryConfig
from agent.memory.governance import (
    IdentityContext,
    MemoryScope,
    MemoryWriteCommand,
)
from agent.memory.manager import MemoryManager
from benchmarks.retrieval.dataset import ensure_cmrc2018_dev, load_cmrc2018_dev
from benchmarks.retrieval.metrics import percentile


_TENANT_ID = "benchmark-memory-outbox"
_ACTOR_ID = "cmrc2018-writer"
_MAX_WRITE_P95_MS = 100.0
_MIN_RECOVERY_DOCUMENTS_PER_SECOND = 20.0


def run_outbox_gate(
    dataset_path: Path,
    max_documents: Optional[int] = None,
    revoke_count: int = 50,
) -> Dict[str, object]:
    """提交真实文档事实，验证批量恢复和撤销不会留下派生污染。"""

    dataset = load_cmrc2018_dev(Path(dataset_path))
    documents = tuple(dataset.documents)
    if max_documents is not None:
        if max_documents <= 0:
            raise ValueError("max_documents 必须大于零")
        documents = documents[:max_documents]
    if not documents:
        raise ValueError("评测数据集没有文档")
    revoke_count = min(max(0, int(revoke_count)), len(documents))

    with tempfile.TemporaryDirectory() as workspace:
        config = MemoryConfig(
            workspace_root=workspace,
            enable_governed_retrieval=True,
            tenant_id=_TENANT_ID,
        )
        manager = MemoryManager(config, embedding_provider=None)
        identity = IdentityContext(
            tenant_id=_TENANT_ID,
            actor_user_id=_ACTOR_ID,
            roles=frozenset(),
            trace_id="trace-cmrc2018-memory-outbox",
            auth_source="benchmark-runner",
        )
        try:
            write_latencies = []
            records = []
            for document in documents:
                started = time.perf_counter_ns()
                records.append(
                    manager.governance_service.write(
                        identity,
                        MemoryWriteCommand(
                            content=document.text,
                            scope=MemoryScope.USER,
                            source_type="cmrc2018",
                            source_ref="cmrc2018:%s" % document.document_id,
                            idempotency_key="cmrc2018-write:%s"
                            % document.document_id,
                            metadata={
                                "title": document.title or document.document_id
                            },
                        ),
                    )
                )
                write_latencies.append(
                    (time.perf_counter_ns() - started) / 1_000_000.0
                )

            pending_before_recovery = (
                manager.governance_repository.count_derivative_jobs(_TENANT_ID)
            )
            manager.close()
            manager = None
            recovery_started = time.perf_counter_ns()
            manager = MemoryManager(config, embedding_provider=None)
            recovery_ms = (
                time.perf_counter_ns() - recovery_started
            ) / 1_000_000.0
            initial_active = manager.governance_repository.list_active_records(
                _TENANT_ID
            )
            initial_documents = [
                manager._governed_index_document(record)
                for record in initial_active
            ]
            initial_projection_matches = sum(
                int(manager._governed_projection_matches(record))
                for record in initial_active
            )
            initial_index_matches = manager.lexical_index.matches_tenant(
                _TENANT_ID, initial_documents
            )

            for record in records[:revoke_count]:
                manager.governance_service.revoke(
                    identity,
                    record.memory_id,
                    "cmrc2018-revoke:%s" % record.memory_id,
                    "真实文档撤销污染门禁",
                )
            pending_before_revoke_recovery = (
                manager.governance_repository.count_derivative_jobs(_TENANT_ID)
            )
            manager.close()
            manager = None
            revoke_recovery_started = time.perf_counter_ns()
            manager = MemoryManager(config, embedding_provider=None)
            revoke_recovery_ms = (
                time.perf_counter_ns() - revoke_recovery_started
            ) / 1_000_000.0
            final_active = manager.governance_repository.list_active_records(
                _TENANT_ID
            )
            final_documents = [
                manager._governed_index_document(record)
                for record in final_active
            ]
            revoked_index_count = sum(
                int(
                    manager.lexical_index.contains_document(
                        _TENANT_ID, record.memory_id
                    )
                )
                for record in records[:revoke_count]
            )
            revoked_projection_count = sum(
                int(manager._governed_projection_path(record.memory_id).exists())
                for record in records[:revoke_count]
            )
            final_index_matches = manager.lexical_index.matches_tenant(
                _TENANT_ID, final_documents
            )
            pending_after_recovery = (
                manager.governance_repository.count_derivative_jobs(_TENANT_ID)
            )
        finally:
            if manager is not None:
                manager.close()

    metrics = {
        "fact_write_latency_ms_mean": statistics.mean(write_latencies),
        "fact_write_latency_ms_p50": percentile(write_latencies, 50),
        "fact_write_latency_ms_p95": percentile(write_latencies, 95),
        "initial_pending_job_count": pending_before_recovery,
        "initial_recovery_ms": recovery_ms,
        "initial_recovery_documents_per_second": _throughput(
            len(documents), recovery_ms
        ),
        "initial_active_count": len(initial_active),
        "initial_projection_match_count": initial_projection_matches,
        "initial_index_matches": initial_index_matches,
        "revoke_count": revoke_count,
        "pending_before_revoke_recovery": pending_before_revoke_recovery,
        "revoke_recovery_ms": revoke_recovery_ms,
        "revoke_recovery_documents_per_second": _throughput(
            max(1, revoke_count), revoke_recovery_ms
        ),
        "final_active_count": len(final_active),
        "revoked_index_count": revoked_index_count,
        "revoked_projection_count": revoked_projection_count,
        "final_index_matches": final_index_matches,
        "pending_after_recovery": pending_after_recovery,
    }
    gates = _build_gates(
        metrics,
        evaluated_count=len(documents),
        available_count=len(dataset.documents),
        max_documents=max_documents,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": all(bool(gate["passed"]) for gate in gates),
        "implementation_sha256": _implementation_fingerprint(),
        "dataset": {
            "id": dataset.source_id,
            "sha256": dataset.source_sha256,
            "available_document_count": len(dataset.documents),
            "evaluated_document_count": len(documents),
            "real_data_ratio": 1.0,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
        },
        "thresholds": {
            "max_write_p95_ms": _MAX_WRITE_P95_MS,
            "min_recovery_documents_per_second": (
                _MIN_RECOVERY_DOCUMENTS_PER_SECOND
            ),
        },
        "metrics": metrics,
        "gates": gates,
    }


def _throughput(item_count: int, latency_ms: float) -> float:
    return item_count / max(latency_ms / 1000.0, 0.000001)


def _build_gates(
    metrics: Dict[str, object],
    *,
    evaluated_count: int,
    available_count: int,
    max_documents: Optional[int],
) -> Sequence[Dict[str, object]]:
    """生成数据、性能、完整性和撤销污染四类硬门禁。"""

    expected_final = evaluated_count - int(metrics["revoke_count"])
    checks = (
        (
            "data.full_document_set",
            evaluated_count,
            available_count,
            max_documents is None and evaluated_count == available_count,
        ),
        (
            "outbox.fact_and_job_atomicity",
            metrics["initial_pending_job_count"],
            evaluated_count,
            int(metrics["initial_pending_job_count"]) == evaluated_count,
        ),
        (
            "outbox.initial_active_count",
            metrics["initial_active_count"],
            evaluated_count,
            int(metrics["initial_active_count"]) == evaluated_count,
        ),
        (
            "outbox.initial_projection_integrity",
            metrics["initial_projection_match_count"],
            evaluated_count,
            int(metrics["initial_projection_match_count"]) == evaluated_count,
        ),
        (
            "outbox.initial_index_integrity",
            metrics["initial_index_matches"],
            True,
            metrics["initial_index_matches"] is True,
        ),
        (
            "outbox.revoke_jobs_persisted",
            metrics["pending_before_revoke_recovery"],
            metrics["revoke_count"],
            int(metrics["pending_before_revoke_recovery"])
            == int(metrics["revoke_count"]),
        ),
        (
            "outbox.final_active_count",
            metrics["final_active_count"],
            expected_final,
            int(metrics["final_active_count"]) == expected_final,
        ),
        (
            "outbox.final_index_integrity",
            metrics["final_index_matches"],
            True,
            metrics["final_index_matches"] is True,
        ),
        (
            "outbox.pending_after_recovery",
            metrics["pending_after_recovery"],
            0,
            int(metrics["pending_after_recovery"]) == 0,
        ),
        (
            "safety.revoked_index_pollution",
            metrics["revoked_index_count"],
            0,
            int(metrics["revoked_index_count"]) == 0,
        ),
        (
            "safety.revoked_projection_pollution",
            metrics["revoked_projection_count"],
            0,
            int(metrics["revoked_projection_count"]) == 0,
        ),
        (
            "performance.fact_write_p95_ms",
            metrics["fact_write_latency_ms_p95"],
            _MAX_WRITE_P95_MS,
            float(metrics["fact_write_latency_ms_p95"])
            <= _MAX_WRITE_P95_MS,
        ),
        (
            "performance.initial_recovery_throughput",
            metrics["initial_recovery_documents_per_second"],
            _MIN_RECOVERY_DOCUMENTS_PER_SECOND,
            float(metrics["initial_recovery_documents_per_second"])
            >= _MIN_RECOVERY_DOCUMENTS_PER_SECOND,
        ),
        (
            "performance.revoke_recovery_throughput",
            metrics["revoke_recovery_documents_per_second"],
            _MIN_RECOVERY_DOCUMENTS_PER_SECOND,
            float(metrics["revoke_recovery_documents_per_second"])
            >= _MIN_RECOVERY_DOCUMENTS_PER_SECOND,
        ),
    )
    return tuple(
        {
            "name": name,
            "actual": actual,
            "expected": expected,
            "passed": bool(passed),
        }
        for name, actual, expected, passed in checks
    )


def _implementation_fingerprint() -> str:
    """绑定事实库、服务、运行时、检索实现和本门禁。"""

    paths = (
        Path("agent/memory/governance/repository.py"),
        Path("agent/memory/governance/service.py"),
        Path("agent/memory/governance/locks.py"),
        Path("agent/memory/manager.py"),
        Path("agent/retrieval/lexical.py"),
        Path("common/path_safety.py"),
        Path("benchmarks/memory/outbox.py"),
        Path("benchmarks/retrieval/dataset.py"),
        Path("benchmarks/retrieval/metrics.py"),
        Path("benchmarks/retrieval/data_sources.json"),
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="运行治理记忆派生任务门禁")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--max-documents", type=int)
    parser.add_argument("--revoke-count", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset_path = args.dataset or ensure_cmrc2018_dev()
    report = run_outbox_gate(
        dataset_path,
        max_documents=args.max_documents,
        revoke_count=args.revoke_count,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
