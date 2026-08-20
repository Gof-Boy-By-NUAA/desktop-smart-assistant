"""在同一份真实 CMRC 2018 数据上执行知识模块对比门禁。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from benchmarks.retrieval.dataset import (
    SOURCE_ID,
    ensure_cmrc2018_dev,
    load_cmrc2018_dev,
    load_source_manifest,
)
from benchmarks.retrieval.evaluate import _query_selection_hash, _select_queries
from benchmarks.retrieval.smart_assistant_baseline import (
    implementation_fingerprint as legacy_implementation_fingerprint,
    implementation_paths as legacy_implementation_paths,
)
from benchmarks.retrieval.metrics import percentile

from .evaluate import (
    implementation_fingerprint as governed_implementation_fingerprint,
    implementation_paths,
    run_real_data_security_checks,
)


REQUIRED_REPETITIONS = 8
INDEX_BENCHMARK_BLOCKS = 16
_QUALITY_ORDER_SEED = 2026073101
_INDEX_ORDER_SEED = 2026073102
_BOOTSTRAP_SEED = 2026073103
_BOOTSTRAP_REPETITIONS = 10_000
_MAX_ONE_SIDED_P_VALUE = 0.05
_MAX_PAIRED_CI95_RATIO = 1.0
_MAX_INDEX_MEDIAN_RATIO = 0.95
_TRIAL_TIMEOUT_SECONDS = 3600

_HIGHER_QUALITY_METRICS = (
    "recall_at_1",
    "recall_at_5",
    "recall_at_10",
    "mrr_at_10",
)
_LOWER_QUALITY_METRICS = ("empty_result_rate",)
_QUALITY_FLOORS = {
    "recall_at_1": 0.85,
    "recall_at_5": 0.90,
    "recall_at_10": 0.92,
    "mrr_at_10": 0.88,
}
_QUALITY_CEILINGS = {"empty_result_rate": 0.05}
_CITATION_EXACT_METRICS = (
    "citation_coverage",
    "citation_location_accuracy",
    "citation_document_accuracy",
    "citation_resolution_accuracy",
    "citation_source_binding_accuracy",
)
_DETERMINISTIC_METRICS = (
    "query_count",
    "recall_at_1",
    "recall_at_5",
    "recall_at_10",
    "mrr_at_10",
    "empty_result_rate",
    "citation_coverage",
    "citation_location_accuracy",
    "citation_document_accuracy",
    "citation_resolution_accuracy",
    "citation_source_binding_accuracy",
    "answer_span_citation_rate_at_10",
    "returned_hit_count",
    "citation_hit_count",
    "citation_resolution_count",
    "citation_source_binding_count",
)
_TIMING_METRICS = (
    "latency_ms_mean",
    "latency_ms_p50",
    "latency_ms_p95",
)
_ABSOLUTE_LATENCY_LIMITS_MS = {
    "latency_ms_mean": 15.0,
    "latency_ms_p95": 25.0,
    "index_latency_ms": 5000.0,
}
_RELATIVE_LATENCY_LIMITS = {
    "latency_ms_mean": 3.0,
    "latency_ms_p95": 3.0,
    "index_latency_ms": 1.0,
}

_COMPARISON_IMPLEMENTATION_PATHS = (
    Path("benchmarks/knowledge/compare.py"),
    Path("benchmarks/knowledge/evaluate.py"),
    Path("benchmarks/knowledge/trial.py"),
    Path("benchmarks/knowledge/verify.py"),
    Path("benchmarks/retrieval/smart_assistant_baseline.py"),
    Path("benchmarks/retrieval/data_sources.json"),
    Path("benchmarks/retrieval/dataset.py"),
    Path("benchmarks/retrieval/evaluate.py"),
    Path("benchmarks/retrieval/metrics.py"),
    Path("agent/memory/storage.py"),
    Path("agent/memory/governance/__init__.py"),
    Path("agent/memory/governance/contracts.py"),
    Path("agent/knowledge/contracts.py"),
    Path("agent/knowledge/parser.py"),
    Path("agent/knowledge/repository.py"),
    Path("agent/knowledge/runtime.py"),
    Path("agent/retrieval/lexical.py"),
    Path("agent/retrieval/__init__.py"),
    Path("agent/tools/knowledge/knowledge_tools.py"),
)

_EXPECTED_ENGINE_IDS = {
    "legacy": "smart-assistant-legacy-knowledge",
    "governed": "smart-assistant-governed-knowledge-v1",
}
_REQUIRED_INDEX_VALIDATION_FIELDS = (
    "expected_document_count",
    "active_document_count",
    "document_ids_match",
    "sqlite_integrity_ok",
    "index_matches",
    "pending_derivative_count",
    "query_probe_count",
    "query_probes_passed",
)


def compare_knowledge_engines(
    dataset_path: Path,
    max_queries: Optional[int] = None,
    repetitions: int = REQUIRED_REPETITIONS,
) -> Dict[str, object]:
    """按预注册独立轮次运行，并生成质量、安全和统计硬门禁。"""

    if repetitions != REQUIRED_REPETITIONS:
        raise ValueError("知识质量门禁必须恰好运行八轮")

    implementation_fingerprint = _comparison_fingerprint()
    dataset_path = Path(dataset_path)
    legacy_runs: List[Dict[str, object]] = []
    governed_runs: List[Dict[str, object]] = []
    security_runs: List[Dict[str, object]] = []
    execution_order: List[Dict[str, object]] = []
    warmup_reports = []
    warmup_queries = min(64, max_queries) if max_queries is not None else 64
    warmup_query_evidence = _expected_query_evidence(dataset_path, warmup_queries)
    quality_query_evidence = _expected_query_evidence(dataset_path, max_queries)
    for position, engine_name in enumerate(("legacy", "governed"), start=1):
        report = _invoke_trial_process(
            dataset_path,
            engine_name,
            "full",
            max_queries=warmup_queries,
        )
        _assert_full_trial(report, engine_name, warmup_query_evidence)
        warmup_reports.append(
            _trial_summary(report, engine_name, 0, position, warmup=True)
        )

    quality_orders = _balanced_quality_orders()
    for repetition, order in enumerate(quality_orders, start=1):
        for position, engine_name in enumerate(order, start=1):
            report = _invoke_trial_process(
                dataset_path,
                engine_name,
                "full",
                max_queries=max_queries,
            )
            _assert_full_trial(report, engine_name, quality_query_evidence)
            if engine_name == "legacy":
                legacy_runs.append(report)
            else:
                governed_runs.append(report)
            execution_order.append(
                _trial_summary(
                    report,
                    engine_name,
                    repetition,
                    position,
                    warmup=False,
                )
            )

    index_benchmark = _run_balanced_index_benchmark(dataset_path)
    verified_index_summary = _recompute_index_benchmark(index_benchmark)
    index_benchmark["verified_summary"] = verified_index_summary
    measurement_protocol = _assert_measurement_protocol_stable(
        warmup_reports, execution_order, index_benchmark
    )

    # 安全探针放在性能轮次之后，并按数据哈希固定选择不重复文档。
    security_schedule = _security_document_schedule()
    for repetition, indices in enumerate(security_schedule):
        security_runs.append(
            run_real_data_security_checks(
                dataset_path,
                sample_document_indices=indices,
                context_index=repetition,
            )
        )

    if _comparison_fingerprint() != implementation_fingerprint:
        raise RuntimeError("门禁运行期间实现指纹发生变化")
    legacy = _aggregate_engine_reports(legacy_runs)
    governed = _aggregate_engine_reports(governed_runs)
    legacy["index_latency_ms"] = verified_index_summary["legacy_median_ms"]
    governed["index_latency_ms"] = verified_index_summary["governed_median_ms"]
    legacy["timing_samples"]["index_latency_ms"] = index_benchmark["legacy"][
        "samples_ms"
    ]
    governed["timing_samples"]["index_latency_ms"] = index_benchmark["governed"][
        "samples_ms"
    ]
    _assert_comparable(legacy, governed)
    query_benchmark = _build_paired_query_benchmark(legacy, governed)
    security = _aggregate_security_reports(security_runs)
    gates = _build_gates(
        legacy,
        governed,
        security,
        query_benchmark,
        index_benchmark,
        max_queries=max_queries,
    )
    full_dataset_gate = next(
        gate for gate in gates if gate["name"] == "data.full_query_set"
    )
    return {
        "schema_version": 4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": all(bool(gate["passed"]) for gate in gates),
        "official_full_dataset_gate": bool(full_dataset_gate["passed"]),
        "repetitions": REQUIRED_REPETITIONS,
        "comparison_implementation_sha256": implementation_fingerprint,
        "comparison_implementation_paths": list(comparison_paths()),
        "thresholds": {
            "quality_floors": _QUALITY_FLOORS,
            "quality_ceilings": _QUALITY_CEILINGS,
            "absolute_latency_limits_ms": _ABSOLUTE_LATENCY_LIMITS_MS,
            "relative_latency_limits": _RELATIVE_LATENCY_LIMITS,
            "max_one_sided_p_value": _MAX_ONE_SIDED_P_VALUE,
            "max_paired_ci95_ratio": _MAX_PAIRED_CI95_RATIO,
            "max_index_median_ratio": _MAX_INDEX_MEDIAN_RATIO,
            "bootstrap_repetitions": _BOOTSTRAP_REPETITIONS,
            "index_protocol": index_benchmark["protocol"],
        },
        "quality_protocol": {
            "name": "balanced-independent-process-v2",
            "rounds": REQUIRED_REPETITIONS,
            "order_seed": _QUALITY_ORDER_SEED,
            "orders": [list(order) for order in quality_orders],
            "fresh_process_per_trial": True,
            "fresh_database_per_trial": True,
            "warmup_trials_per_engine": 1,
        },
        "quality_warmup": warmup_reports,
        "execution_order": execution_order,
        "measurement_protocol": measurement_protocol,
        "query_benchmark": query_benchmark,
        "index_benchmark": index_benchmark,
        "security_protocol": {
            "sample_indices": [list(indices) for indices in security_schedule],
            "unique_document_count": REQUIRED_REPETITIONS * 3,
            "identity_context_count": REQUIRED_REPETITIONS,
        },
        "legacy": legacy,
        "governed": governed,
        "security": security,
        "gates": gates,
    }


def comparison_paths() -> Sequence[str]:
    """列出比较结论指纹覆盖的仓库相对路径。"""

    paths = set(path.as_posix() for path in _COMPARISON_IMPLEMENTATION_PATHS)
    paths.update(str(path).replace("\\", "/") for path in implementation_paths())
    return tuple(sorted(paths))


def _balanced_quality_orders() -> Sequence[Sequence[str]]:
    """用固定种子生成四组 AB 和四组 BA 查询顺序。"""

    orders = [
        ("legacy", "governed") for _ in range(REQUIRED_REPETITIONS // 2)
    ]
    orders.extend(
        ("governed", "legacy") for _ in range(REQUIRED_REPETITIONS // 2)
    )
    random.Random(_QUALITY_ORDER_SEED).shuffle(orders)
    return tuple(orders)


def _balanced_index_orders() -> Sequence[Sequence[str]]:
    """用固定种子生成等量 ABBA 和 BAAB 独立区组。"""

    orders = [
        ("legacy", "governed", "governed", "legacy")
        for _ in range(INDEX_BENCHMARK_BLOCKS // 2)
    ]
    orders.extend(
        ("governed", "legacy", "legacy", "governed")
        for _ in range(INDEX_BENCHMARK_BLOCKS // 2)
    )
    random.Random(_INDEX_ORDER_SEED).shuffle(orders)
    return tuple(orders)


def _security_document_schedule() -> Sequence[Sequence[int]]:
    """按固定数据哈希选择八轮共二十四个不重复文档。"""

    source = load_source_manifest()[SOURCE_ID]
    seed = int(str(source["sha256"])[:16], 16)
    selected = random.Random(seed).sample(range(848), REQUIRED_REPETITIONS * 3)
    return tuple(
        tuple(selected[offset : offset + 3])
        for offset in range(0, len(selected), 3)
    )


def _invoke_trial_process(
    dataset_path: Path,
    engine_name: str,
    mode: str,
    max_queries: Optional[int] = None,
) -> Dict[str, object]:
    """在新 Python 进程中执行一次试验并严格解析单一 JSON 输出。"""

    if engine_name not in _EXPECTED_ENGINE_IDS:
        raise ValueError("未知 Knowledge 引擎: %s" % engine_name)
    if mode not in ("full", "index"):
        raise ValueError("Knowledge 试验模式必须是 full 或 index")
    command = [
        sys.executable,
        "-m",
        "benchmarks.knowledge.trial",
        "--dataset",
        str(Path(dataset_path).resolve()),
        "--engine",
        engine_name,
        "--mode",
        mode,
    ]
    if max_queries is not None:
        command.extend(("--max-queries", str(max_queries)))
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    trial_id = uuid.uuid4().hex
    environment["SMART_ASSISTANT_KNOWLEDGE_TRIAL_ID"] = trial_id
    try:
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=_TRIAL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Knowledge 独立试验超时(%s/%s)" % (engine_name, mode)
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "Knowledge 独立试验失败(%s/%s): %s"
            % (engine_name, mode, completed.stderr.strip()[-4000:])
        )
    try:
        payload = json.loads(
            completed.stdout,
            parse_constant=_reject_non_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        _assert_finite_json_values(payload)
    except ValueError as exc:
        raise RuntimeError("Knowledge 独立试验没有返回严格 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Knowledge 独立试验结果必须是 JSON 对象")
    measurement = payload.get("measurement_environment", {})
    if measurement.get("process_instance_id") != trial_id:
        raise RuntimeError("Knowledge 独立试验实例标识不匹配")
    return payload


def _reject_non_json_constant(value: str) -> object:
    """拒绝 Python JSON 解码器默认接受的 NaN 和 Infinity。"""

    raise ValueError("JSON 包含非标准数值: %s" % value)


def _reject_duplicate_json_keys(pairs: Sequence[Sequence[object]]) -> Dict[str, object]:
    """拒绝同一 JSON 对象中的重复键，避免后值覆盖证据。"""

    result: Dict[str, object] = {}
    for key, value in pairs:
        name = str(key)
        if name in result:
            raise ValueError("JSON 对象包含重复键: %s" % name)
        result[name] = value
    return result


def _assert_finite_json_values(value: object) -> None:
    """递归拒绝标准 JSON 文本中溢出得到的非有限浮点数。"""

    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON 包含非有限浮点数")
    if isinstance(value, dict):
        for nested in value.values():
            _assert_finite_json_values(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_finite_json_values(nested)


def _assert_measurement_environment(report: Dict[str, object]) -> None:
    """拒绝缺失独立进程与调度参数证据的试验。"""

    environment = report.get("measurement_environment")
    if not isinstance(environment, dict):
        raise ValueError("Knowledge 试验缺少 measurement_environment")
    required = (
        "fresh_process",
        "process_instance_id",
        "pid",
        "platform",
        "machine",
        "processor",
        "cpu_count",
        "cpu_affinity",
        "priority",
        "power_plan",
        "background_load",
    )
    if any(field not in environment for field in required):
        raise ValueError("Knowledge 试验缺少调度环境字段")
    pid = environment["pid"]
    instance_id = str(environment["process_instance_id"])
    if (
        environment["fresh_process"] is not True
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or len(instance_id) != 32
        or any(character not in "0123456789abcdef" for character in instance_id)
    ):
        raise ValueError("Knowledge 试验不是有效独立进程")
    if not isinstance(environment["background_load"], dict):
        raise ValueError("Knowledge 试验缺少后台负载快照")
    if environment["platform"] != platform.platform():
        raise ValueError("Knowledge 试验平台与控制进程不一致")
    if int(environment["cpu_count"] or 0) <= 0:
        raise ValueError("Knowledge 试验 CPU 数量非法")
    affinity = environment["cpu_affinity"]
    if (
        not isinstance(affinity, list)
        or len(affinity) != 1
        or isinstance(affinity[0], bool)
        or not isinstance(affinity[0], int)
        or affinity[0] < 0
    ):
        raise ValueError("Knowledge 试验 CPU 亲和性非法")
    load = environment["background_load"].get("cpu_busy_ratio")
    memory = environment["background_load"].get("available_memory_bytes")
    if load is not None and (
        isinstance(load, bool)
        or not isinstance(load, (int, float))
        or not math.isfinite(float(load))
        or not 0.0 <= float(load) <= 1.0
    ):
        raise ValueError("Knowledge 试验 CPU 负载快照非法")
    if memory is not None and (
        isinstance(memory, bool) or not isinstance(memory, int) or memory <= 0
    ):
        raise ValueError("Knowledge 试验可用内存快照非法")
    if os.name == "nt":
        try:
            normalized_plan = str(uuid.UUID(str(environment["power_plan"])))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Windows Knowledge 试验电源计划非法") from exc
        if (
            environment["priority"] != "above_normal"
            or normalized_plan != environment["power_plan"]
            or load is None
            or memory is None
        ):
            raise ValueError("Windows Knowledge 试验调度参数未生效")
    elif environment["priority"] != "platform_default":
        raise ValueError("Knowledge 试验进程优先级与平台不一致")


def _assert_full_trial(
    report: Dict[str, object],
    engine_name: str,
    expected_query_evidence: Dict[str, object],
) -> None:
    """验证查询试验的引擎、逐查询样本和环境证据。"""

    _assert_measurement_environment(report)
    if report.get("schema_version") != 3:
        raise ValueError("查询试验 schema_version 不受支持")
    expected_id = _EXPECTED_ENGINE_IDS[engine_name]
    if report.get("engine", {}).get("id") != expected_id:
        raise ValueError("查询试验返回了错误引擎")
    _assert_engine_implementation(report, engine_name)
    _assert_dataset_report(report.get("dataset"), expected_query_evidence)
    metrics = report.get("metrics")
    samples = report.get("query_latency_samples")
    raw_samples = report.get("query_latency_samples_ms")
    if not isinstance(metrics, dict) or not isinstance(samples, list):
        raise ValueError("查询试验缺少指标或逐查询样本")
    if not isinstance(raw_samples, list):
        raise ValueError("查询试验缺少原始延迟样本")
    expected_count = int(metrics.get("query_count", -1))
    if int(report["dataset"]["evaluated_query_count"]) != expected_count:
        raise ValueError("查询试验 query_count 与数据集记录不一致")
    if len(samples) != expected_count or len(raw_samples) != expected_count:
        raise ValueError("查询延迟样本数与 query_count 不一致")
    if any(
        not isinstance(sample, dict) or not str(sample.get("query_id", "")).strip()
        for sample in samples
    ):
        raise ValueError("查询试验的 query_id 缺失或重复")
    query_ids = [str(sample["query_id"]) for sample in samples]
    if len(set(query_ids)) != expected_count:
        raise ValueError("查询试验的 query_id 缺失或重复")
    if query_ids != expected_query_evidence["query_ids"]:
        raise ValueError("查询试验的问题集合或顺序与真实数据不一致")
    for sample, raw_value in zip(samples, raw_samples):
        latency = float(sample.get("latency_ms", math.nan))
        if (
            not math.isfinite(latency)
            or latency < 0.0
            or latency != float(raw_value)
        ):
            raise ValueError("查询延迟样本包含非法值或映射不一致")
    recomputed = _latency_metrics(raw_samples)
    for metric, expected in recomputed.items():
        actual = float(metrics.get(metric, math.nan))
        if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("查询试验指标与原始延迟不一致: %s" % metric)


def _assert_index_trial(report: Dict[str, object], engine_name: str) -> None:
    """验证建库计时完成后索引完整、可查且派生任务收敛。"""

    _assert_measurement_environment(report)
    if report.get("schema_version") != 1:
        raise ValueError("建库试验 schema_version 不受支持")
    source = load_source_manifest()[SOURCE_ID]
    if report.get("engine_id") != _EXPECTED_ENGINE_IDS[engine_name]:
        raise ValueError("建库计时返回了错误引擎")
    _assert_engine_implementation(report, engine_name)
    if (
        report.get("dataset_id") != SOURCE_ID
        or report.get("dataset_sha256") != source["sha256"]
        or int(report.get("document_count", -1)) != 848
    ):
        raise ValueError("建库计时工作量与固定 CMRC 2018 数据不一致")
    latency_ms = float(report.get("latency_ms", math.nan))
    latency_ns = int(report.get("latency_ns", -1))
    if (
        not math.isfinite(latency_ms)
        or latency_ms <= 0.0
        or latency_ns <= 0
        or not math.isclose(
            latency_ms, latency_ns / 1_000_000.0, rel_tol=0.0, abs_tol=1e-9
        )
    ):
        raise ValueError("建库计时样本非法")
    validation = report.get("validation")
    if not isinstance(validation, dict) or any(
        field not in validation for field in _REQUIRED_INDEX_VALIDATION_FIELDS
    ):
        raise ValueError("建库计时缺少完整性验证字段")
    valid = (
        int(validation["expected_document_count"]) == 848
        and int(validation["active_document_count"]) == 848
        and validation["document_ids_match"] is True
        and validation["sqlite_integrity_ok"] is True
        and validation["index_matches"] is True
        and int(validation["pending_derivative_count"]) == 0
        and int(validation["query_probe_count"]) > 0
        and validation["query_probes_passed"] is True
    )
    if not valid:
        raise ValueError("建库计时后的索引完整性验证失败")


def _assert_engine_implementation(
    report: Dict[str, object], engine_name: str
) -> None:
    """由父进程复算当前引擎路径和实现指纹。"""

    if engine_name == "legacy":
        expected_paths = tuple(legacy_implementation_paths())
        expected_fingerprint = legacy_implementation_fingerprint()
    else:
        expected_paths = tuple(implementation_paths())
        expected_fingerprint = governed_implementation_fingerprint()
    actual_paths = report.get("implementation_paths")
    if not isinstance(actual_paths, list) or tuple(actual_paths) != expected_paths:
        raise ValueError("Knowledge 试验实现路径与当前源码不一致")
    if report.get("implementation_sha256") != expected_fingerprint:
        raise ValueError("Knowledge 试验实现指纹与当前源码不一致")


def _assert_dataset_report(
    dataset: object,
    expected_query_evidence: Dict[str, object],
) -> None:
    """验证查询报告绑定固定数据来源和完整字段。"""

    if not isinstance(dataset, dict):
        raise ValueError("查询试验缺少数据集证据")
    source = load_source_manifest()[SOURCE_ID]
    required_fields = (
        "id",
        "sha256",
        "repository",
        "commit",
        "document_count",
        "available_query_count",
        "evaluated_query_count",
        "query_selection_sha256",
        "real_data_ratio",
    )
    if any(field not in dataset for field in required_fields):
        raise ValueError("查询试验缺少数据集字段")
    selection_hash = str(dataset["query_selection_sha256"])
    valid = (
        dataset["id"] == SOURCE_ID
        and dataset["sha256"] == source["sha256"]
        and dataset["repository"] == source["repository"]
        and dataset["commit"] == source["commit"]
        and int(dataset["document_count"]) == 848
        and int(dataset["available_query_count"]) == 3219
        and int(dataset["evaluated_query_count"])
        == int(expected_query_evidence["evaluated_query_count"])
        and selection_hash == expected_query_evidence["query_selection_sha256"]
        and float(dataset["real_data_ratio"]) == 1.0
    )
    if not valid:
        raise ValueError("查询试验数据集证据与固定 CMRC 2018 不一致")


def _expected_query_evidence(
    dataset_path: Path,
    max_queries: Optional[int],
) -> Dict[str, object]:
    """从固定真实数据复算本轮问题集合和选择哈希。"""

    dataset = load_cmrc2018_dev(Path(dataset_path))
    queries = _select_queries(dataset.queries, max_queries)
    return {
        "evaluated_query_count": len(queries),
        "query_selection_sha256": _query_selection_hash(queries),
        "query_ids": [query.query_id for query in queries],
    }


def _latency_metrics(values: Sequence[float]) -> Dict[str, float]:
    """只从原始延迟样本计算单轮三个计时指标。"""

    samples = [float(value) for value in values]
    if not samples or any(
        not math.isfinite(value) or value <= 0.0 for value in samples
    ):
        raise ValueError("查询延迟样本包含非法值")
    return {
        "latency_ms_mean": sum(samples) / len(samples),
        "latency_ms_p50": percentile(samples, 50),
        "latency_ms_p95": percentile(samples, 95),
    }


def _trial_summary(
    report: Dict[str, object],
    engine_name: str,
    repetition: int,
    position: int,
    warmup: bool,
) -> Dict[str, object]:
    """保留执行顺序与环境，不复制体量较大的查询结果。"""

    dataset = report.get("dataset", {})
    return {
        "engine": engine_name,
        "engine_id": _EXPECTED_ENGINE_IDS[engine_name],
        "round": repetition,
        "position": position,
        "warmup": warmup,
        "generated_at": report.get("generated_at"),
        "dataset_sha256": dataset.get("sha256"),
        "implementation_sha256": report.get("implementation_sha256"),
        "measurement_environment": report["measurement_environment"],
    }


def _assert_measurement_protocol_stable(
    quality_warmup: Sequence[Dict[str, object]],
    execution_order: Sequence[Dict[str, object]],
    index_benchmark: Dict[str, object],
) -> Dict[str, object]:
    """拒绝轮次不足或调度配置在测量期间发生漂移。"""

    index_trials = [
        trial
        for block in index_benchmark["blocks"]
        for trial in block["trials"]
    ]
    if len(execution_order) != REQUIRED_REPETITIONS * 2:
        raise ValueError("查询试验执行次数不符合八轮协议")
    if len(index_trials) != INDEX_BENCHMARK_BLOCKS * 4:
        raise ValueError("建库试验执行次数不符合十六区组协议")
    if len(quality_warmup) != 2 or len(index_benchmark["warmup"]) != 2:
        raise ValueError("性能试验预热次数不符合协议")
    environments = [
        row["measurement_environment"] for row in quality_warmup
    ] + [
        row["measurement_environment"] for row in execution_order
    ] + [
        row["measurement_environment"] for row in index_benchmark["warmup"]
    ] + [row["measurement_environment"] for row in index_trials]
    process_instance_ids = [
        str(environment["process_instance_id"]) for environment in environments
    ]
    process_ids = [int(environment["pid"]) for environment in environments]
    if len(set(process_instance_ids)) != len(environments):
        raise ValueError("性能试验重复使用 process_instance_id")
    # 操作系统只保证同时存活进程的 PID 唯一；短生命周期子进程退出后，
    # Windows 和 POSIX 都允许复用 PID。新进程边界由父进程生成并由子进程
    # 回显的不可预测 process_instance_id 证明，PID 只保留为诊断字段。
    stable_fields = (
        "platform",
        "machine",
        "processor",
        "cpu_count",
        "cpu_affinity",
        "priority",
        "power_plan",
    )
    reference = environments[0]
    for environment in environments[1:]:
        for field in stable_fields:
            if environment[field] != reference[field]:
                raise ValueError("性能试验调度环境发生变化: %s" % field)
    cpu_loads = [
        environment["background_load"].get("cpu_busy_ratio")
        for environment in environments
    ]
    available_memory = [
        environment["background_load"].get("available_memory_bytes")
        for environment in environments
    ]
    return {
        "measured_trial_count": len(environments),
        "unique_process_instance_count": len(set(process_instance_ids)),
        "unique_pid_count": len(set(process_ids)),
        "stable_fields": {field: reference[field] for field in stable_fields},
        "cpu_busy_ratio_samples": cpu_loads,
        "available_memory_bytes_samples": available_memory,
    }


def _aggregate_engine_reports(
    reports: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    """拒绝确定性指标漂移，并保留八个独立轮次的计时。"""

    if len(reports) != REQUIRED_REPETITIONS:
        raise ValueError("每个知识引擎必须提供八轮报告")
    aggregate = copy.deepcopy(reports[0])
    reference = reports[0]
    for report in reports[1:]:
        _assert_repetition_compatible(reference, report)

    for metric in _DETERMINISTIC_METRICS:
        values = [report["metrics"][metric] for report in reports]
        if any(value != values[0] for value in values[1:]):
            raise ValueError("重复运行的确定性指标不一致: %s" % metric)
        aggregate["metrics"][metric] = values[0]

    query_latency_runs = []
    round_timing_metrics = []
    for report in reports:
        values = [float(value) for value in report["query_latency_samples_ms"]]
        if len(values) != int(report["metrics"]["query_count"]):
            raise ValueError("查询延迟样本数与 query_count 不一致")
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("查询延迟样本包含非法值")
        query_latency_runs.append(values)
        recomputed = _latency_metrics(values)
        for metric, expected in recomputed.items():
            actual = float(report["metrics"][metric])
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError("查询试验指标与原始延迟不一致: %s" % metric)
        round_timing_metrics.append(recomputed)
    query_latencies = [value for values in query_latency_runs for value in values]
    aggregate["metrics"]["latency_ms_mean"] = sum(query_latencies) / len(
        query_latencies
    )
    aggregate["metrics"]["latency_ms_p50"] = percentile(query_latencies, 50)
    aggregate["metrics"]["latency_ms_p95"] = percentile(query_latencies, 95)
    aggregate["query_latency_sample_count"] = len(query_latencies)
    aggregate["query_latency_run_sample_counts"] = [
        len(values) for values in query_latency_runs
    ]
    aggregate["query_sequence_ids"] = [
        str(sample["query_id"]) for sample in reports[0]["query_latency_samples"]
    ]
    aggregate["query_latency_runs"] = [
        {
            "round": round_number,
            "samples": list(report["query_latency_samples"]),
        }
        for round_number, report in enumerate(reports, start=1)
    ]
    aggregate.pop("query_latency_samples", None)
    aggregate.pop("query_latency_samples_ms", None)

    timing_samples = {
        metric: [row[metric] for row in round_timing_metrics]
        for metric in _TIMING_METRICS
    }
    aggregate["timing_samples"] = timing_samples
    aggregate["repetitions"] = len(reports)
    aggregate["generated_at_samples"] = [report["generated_at"] for report in reports]
    return aggregate


def _run_balanced_index_benchmark(dataset_path: Path) -> Dict[str, object]:
    """等量预热后用十六个独立平衡区组采集建库样本。"""

    warmup = []
    for position, engine_name in enumerate(("legacy", "governed"), start=1):
        report = _invoke_trial_process(dataset_path, engine_name, "index")
        _assert_index_trial(report, engine_name)
        warmup.append(
            _index_trial_summary(
                report, engine_name, block_number=0, position=position, warmup=True
            )
        )

    block_orders = _balanced_index_orders()
    blocks = []
    samples = {"legacy": [], "governed": []}
    pair_ratios = []
    block_ratios = []
    for block_number, order in enumerate(block_orders, start=1):
        trial_rows = []
        for position, engine_name in enumerate(order, start=1):
            report = _invoke_trial_process(dataset_path, engine_name, "index")
            _assert_index_trial(report, engine_name)
            row = _index_trial_summary(
                report,
                engine_name,
                block_number=block_number,
                position=position,
                warmup=False,
            )
            trial_rows.append(row)
            samples[engine_name].append(float(row["latency_ms"]))
        current_pair_ratios = []
        for left, right in (
            (trial_rows[0], trial_rows[1]),
            (trial_rows[2], trial_rows[3]),
        ):
            pair = {str(left["engine"]): float(left["latency_ms"])}
            pair[str(right["engine"])] = float(right["latency_ms"])
            ratio = pair["governed"] / pair["legacy"]
            pair_ratios.append(ratio)
            current_pair_ratios.append(ratio)
        block_ratio = math.sqrt(current_pair_ratios[0] * current_pair_ratios[1])
        block_ratios.append(block_ratio)
        blocks.append(
            {
                "block": block_number,
                "order": list(order),
                "trials": trial_rows,
                "paired_ratios": current_pair_ratios,
                "block_ratio": block_ratio,
                "legacy_median_ms": statistics.median(
                    float(row["latency_ms"])
                    for row in trial_rows
                    if row["engine"] == "legacy"
                ),
                "governed_median_ms": statistics.median(
                    float(row["latency_ms"])
                    for row in trial_rows
                    if row["engine"] == "governed"
                ),
            }
        )

    legacy_median = statistics.median(samples["legacy"])
    governed_median = statistics.median(samples["governed"])
    block_statistics = _paired_ratio_statistics(
        block_ratios, seed=_BOOTSTRAP_SEED + 100
    )
    return {
        "protocol": {
            "name": "balanced-abba-baab-independent-process-v2",
            "warmup_trials_per_engine": 1,
            "blocks": INDEX_BENCHMARK_BLOCKS,
            "pairs": INDEX_BENCHMARK_BLOCKS * 2,
            "order_seed": _INDEX_ORDER_SEED,
            "orders": [list(order) for order in block_orders],
            "samples_per_engine": len(samples["legacy"]),
            "timer": "perf_counter_ns",
            "fresh_database_per_trial": True,
            "fresh_process_per_trial": True,
            "block_effect": "geometric_mean_of_two_adjacent_pair_ratios",
        },
        "warmup": warmup,
        "blocks": blocks,
        "legacy": {
            "samples_ms": samples["legacy"],
            "median_ms": legacy_median,
        },
        "governed": {
            "samples_ms": samples["governed"],
            "median_ms": governed_median,
        },
        "governed_to_legacy_ratio": governed_median / legacy_median,
        "paired_ratios": pair_ratios,
        "block_ratios": block_ratios,
        "block_statistics": block_statistics,
        "block_win_count": block_statistics["strict_win_count"],
        "pair_win_count": sum(int(ratio < 1.0) for ratio in pair_ratios),
    }


def _index_trial_summary(
    report: Dict[str, object],
    engine_name: str,
    block_number: int,
    position: int,
    warmup: bool,
) -> Dict[str, object]:
    """压缩单次建库报告，同时保留完整性和环境证据。"""

    return {
        "engine": engine_name,
        "engine_id": report["engine_id"],
        "implementation_sha256": report["implementation_sha256"],
        "implementation_paths": report["implementation_paths"],
        "dataset_id": report["dataset_id"],
        "dataset_sha256": report["dataset_sha256"],
        "document_count": report["document_count"],
        "latency_ns": report["latency_ns"],
        "latency_ms": report["latency_ms"],
        "validation": report["validation"],
        "measurement_environment": report["measurement_environment"],
        "block": block_number,
        "position": position,
        "warmup": warmup,
    }


def _build_paired_query_benchmark(
    legacy: Dict[str, object],
    governed: Dict[str, object],
) -> Dict[str, object]:
    """以八个独立轮次为统计单位构造查询延迟配对证据。"""

    metrics = {}
    for metric_index, metric in enumerate(_TIMING_METRICS):
        legacy_samples = [
            float(value) for value in legacy["timing_samples"][metric]
        ]
        governed_samples = [
            float(value) for value in governed["timing_samples"][metric]
        ]
        if (
            len(legacy_samples) != REQUIRED_REPETITIONS
            or len(governed_samples) != REQUIRED_REPETITIONS
        ):
            raise ValueError("查询统计必须使用八个独立轮次")
        round_pairs = []
        for round_number, (before, after) in enumerate(
            zip(legacy_samples, governed_samples), start=1
        ):
            if (
                not math.isfinite(before)
                or not math.isfinite(after)
                or before <= 0.0
                or after <= 0.0
            ):
                raise ValueError("查询轮次包含非法延迟")
            round_pairs.append(
                {
                    "round": round_number,
                    "legacy_ms": before,
                    "governed_ms": after,
                    "governed_to_legacy_ratio": after / before,
                }
            )
        ratios = [
            float(pair["governed_to_legacy_ratio"]) for pair in round_pairs
        ]
        metrics[metric] = {
            "legacy_round_samples_ms": legacy_samples,
            "governed_round_samples_ms": governed_samples,
            "round_pairs": round_pairs,
            **_paired_ratio_statistics(
                ratios, seed=_BOOTSTRAP_SEED + metric_index
            ),
        }
    return {
        "statistical_unit": "independent_round",
        "rounds": REQUIRED_REPETITIONS,
        "ties_count_as_failures": True,
        "metrics": metrics,
    }


def _paired_ratio_statistics(
    ratios: Sequence[float],
    seed: int,
) -> Dict[str, object]:
    """计算严格胜次、精确单侧符号检验和固定种子自助区间。"""

    values = [float(value) for value in ratios]
    if not values or any(
        not math.isfinite(value) or value <= 0.0 for value in values
    ):
        raise ValueError("配对比率必须是非空有限正数")
    wins = sum(int(value < 1.0) for value in values)
    ties = sum(int(value == 1.0) for value in values)
    return {
        "ratios": values,
        "sample_count": len(values),
        "strict_win_count": wins,
        "tie_count": ties,
        "one_sided_sign_test_p_value": _one_sided_sign_test_p_value(
            wins, len(values)
        ),
        "median_ratio": statistics.median(values),
        "bootstrap_median_ratio_ci95": _bootstrap_median_ratio_ci95(
            values, seed
        ),
        "bootstrap_seed": seed,
        "bootstrap_repetitions": _BOOTSTRAP_REPETITIONS,
    }


def _one_sided_sign_test_p_value(wins: int, sample_count: int) -> float:
    """计算胜出概率为二分之一时的精确单侧尾概率。"""

    if sample_count <= 0 or wins < 0 or wins > sample_count:
        raise ValueError("符号检验样本数或胜次非法")
    tail = sum(
        math.comb(sample_count, count)
        for count in range(wins, sample_count + 1)
    )
    return tail / float(2**sample_count)


def _bootstrap_median_ratio_ci95(
    ratios: Sequence[float],
    seed: int,
) -> Sequence[float]:
    """对轮次或区组比率执行固定种子的百分位自助法。"""

    values = tuple(float(value) for value in ratios)
    randomizer = random.Random(seed)
    medians = []
    for _ in range(_BOOTSTRAP_REPETITIONS):
        sample = [randomizer.choice(values) for _ in values]
        medians.append(statistics.median(sample))
    medians.sort()
    return [
        percentile(medians, 2.5),
        percentile(medians, 97.5),
    ]


def _aggregate_security_reports(
    reports: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    """保留每轮安全结果，并以最坏值形成零容忍判定输入。"""

    if len(reports) != REQUIRED_REPETITIONS:
        raise ValueError("安全检查必须提供八轮报告")
    sample_document_ids = [
        str(document_id)
        for report in reports
        for document_id in report.get("sample_document_ids", ())
    ]
    sample_document_indices = [
        int(index)
        for report in reports
        for index in report.get("sample_document_indices", ())
    ]
    context_indices = [report.get("context_index") for report in reports]
    tenant_ids = [report.get("tenant_id") for report in reports]
    owner_user_ids = [report.get("owner_user_id") for report in reports]
    scopes = sorted({str(report.get("private_scope")) for report in reports})
    sensitivities = sorted(
        {str(report.get("private_sensitivity")) for report in reports}
    )
    collection_ids = [report.get("private_collection_id") for report in reports]
    return {
        "repetitions": len(reports),
        "dataset_ids": sorted({str(report["dataset_id"]) for report in reports}),
        "dataset_sha256_values": sorted(
            {str(report["dataset_sha256"]) for report in reports}
        ),
        "synthetic_content_used": any(
            bool(report["synthetic_content_used"]) for report in reports
        ),
        "all_owner_precondition_hit": all(
            bool(report["owner_precondition_hit"]) for report in reports
        ),
        "all_revoke_precondition_hit": all(
            bool(report["revoke_precondition_hit"]) for report in reports
        ),
        "all_stale_index_delete_was_injected": all(
            bool(report["stale_index_delete_was_injected"]) for report in reports
        ),
        "all_derivative_failure_observed": all(
            report.get("derivative_failure_observed") is True for report in reports
        ),
        "all_cross_tenant_rejected": all(
            report.get("cross_tenant_rejected") is True for report in reports
        ),
        "all_source_ref_tamper_precondition_hit": all(
            bool(report["source_ref_tamper_precondition_hit"])
            for report in reports
        ),
        "all_source_ref_tamper_was_injected": all(
            bool(report["source_ref_tamper_was_injected"]) for report in reports
        ),
        "all_source_ref_tamper_rejected": all(
            bool(report["source_ref_tamper_rejected"]) for report in reports
        ),
        "max_unauthorized_result_count": max(
            int(report["unauthorized_result_count"]) for report in reports
        ),
        "max_permission_leakage_rate": max(
            float(report["permission_leakage_rate"]) for report in reports
        ),
        "max_revoked_result_count": max(
            int(report["revoked_result_count"]) for report in reports
        ),
        "max_revoked_pollution_rate": max(
            float(report["revoked_pollution_rate"]) for report in reports
        ),
        "max_source_ref_tamper_resolution_count": max(
            int(report["source_ref_tamper_resolution_count"])
            for report in reports
        ),
        "sample_document_ids": sample_document_ids,
        "sample_document_indices": sample_document_indices,
        "unique_sample_document_count": len(set(sample_document_ids)),
        "unique_sample_index_count": len(set(sample_document_indices)),
        "context_indices": context_indices,
        "tenant_ids": tenant_ids,
        "owner_user_ids": owner_user_ids,
        "private_scopes": scopes,
        "private_sensitivities": sensitivities,
        "private_collection_ids": collection_ids,
        "unique_context_count": len(set(context_indices)),
        "unique_tenant_count": len(set(tenant_ids)),
        "unique_owner_user_count": len(set(owner_user_ids)),
        "unique_collection_count": len(set(collection_ids)),
        "runs": list(reports),
    }


def _assert_repetition_compatible(
    reference: Dict[str, object],
    candidate: Dict[str, object],
) -> None:
    """确认同一引擎的重复运行没有更换代码、数据或问题集合。"""

    if reference["engine"] != candidate["engine"]:
        raise ValueError("重复运行的引擎信息不一致")
    if reference["implementation_sha256"] != candidate["implementation_sha256"]:
        raise ValueError("重复运行期间实现指纹发生变化")
    fields = (
        "id",
        "sha256",
        "repository",
        "commit",
        "document_count",
        "available_query_count",
        "evaluated_query_count",
        "query_selection_sha256",
        "real_data_ratio",
    )
    for field_name in fields:
        if reference["dataset"][field_name] != candidate["dataset"][field_name]:
            raise ValueError("重复运行的数据字段不一致: %s" % field_name)
    reference_query_ids = [
        str(sample["query_id"]) for sample in reference["query_latency_samples"]
    ]
    candidate_query_ids = [
        str(sample["query_id"]) for sample in candidate["query_latency_samples"]
    ]
    if candidate_query_ids != reference_query_ids:
        raise ValueError("重复运行的查询顺序不一致")


def _assert_comparable(
    legacy: Dict[str, object],
    governed: Dict[str, object],
) -> None:
    """拒绝比较来源、版本或问题选择不同的报告。"""

    fields = (
        "id",
        "sha256",
        "repository",
        "commit",
        "document_count",
        "available_query_count",
        "evaluated_query_count",
        "query_selection_sha256",
        "real_data_ratio",
    )
    for field_name in fields:
        if legacy["dataset"][field_name] != governed["dataset"][field_name]:
            raise ValueError("知识报告不可比较，字段不一致: %s" % field_name)
    legacy_query_ids = list(legacy["query_sequence_ids"])
    governed_query_ids = list(governed["query_sequence_ids"])
    if legacy_query_ids != governed_query_ids:
        raise ValueError("知识报告不可比较，查询顺序不一致")


def _build_gates(
    legacy: Dict[str, object],
    governed: Dict[str, object],
    security: Dict[str, object],
    query_benchmark: Dict[str, object],
    index_benchmark: Dict[str, object],
    max_queries: Optional[int],
) -> List[Dict[str, object]]:
    """构造数据、质量、引用、安全和延迟五类独立硬门禁。"""

    source = load_source_manifest()[SOURCE_ID]
    legacy_dataset = legacy["dataset"]
    governed_dataset = governed["dataset"]
    legacy_metrics = legacy["metrics"]
    governed_metrics = governed["metrics"]
    gates: List[Dict[str, object]] = []

    _append_gate(
        gates,
        "data.dataset_id",
        "data",
        governed_dataset["id"],
        SOURCE_ID,
        "equal",
        governed_dataset["id"] == SOURCE_ID,
    )
    for field_name in ("sha256", "repository", "commit"):
        _append_gate(
            gates,
            "data.%s" % field_name,
            "data",
            governed_dataset[field_name],
            source[field_name],
            "equal",
            governed_dataset[field_name] == source[field_name],
        )
    _append_gate(
        gates,
        "data.real_data_ratio",
        "data",
        governed_dataset["real_data_ratio"],
        1.0,
        "equal",
        governed_dataset["real_data_ratio"] == 1.0,
    )
    full_dataset = (
        max_queries is None
        and int(governed_dataset["evaluated_query_count"])
        == int(governed_dataset["available_query_count"])
        == int(governed_metrics["query_count"])
    )
    _append_gate(
        gates,
        "data.full_query_set",
        "data",
        governed_dataset["evaluated_query_count"],
        governed_dataset["available_query_count"],
        "equal_without_max_queries",
        full_dataset,
    )

    for metric in _HIGHER_QUALITY_METRICS:
        before = float(legacy_metrics[metric])
        after = float(governed_metrics[metric])
        _append_gate(
            gates,
            "quality.%s.strict_improvement" % metric,
            "quality",
            after,
            before,
            "greater_than",
            after > before,
        )
        _append_gate(
            gates,
            "quality.%s.absolute_floor" % metric,
            "quality",
            after,
            _QUALITY_FLOORS[metric],
            "greater_than_or_equal",
            after >= _QUALITY_FLOORS[metric],
        )

    for metric in _LOWER_QUALITY_METRICS:
        before = float(legacy_metrics[metric])
        after = float(governed_metrics[metric])
        _append_gate(
            gates,
            "quality.%s.strict_improvement" % metric,
            "quality",
            after,
            before,
            "less_than",
            after < before,
        )
        _append_gate(
            gates,
            "quality.%s.absolute_ceiling" % metric,
            "quality",
            after,
            _QUALITY_CEILINGS[metric],
            "less_than_or_equal",
            after <= _QUALITY_CEILINGS[metric],
        )

    for metric in _CITATION_EXACT_METRICS:
        actual = float(governed_metrics[metric])
        _append_gate(
            gates,
            "citation.%s" % metric,
            "citation",
            actual,
            1.0,
            "equal",
            actual == 1.0,
        )
    answer_span_rate = float(governed_metrics["answer_span_citation_rate_at_10"])
    recall_at_10 = float(governed_metrics["recall_at_10"])
    _append_gate(
        gates,
        "citation.answer_span_covers_retrieved_answers",
        "citation",
        answer_span_rate,
        recall_at_10,
        "greater_than_or_equal",
        answer_span_rate >= recall_at_10,
    )
    returned_hits = int(governed_metrics["returned_hit_count"])
    citation_hits = int(governed_metrics["citation_hit_count"])
    _append_gate(
        gates,
        "citation.every_returned_hit_has_citation",
        "citation",
        citation_hits,
        returned_hits,
        "equal_and_nonzero",
        returned_hits > 0 and citation_hits == returned_hits,
    )

    capabilities = governed["engine"]["capabilities"]
    for capability in (
        "immutable_versions",
        "verified_citations",
        "fact_source_recheck",
        "tenant_user_collection_filtering",
        "citation_protocol_v3",
        "source_ref_hash_binding",
        "citation_resolution_recheck",
    ):
        _append_gate(
            gates,
            "safety.capability.%s" % capability,
            "safety",
            bool(capabilities.get(capability)),
            True,
            "equal",
            capabilities.get(capability) is True,
        )
    _append_gate(
        gates,
        "safety.capability.citation_protocol_version",
        "safety",
        capabilities.get("citation_protocol_version"),
        3,
        "equal",
        capabilities.get("citation_protocol_version") == 3,
    )

    expected_hash = str(source["sha256"])
    _append_gate(
        gates,
        "safety.real_dataset_only",
        "safety",
        {
            "dataset_ids": security["dataset_ids"],
            "dataset_sha256_values": security["dataset_sha256_values"],
            "synthetic_content_used": security["synthetic_content_used"],
        },
        {
            "dataset_ids": [SOURCE_ID],
            "dataset_sha256_values": [expected_hash],
            "synthetic_content_used": False,
        },
        "equal",
        security["dataset_ids"] == [SOURCE_ID]
        and security["dataset_sha256_values"] == [expected_hash]
        and security["synthetic_content_used"] is False,
    )
    for field_name in (
        "all_owner_precondition_hit",
        "all_revoke_precondition_hit",
        "all_stale_index_delete_was_injected",
        "all_derivative_failure_observed",
        "all_cross_tenant_rejected",
        "all_source_ref_tamper_precondition_hit",
        "all_source_ref_tamper_was_injected",
        "all_source_ref_tamper_rejected",
    ):
        _append_gate(
            gates,
            "safety.%s" % field_name,
            "safety",
            security[field_name],
            True,
            "equal",
            security[field_name] is True,
        )
    for field_name in (
        "max_unauthorized_result_count",
        "max_permission_leakage_rate",
        "max_revoked_result_count",
        "max_revoked_pollution_rate",
        "max_source_ref_tamper_resolution_count",
    ):
        actual = float(security[field_name])
        _append_gate(
            gates,
            "safety.%s" % field_name,
            "safety",
            security[field_name],
            0,
            "equal",
            actual == 0.0,
        )

    expected_security_coverage = {
        "unique_sample_document_count": REQUIRED_REPETITIONS * 3,
        "unique_sample_index_count": REQUIRED_REPETITIONS * 3,
        "unique_context_count": REQUIRED_REPETITIONS,
        "unique_tenant_count": REQUIRED_REPETITIONS,
        "unique_owner_user_count": REQUIRED_REPETITIONS,
        "unique_collection_count": REQUIRED_REPETITIONS,
        "private_scopes": ["session", "shared", "user"],
        "private_sensitivities": ["internal", "private", "restricted"],
    }
    actual_security_coverage = {
        field: security[field] for field in expected_security_coverage
    }
    _append_gate(
        gates,
        "safety.rotating_identity_and_document_coverage",
        "safety",
        actual_security_coverage,
        expected_security_coverage,
        "equal",
        actual_security_coverage == expected_security_coverage,
    )

    _append_latency_gates(
        gates, legacy, governed, query_benchmark, index_benchmark
    )
    return gates


def _append_latency_gates(
    gates: List[Dict[str, object]],
    legacy: Dict[str, object],
    governed: Dict[str, object],
    query_benchmark: Dict[str, object],
    index_benchmark: Dict[str, object],
) -> None:
    """同时限制绝对值、相对倍率、显著性和配对效应。"""

    for metric in _TIMING_METRICS:
        before = float(legacy["metrics"][metric])
        after = float(governed["metrics"][metric])
        _append_gate(
            gates,
            "latency.%s.strict_improvement" % metric,
            "latency",
            after,
            before,
            "less_than",
            after < before,
        )
        if metric in _ABSOLUTE_LATENCY_LIMITS_MS:
            _append_single_latency_metric(gates, metric, before, after)
        statistics_report = query_benchmark["metrics"][metric]
        source_ratios = _recompute_query_round_ratios(statistics_report)
        _append_paired_statistic_gates(
            gates,
            metric,
            statistics_report,
            category="latency",
            expected_sample_count=REQUIRED_REPETITIONS,
            source_ratios=source_ratios,
            expected_bootstrap_seed=_BOOTSTRAP_SEED + _TIMING_METRICS.index(metric),
        )
    _append_single_latency_metric(
        gates,
        "index_latency_ms",
        float(legacy["index_latency_ms"]),
        float(governed["index_latency_ms"]),
    )
    _append_gate(
        gates,
        "latency.index_latency_ms.strict_improvement",
        "latency",
        float(governed["index_latency_ms"]),
        float(legacy["index_latency_ms"]),
        "less_than",
        float(governed["index_latency_ms"]) < float(legacy["index_latency_ms"]),
    )
    index_statistics = index_benchmark["block_statistics"]
    verified_index = _recompute_index_benchmark(index_benchmark)
    index_source_ratios = verified_index["block_ratios"]
    if (
        float(legacy["index_latency_ms"]) != verified_index["legacy_median_ms"]
        or float(governed["index_latency_ms"])
        != verified_index["governed_median_ms"]
    ):
        raise ValueError("建库聚合延迟与原始试验不一致")
    index_statistics = _append_paired_statistic_gates(
        gates,
        "index_latency_ms",
        index_statistics,
        category="latency",
        expected_sample_count=INDEX_BENCHMARK_BLOCKS,
        source_ratios=index_source_ratios,
        expected_bootstrap_seed=_BOOTSTRAP_SEED + 100,
    )
    _append_gate(
        gates,
        "latency.index_latency_ms.minimum_effect",
        "latency",
        index_statistics["median_ratio"],
        _MAX_INDEX_MEDIAN_RATIO,
        "less_than_or_equal",
        float(index_statistics["median_ratio"]) <= _MAX_INDEX_MEDIAN_RATIO,
    )


def _append_paired_statistic_gates(
    gates: List[Dict[str, object]],
    metric: str,
    statistics_report: Dict[str, object],
    category: str,
    expected_sample_count: int,
    source_ratios: Sequence[float],
    expected_bootstrap_seed: int,
) -> Dict[str, object]:
    """从原始比率复算统计量，再写成独立门禁。"""

    ratios = statistics_report.get("ratios")
    normalized_source = [float(value) for value in source_ratios]
    if (
        not isinstance(ratios, list)
        or len(ratios) != expected_sample_count
        or [float(value) for value in ratios] != normalized_source
    ):
        raise ValueError("配对统计原始比率数量不符合预注册协议")
    seed = int(statistics_report.get("bootstrap_seed", -1))
    if seed != expected_bootstrap_seed:
        raise ValueError("配对统计 Bootstrap 种子发生变化")
    recomputed = _paired_ratio_statistics(ratios, seed=seed)
    if int(statistics_report.get("bootstrap_repetitions", -1)) != _BOOTSTRAP_REPETITIONS:
        raise ValueError("配对统计 Bootstrap 重复次数发生变化")
    for field in (
        "sample_count",
        "strict_win_count",
        "tie_count",
        "one_sided_sign_test_p_value",
        "median_ratio",
        "bootstrap_median_ratio_ci95",
        "bootstrap_seed",
        "bootstrap_repetitions",
    ):
        if statistics_report.get(field) != recomputed[field]:
            raise ValueError("配对统计字段与原始比率不一致: %s" % field)
    p_value = float(recomputed["one_sided_sign_test_p_value"])
    median_ratio = float(recomputed["median_ratio"])
    ci95 = [
        float(value)
        for value in recomputed["bootstrap_median_ratio_ci95"]
    ]
    _append_gate(
        gates,
        "latency.%s.sign_test" % metric,
        category,
        p_value,
        _MAX_ONE_SIDED_P_VALUE,
        "less_than_or_equal",
        p_value <= _MAX_ONE_SIDED_P_VALUE,
        details={
            "strict_win_count": recomputed["strict_win_count"],
            "sample_count": recomputed["sample_count"],
            "ties_count_as_failures": True,
        },
    )
    _append_gate(
        gates,
        "latency.%s.median_ratio" % metric,
        category,
        median_ratio,
        1.0,
        "less_than",
        median_ratio < 1.0,
    )
    _append_gate(
        gates,
        "latency.%s.bootstrap_ci95_upper" % metric,
        category,
        ci95[1],
        _MAX_PAIRED_CI95_RATIO,
        "less_than",
        ci95[1] < _MAX_PAIRED_CI95_RATIO,
        details={
            "ci95": ci95,
            "bootstrap_seed": recomputed["bootstrap_seed"],
            "bootstrap_repetitions": recomputed["bootstrap_repetitions"],
        },
    )
    return recomputed


def _recompute_query_round_ratios(
    statistics_report: Dict[str, object],
) -> Sequence[float]:
    """从显式轮次配对复算查询比率。"""

    pairs = statistics_report.get("round_pairs")
    if not isinstance(pairs, list) or len(pairs) != REQUIRED_REPETITIONS:
        raise ValueError("查询轮次配对不符合八轮协议")
    ratios = []
    for expected_round, pair in enumerate(pairs, start=1):
        if not isinstance(pair, dict) or pair.get("round") != expected_round:
            raise ValueError("查询轮次配对编号不连续")
        before = float(pair.get("legacy_ms", math.nan))
        after = float(pair.get("governed_ms", math.nan))
        ratio = float(pair.get("governed_to_legacy_ratio", math.nan))
        if (
            not math.isfinite(before)
            or not math.isfinite(after)
            or before <= 0.0
            or after <= 0.0
            or not math.isclose(
                ratio, after / before, rel_tol=0.0, abs_tol=1e-12
            )
        ):
            raise ValueError("查询轮次配对比率与原始计时不一致")
        ratios.append(after / before)
    return ratios


def _recompute_index_block_ratios(
    index_benchmark: Dict[str, object],
) -> Sequence[float]:
    """兼容调用方，只返回从原始试验复算的区组比率。"""

    return _recompute_index_benchmark(index_benchmark)["block_ratios"]


def _recompute_index_benchmark(
    index_benchmark: Dict[str, object],
) -> Dict[str, object]:
    """从六十四次原始试验重建并核对全部建库汇总字段。"""

    blocks = index_benchmark.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != INDEX_BENCHMARK_BLOCKS:
        raise ValueError("建库区组数量不符合预注册协议")
    block_ratios = []
    all_pair_ratios = []
    samples = {"legacy": [], "governed": []}
    observed_orders = []
    for expected_block, block in enumerate(blocks, start=1):
        if not isinstance(block, dict) or block.get("block") != expected_block:
            raise ValueError("建库区组编号不连续")
        trials = block.get("trials")
        order = block.get("order")
        if (
            not isinstance(trials, list)
            or len(trials) != 4
            or not isinstance(order, list)
            or [trial.get("engine") for trial in trials] != order
        ):
            raise ValueError("建库区组试验顺序不一致")
        observed_orders.append(tuple(order))
        for trial in trials:
            samples[str(trial["engine"])].append(float(trial["latency_ms"]))
        current_pair_ratios = []
        for left, right in ((trials[0], trials[1]), (trials[2], trials[3])):
            values = {
                str(left["engine"]): float(left["latency_ms"]),
                str(right["engine"]): float(right["latency_ms"]),
            }
            if set(values) != {"legacy", "governed"} or any(
                not math.isfinite(value) or value <= 0.0
                for value in values.values()
            ):
                raise ValueError("建库区组配对不完整")
            current_pair_ratios.append(values["governed"] / values["legacy"])
        block_ratio = math.sqrt(current_pair_ratios[0] * current_pair_ratios[1])
        expected_legacy_median = statistics.median(
            float(trial["latency_ms"])
            for trial in trials
            if trial["engine"] == "legacy"
        )
        expected_governed_median = statistics.median(
            float(trial["latency_ms"])
            for trial in trials
            if trial["engine"] == "governed"
        )
        if (
            [float(value) for value in block.get("paired_ratios", ())]
            != current_pair_ratios
            or not math.isclose(
                float(block.get("block_ratio", math.nan)),
                block_ratio,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(block.get("legacy_median_ms", math.nan)),
                expected_legacy_median,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(block.get("governed_median_ms", math.nan)),
                expected_governed_median,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("建库区组统计与原始计时不一致")
        all_pair_ratios.extend(current_pair_ratios)
        block_ratios.append(block_ratio)
    expected_orders = tuple(tuple(order) for order in _balanced_index_orders())
    if tuple(observed_orders) != expected_orders:
        raise ValueError("建库区组顺序与预注册协议不一致")
    if [float(value) for value in index_benchmark.get("block_ratios", ())] != block_ratios:
        raise ValueError("建库区组比率汇总与原始计时不一致")
    legacy_median = statistics.median(samples["legacy"])
    governed_median = statistics.median(samples["governed"])
    expected_ratio = governed_median / legacy_median
    if (
        [float(value) for value in index_benchmark.get("paired_ratios", ())]
        != all_pair_ratios
        or [float(value) for value in index_benchmark.get("legacy", {}).get("samples_ms", ())]
        != samples["legacy"]
        or [float(value) for value in index_benchmark.get("governed", {}).get("samples_ms", ())]
        != samples["governed"]
        or not math.isclose(
            float(index_benchmark.get("legacy", {}).get("median_ms", math.nan)),
            legacy_median,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            float(index_benchmark.get("governed", {}).get("median_ms", math.nan)),
            governed_median,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or not math.isclose(
            float(index_benchmark.get("governed_to_legacy_ratio", math.nan)),
            expected_ratio,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        or int(index_benchmark.get("pair_win_count", -1))
        != sum(int(ratio < 1.0) for ratio in all_pair_ratios)
        or int(index_benchmark.get("block_win_count", -1))
        != sum(int(ratio < 1.0) for ratio in block_ratios)
    ):
        raise ValueError("建库汇总字段与原始试验不一致")
    return {
        "legacy_samples_ms": samples["legacy"],
        "governed_samples_ms": samples["governed"],
        "legacy_median_ms": legacy_median,
        "governed_median_ms": governed_median,
        "paired_ratios": all_pair_ratios,
        "block_ratios": block_ratios,
        "governed_to_legacy_ratio": expected_ratio,
    }


def _append_single_latency_metric(
    gates: List[Dict[str, object]],
    metric: str,
    before: float,
    after: float,
) -> None:
    """为单个计时指标生成绝对和相对两项门禁。"""

    absolute_limit = _ABSOLUTE_LATENCY_LIMITS_MS[metric]
    relative_limit = _RELATIVE_LATENCY_LIMITS[metric]
    ratio = after / before if before > 0.0 else math.inf
    _append_gate(
        gates,
        "latency.%s.absolute" % metric,
        "latency",
        after,
        absolute_limit,
        "less_than_or_equal",
        after <= absolute_limit,
    )
    _append_gate(
        gates,
        "latency.%s.relative" % metric,
        "latency",
        ratio,
        relative_limit,
        "less_than_or_equal",
        ratio <= relative_limit,
        details={"legacy": before, "governed": after},
    )


def _append_gate(
    gates: List[Dict[str, object]],
    name: str,
    category: str,
    actual: object,
    expected: object,
    operator: str,
    passed: bool,
    details: Optional[Dict[str, object]] = None,
) -> None:
    """使用统一结构记录可机读的门禁证据。"""

    gate = {
        "name": name,
        "category": category,
        "actual": actual,
        "expected": expected,
        "operator": operator,
        "passed": bool(passed),
    }
    if details:
        gate["details"] = details
    gates.append(gate)


def _comparison_fingerprint(repository_root: Optional[Path] = None) -> str:
    """绑定门禁、两种引擎适配器、数据清单和产品实现。"""

    root = (
        Path(repository_root)
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    digest = hashlib.sha256()
    for relative_name in comparison_paths():
        relative_path = Path(relative_name)
        path = root / relative_path
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    """运行默认全量门禁，并在失败时返回非零退出码。"""

    parser = argparse.ArgumentParser(description="运行真实知识模块八轮对比门禁")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument(
        "--max-queries",
        type=int,
        help="仅用于诊断；使用后完整数据门禁必定失败",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    dataset_path = args.dataset or ensure_cmrc2018_dev()
    report = compare_knowledge_engines(
        dataset_path,
        max_queries=args.max_queries,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes((rendered + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

