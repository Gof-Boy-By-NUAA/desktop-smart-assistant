"""知识对比 schema 4 的控制流、证据绑定和公式测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import statistics
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import benchmarks.knowledge.compare as knowledge_compare
import benchmarks.knowledge.evaluate as knowledge_evaluate
from benchmarks.knowledge.evaluate import GovernedKnowledgeEngine
from benchmarks.retrieval.smart_assistant_baseline import (
    SMART_ASSISTANT_SOURCE_ZIP_SHA256,
    ORIGINAL_MEMORY_STORAGE_AST_SHA256,
    verify_original_memory_storage,
)
from benchmarks.retrieval.dataset import SOURCE_ID, load_source_manifest


class KnowledgeComparisonSchema4Test(unittest.TestCase):
    """夹具只验证门禁控制流，不能替代真实 CMRC 2018 性能证据。"""

    def test_preregistered_protocol_constants_are_fixed(self):
        self.assertEqual(8, knowledge_compare.REQUIRED_REPETITIONS)
        self.assertEqual(16, knowledge_compare.INDEX_BENCHMARK_BLOCKS)
        self.assertEqual(10_000, knowledge_compare._BOOTSTRAP_REPETITIONS)
        self.assertEqual(2026073103, knowledge_compare._BOOTSTRAP_SEED)

    def test_schema4_happy_path_uses_complete_independent_protocol(self):
        factory = _TrialFactory()

        with _patched_benchmark(factory):
            report = knowledge_compare.compare_knowledge_engines(Path("real.json"))

        self.assertTrue(report["passed"])
        self.assertTrue(report["official_full_dataset_gate"])
        self.assertEqual(4, report["schema_version"])
        self.assertEqual(8, report["repetitions"])
        self.assertEqual(16, len(report["execution_order"]))
        self.assertEqual(16, len(report["index_benchmark"]["blocks"]))
        self.assertEqual(
            32, report["index_benchmark"]["protocol"]["samples_per_engine"]
        )
        self.assertEqual(84, report["measurement_protocol"]["measured_trial_count"])
        self.assertEqual(
            84, report["measurement_protocol"]["unique_process_instance_count"]
        )
        self.assertGreaterEqual(
            report["measurement_protocol"]["unique_pid_count"], 1
        )
        self.assertEqual(3219 * 8, report["governed"]["query_latency_sample_count"])
        self.assertEqual(24, report["security"]["unique_sample_document_count"])
        self.assertEqual(24, report["security"]["unique_sample_index_count"])
        self.assertEqual(
            Counter({("legacy", "governed"): 4, ("governed", "legacy"): 4}),
            Counter(tuple(order) for order in report["quality_protocol"]["orders"]),
        )
        self.assertEqual(
            Counter(
                {
                    ("legacy", "governed", "governed", "legacy"): 8,
                    ("governed", "legacy", "legacy", "governed"): 8,
                }
            ),
            Counter(
                tuple(order)
                for order in report["index_benchmark"]["protocol"]["orders"]
            ),
        )
        self.assertEqual(Counter({"full": 18, "index": 66}), factory.mode_counts)

    def test_subset_is_diagnostic_and_cannot_pass_official_gate(self):
        with _patched_benchmark(_TrialFactory()):
            report = knowledge_compare.compare_knowledge_engines(
                Path("real.json"), max_queries=100
            )

        self.assertFalse(report["passed"])
        self.assertFalse(report["official_full_dataset_gate"])
        self.assertFalse(_gate(report, "data.full_query_set")["passed"])

    def test_seven_of_eight_query_wins_passes_sign_gate(self):
        def mutate(report, engine_name, occurrence, _max_queries):
            if engine_name == "governed" and occurrence > 0:
                _replace_latencies(report, 7.0 if occurrence <= 7 else 9.0)

        factory = _TrialFactory(full_mutator=mutate)
        with _patched_benchmark(factory):
            report = knowledge_compare.compare_knowledge_engines(Path("real.json"))

        gate = _gate(report, "latency.latency_ms_mean.sign_test")
        self.assertTrue(gate["passed"])
        self.assertEqual(7, gate["details"]["strict_win_count"])
        self.assertEqual(0.03515625, gate["actual"])

    def test_six_of_eight_query_wins_fails_sign_gate(self):
        def mutate(report, engine_name, occurrence, _max_queries):
            if engine_name == "governed" and occurrence > 0:
                _replace_latencies(report, 7.0 if occurrence <= 6 else 9.0)

        factory = _TrialFactory(full_mutator=mutate)
        with _patched_benchmark(factory):
            report = knowledge_compare.compare_knowledge_engines(Path("real.json"))

        gate = _gate(report, "latency.latency_ms_mean.sign_test")
        self.assertFalse(report["passed"])
        self.assertFalse(gate["passed"])
        self.assertEqual(6, gate["details"]["strict_win_count"])
        self.assertEqual(0.14453125, gate["actual"])

    def test_sign_test_boundaries_and_ties_are_conservative(self):
        cases = (
            ([0.9] * 6 + [1.1] * 2, 6, 0, 0.14453125, False),
            ([0.9] * 7 + [1.1], 7, 0, 0.03515625, True),
            ([0.9] * 11 + [1.1] * 5, 11, 0, 0.1050567626953125, False),
            ([0.9] * 12 + [1.1] * 4, 12, 0, 0.0384063720703125, True),
            ([0.9] * 6 + [1.0] * 2, 6, 2, 0.14453125, False),
        )
        for index, (ratios, wins, ties, p_value, expected) in enumerate(cases):
            with self.subTest(index=index):
                with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
                    stats = knowledge_compare._paired_ratio_statistics(
                        ratios, seed=100 + index
                    )
                    gates = []
                    knowledge_compare._append_paired_statistic_gates(
                        gates,
                        "probe",
                        stats,
                        "latency",
                        len(ratios),
                        ratios,
                        100 + index,
                    )
                self.assertEqual(wins, stats["strict_win_count"])
                self.assertEqual(ties, stats["tie_count"])
                self.assertEqual(p_value, stats["one_sided_sign_test_p_value"])
                self.assertEqual(expected, _gate_list(gates, "latency.probe.sign_test")["passed"])

    def test_paired_statistics_survive_json_roundtrip(self):
        ratios = [0.7, 0.8, 0.9, 1.1, 0.75, 0.82, 0.88, 0.93]
        with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
            statistics_report = knowledge_compare._paired_ratio_statistics(
                ratios, seed=321
            )
            restored = json.loads(json.dumps(statistics_report))
            self.assertEqual(statistics_report, restored)
            knowledge_compare._append_paired_statistic_gates(
                [], "probe", restored, "latency", len(ratios), ratios, 321
            )

    def test_forged_latency_metrics_are_rejected_against_raw_samples(self):
        for metric in knowledge_compare._TIMING_METRICS:
            with self.subTest(metric=metric):
                report = _full_trial("governed", 10001, 1, None)
                report["metrics"][metric] = 1.0
                with self.assertRaisesRegex(ValueError, "指标与原始延迟不一致"):
                    knowledge_compare._assert_full_trial(
                        report, "governed", _query_evidence_for_report(report)
                    )

    def test_forged_precomputed_statistics_are_rejected(self):
        ratios = [0.9] * 8
        mutations = {
            "ratios": [1.2] * 8,
            "sample_count": 999,
            "strict_win_count": 0,
            "tie_count": 8,
            "one_sided_sign_test_p_value": 1.0,
            "median_ratio": 2.0,
            "bootstrap_median_ratio_ci95": (2.0, 2.0),
            "bootstrap_seed": 999,
            "bootstrap_repetitions": 999,
        }
        with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
            reference = knowledge_compare._paired_ratio_statistics(ratios, seed=123)
            for field, value in mutations.items():
                with self.subTest(field=field):
                    stats = copy.deepcopy(reference)
                    stats[field] = value
                    with self.assertRaises(ValueError):
                        knowledge_compare._append_paired_statistic_gates(
                            [], "probe", stats, "latency", 8, ratios, 123
                        )

    def test_unregistered_bootstrap_seed_is_rejected(self):
        ratios = [0.9] * 8
        with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
            stats = knowledge_compare._paired_ratio_statistics(ratios, seed=999)
            with self.assertRaisesRegex(ValueError, "Bootstrap 种子发生变化"):
                knowledge_compare._append_paired_statistic_gates(
                    [], "probe", stats, "latency", 8, ratios, 123
                )

    def test_index_sign_boundary_uses_sixteen_independent_blocks(self):
        for wins, expected in ((11, False), (12, True)):
            with self.subTest(wins=wins):
                ratios = [0.9] * wins + [1.01] * (16 - wins)
                with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
                    benchmark = _index_benchmark_for_ratios(ratios)
                    stats = benchmark["block_statistics"]
                    gates = []
                    knowledge_compare._append_paired_statistic_gates(
                        gates,
                        "index_probe",
                        stats,
                        "latency",
                        16,
                        knowledge_compare._recompute_index_block_ratios(benchmark),
                        knowledge_compare._BOOTSTRAP_SEED + 100,
                    )
                self.assertEqual(
                    expected,
                    _gate_list(gates, "latency.index_probe.sign_test")["passed"],
                )

    def test_sign_test_can_pass_while_bootstrap_ci_still_fails(self):
        ratios = [0.99] * 12 + [2.0] * 4
        seed = knowledge_compare._BOOTSTRAP_SEED + 100
        stats = knowledge_compare._paired_ratio_statistics(ratios, seed=seed)
        gates = []
        knowledge_compare._append_paired_statistic_gates(
            gates, "probe", stats, "latency", 16, ratios, seed
        )
        self.assertTrue(_gate_list(gates, "latency.probe.sign_test")["passed"])
        self.assertFalse(
            _gate_list(gates, "latency.probe.bootstrap_ci95_upper")["passed"]
        )

    def test_index_minimum_effect_boundary_is_five_percent(self):
        for ratio, expected in ((0.96, False), (0.95, True)):
            with self.subTest(ratio=ratio):
                with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
                    benchmark = _index_benchmark_for_ratios([ratio] * 16)
                    legacy, governed, query = _latency_gate_inputs(benchmark)
                    gates = []
                    knowledge_compare._append_latency_gates(
                        gates, legacy, governed, query, benchmark
                    )
                self.assertEqual(
                    expected,
                    _gate_list(
                        gates, "latency.index_latency_ms.minimum_effect"
                    )["passed"],
                )

    def test_tampered_index_block_statistics_are_rejected(self):
        with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
            benchmark = _index_benchmark_for_ratios([0.9] * 16)
        benchmark["blocks"][0]["block_ratio"] = 0.1

        with self.assertRaisesRegex(ValueError, "区组统计与原始计时不一致"):
            knowledge_compare._recompute_index_block_ratios(benchmark)

        with patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200):
            benchmark = _index_benchmark_for_ratios([0.9] * 16)
        benchmark["governed"]["median_ms"] = 1.0
        with self.assertRaisesRegex(ValueError, "汇总字段与原始试验不一致"):
            knowledge_compare._recompute_index_benchmark(benchmark)

    def test_missing_or_false_derivative_failure_is_zero_tolerance(self):
        for mode in ("missing", "false"):
            with self.subTest(mode=mode):
                reports = [_security_report(index) for index in range(8)]
                if mode == "missing":
                    reports[3].pop("derivative_failure_observed")
                else:
                    reports[3]["derivative_failure_observed"] = False
                with _patched_benchmark(
                    _TrialFactory(), security_side_effect=reports
                ):
                    report = knowledge_compare.compare_knowledge_engines(
                        Path("real.json")
                    )
                self.assertFalse(report["passed"])
                self.assertFalse(
                    _gate(report, "safety.all_derivative_failure_observed")["passed"]
                )

    def test_cross_tenant_rejection_is_zero_tolerance(self):
        for mode in ("missing", "false"):
            with self.subTest(mode=mode):
                reports = [_security_report(index) for index in range(8)]
                if mode == "missing":
                    reports[2].pop("cross_tenant_rejected")
                else:
                    reports[2]["cross_tenant_rejected"] = False
                with _patched_benchmark(
                    _TrialFactory(), security_side_effect=reports
                ):
                    report = knowledge_compare.compare_knowledge_engines(
                        Path("real.json")
                    )
                self.assertFalse(report["passed"])
                self.assertFalse(
                    _gate(report, "safety.all_cross_tenant_rejected")["passed"]
                )

    def test_unique_security_documents_are_zero_tolerance(self):
        reports = [_security_report(index) for index in range(8)]
        reports[7]["sample_document_ids"][2] = reports[0]["sample_document_ids"][0]
        with _patched_benchmark(_TrialFactory(), security_side_effect=reports):
            report = knowledge_compare.compare_knowledge_engines(Path("real.json"))
        self.assertFalse(
            _gate(report, "safety.rotating_identity_and_document_coverage")["passed"]
        )

    def test_citation_and_pollution_gates_remain_zero_tolerance(self):
        def mutate(report, engine_name, _occurrence, _max_queries):
            if engine_name == "governed":
                report["metrics"]["citation_resolution_accuracy"] = 0.999

        security = [_security_report(index) for index in range(8)]
        security[4]["revoked_result_count"] = 1
        security[4]["revoked_pollution_rate"] = 1.0
        with _patched_benchmark(
            _TrialFactory(full_mutator=mutate), security_side_effect=security
        ):
            report = knowledge_compare.compare_knowledge_engines(Path("real.json"))

        self.assertFalse(report["passed"])
        self.assertFalse(
            _gate(report, "citation.citation_resolution_accuracy")["passed"]
        )
        self.assertFalse(
            _gate(report, "safety.max_revoked_pollution_rate")["passed"]
        )

    def test_revoked_pollution_counts_only_the_revoked_document(self):
        hits = (
            knowledge_evaluate.KnowledgeBenchmarkHit("revoked", None),
            knowledge_evaluate.KnowledgeBenchmarkHit("still-active", None),
        )
        self.assertEqual(
            (hits[0],),
            knowledge_evaluate._hits_for_document(hits, "revoked"),
        )
        self.assertEqual(
            (),
            knowledge_evaluate._hits_for_document((hits[1],), "revoked"),
        )
        runtime_hit = SimpleNamespace(
            citation=SimpleNamespace(document_id="revoked")
        )
        self.assertEqual(
            (runtime_hit,),
            knowledge_evaluate._hits_for_document((runtime_hit,), "revoked"),
        )
    def test_source_binding_tamper_probes_are_zero_tolerance(self):
        cases = (
            (
                "source_ref_tamper_precondition_hit",
                False,
                "safety.all_source_ref_tamper_precondition_hit",
            ),
            (
                "source_ref_tamper_was_injected",
                False,
                "safety.all_source_ref_tamper_was_injected",
            ),
            (
                "source_ref_tamper_rejected",
                False,
                "safety.all_source_ref_tamper_rejected",
            ),
            (
                "source_ref_tamper_resolution_count",
                1,
                "safety.max_source_ref_tamper_resolution_count",
            ),
        )
        for field, value, gate_name in cases:
            with self.subTest(field=field):
                reports = [_security_report(index) for index in range(8)]
                reports[5][field] = value
                with _patched_benchmark(
                    _TrialFactory(), security_side_effect=reports
                ):
                    report = knowledge_compare.compare_knowledge_engines(
                        Path("real.json")
                    )
                self.assertFalse(report["passed"])
                self.assertFalse(_gate(report, gate_name)["passed"])

    def test_duplicate_process_instance_is_rejected_but_pid_reuse_is_allowed(self):
        factory = _TrialFactory(repeated_identity_field="process_instance_id")
        with _patched_benchmark(factory):
            with self.assertRaisesRegex(ValueError, "重复使用"):
                knowledge_compare.compare_knowledge_engines(Path("real.json"))

        factory = _TrialFactory(repeated_identity_field="pid")
        with _patched_benchmark(factory):
            report = knowledge_compare.compare_knowledge_engines(Path("real.json"))
        self.assertTrue(report["passed"])
        self.assertEqual(1, report["measurement_protocol"]["unique_pid_count"])
        self.assertEqual(
            84,
            report["measurement_protocol"]["unique_process_instance_count"],
        )

    def test_platform_and_background_load_forgery_are_rejected(self):
        report = _full_trial("legacy", 10001, 1, None)
        report["measurement_environment"]["platform"] = "forged-platform"
        with self.assertRaisesRegex(ValueError, "平台与控制进程不一致"):
            knowledge_compare._assert_full_trial(
                report, "legacy", _query_evidence_for_report(report)
            )

        report = _full_trial("legacy", 10001, 1, None)
        report["measurement_environment"]["background_load"][
            "cpu_busy_ratio"
        ] = float("nan")
        with self.assertRaisesRegex(ValueError, "CPU 负载快照非法"):
            knowledge_compare._assert_full_trial(
                report, "legacy", _query_evidence_for_report(report)
            )

    def test_index_trial_rejects_schema_timing_implementation_and_validation_tamper(self):
        mutators = (
            ("schema", lambda row: row.__setitem__("schema_version", 2), "schema_version"),
            ("timing", lambda row: row.__setitem__("latency_ns", 1), "计时样本非法"),
            (
                "paths",
                lambda row: row.__setitem__("implementation_paths", ["wrong.py"]),
                "实现路径",
            ),
            (
                "fingerprint",
                lambda row: row.__setitem__("implementation_sha256", "0" * 64),
                "实现指纹",
            ),
        )
        for name, mutate, message in mutators:
            with self.subTest(name=name):
                report = _index_trial("governed", 10001, 1)
                mutate(report)
                with self.assertRaisesRegex(ValueError, message):
                    knowledge_compare._assert_index_trial(report, "governed")

        for field in knowledge_compare._REQUIRED_INDEX_VALIDATION_FIELDS:
            with self.subTest(missing_field=field):
                report = _index_trial("governed", 10001, 1)
                report["validation"].pop(field)
                with self.assertRaisesRegex(ValueError, "缺少完整性验证字段"):
                    knowledge_compare._assert_index_trial(report, "governed")

        invalid_values = {
            "expected_document_count": 847,
            "active_document_count": 847,
            "document_ids_match": False,
            "sqlite_integrity_ok": False,
            "index_matches": False,
            "pending_derivative_count": 1,
            "query_probe_count": 0,
            "query_probes_passed": False,
        }
        for field, value in invalid_values.items():
            with self.subTest(invalid_field=field):
                report = _index_trial("governed", 10001, 1)
                report["validation"][field] = value
                with self.assertRaisesRegex(ValueError, "索引完整性验证失败"):
                    knowledge_compare._assert_index_trial(report, "governed")

    def test_full_trial_rejects_query_order_dataset_and_implementation_tamper(self):
        mutators = (
            (
                lambda row: row["query_latency_samples"][1].__setitem__(
                    "query_id", "Q_0"
                ),
                "query_id 缺失或重复",
            ),
            (
                lambda row: row["dataset"].__setitem__("sha256", "0" * 64),
                "固定 CMRC 2018",
            ),
            (
                lambda row: row.__setitem__("implementation_sha256", "0" * 64),
                "实现指纹",
            ),
        )
        for mutate, message in mutators:
            report = _full_trial("governed", 10001, 1, None)
            mutate(report)
            with self.assertRaisesRegex(ValueError, message):
                    knowledge_compare._assert_full_trial(
                        report, "governed", _query_evidence_for_report(report)
                    )

    def test_full_trial_rejects_wrong_problem_set_even_with_valid_hash_shape(self):
        report = _full_trial("governed", 10001, 1, None)
        expected = _query_evidence_for_report(report)
        expected["query_ids"] = [
            "REAL_%d" % index for index in range(len(expected["query_ids"]))
        ]

        with self.assertRaisesRegex(ValueError, "问题集合或顺序"):
            knowledge_compare._assert_full_trial(report, "governed", expected)

    def test_full_trial_rejects_query_hash_count_and_sample_mapping_tamper(self):
        report = _full_trial("governed", 10001, 1, None)
        expected = _query_evidence_for_report(report)
        report["dataset"]["query_selection_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "固定 CMRC 2018"):
            knowledge_compare._assert_full_trial(report, "governed", expected)

        report = _full_trial("governed", 10001, 1, None)
        expected = _query_evidence_for_report(report)
        report["query_latency_samples_ms"].pop()
        with self.assertRaisesRegex(ValueError, "样本数"):
            knowledge_compare._assert_full_trial(report, "governed", expected)

        report = _full_trial("governed", 10001, 1, None)
        expected = _query_evidence_for_report(report)
        report["query_latency_samples"][0]["latency_ms"] += 1.0
        with self.assertRaisesRegex(ValueError, "映射不一致"):
            knowledge_compare._assert_full_trial(report, "governed", expected)

    def test_query_order_drift_between_rounds_is_rejected(self):
        def mutate(report, engine_name, occurrence, _max_queries):
            if engine_name == "governed" and occurrence == 2:
                report["query_latency_samples"].reverse()

        with _patched_benchmark(_TrialFactory(full_mutator=mutate)):
            with self.assertRaisesRegex(ValueError, "问题集合或顺序|查询顺序不一致"):
                knowledge_compare.compare_knowledge_engines(Path("real.json"))

    def test_repetition_count_is_fixed_at_eight(self):
        with self.assertRaisesRegex(ValueError, "恰好运行八轮"):
            knowledge_compare.compare_knowledge_engines(
                Path("real.json"), repetitions=3
            )

    def test_implementation_change_during_run_is_rejected(self):
        with _patched_benchmark(_TrialFactory()):
            with patch.object(
                knowledge_compare,
                "_comparison_fingerprint",
                side_effect=("before", "after"),
            ):
                with self.assertRaisesRegex(RuntimeError, "实现指纹发生变化"):
                    knowledge_compare.compare_knowledge_engines(Path("real.json"))

    def test_comparison_fingerprint_is_checkout_path_independent_and_byte_sensitive(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            for relative_name in knowledge_compare.comparison_paths():
                for root in (first, second):
                    target = root / relative_name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((relative_name + "\n固定内容").encode("utf-8"))
            self.assertEqual(
                knowledge_compare._comparison_fingerprint(first),
                knowledge_compare._comparison_fingerprint(second),
            )
            baseline = knowledge_compare._comparison_fingerprint(first)
            for relative_name in knowledge_compare.comparison_paths():
                with self.subTest(relative_name=relative_name):
                    changed = second / relative_name
                    original = changed.read_bytes()
                    changed.write_bytes(original + b"changed")
                    self.assertNotEqual(
                        baseline,
                        knowledge_compare._comparison_fingerprint(second),
                    )
                    changed.write_bytes(original)

    def test_all_comparison_fingerprint_paths_exist_in_repository(self):
        repository_root = Path(knowledge_compare.__file__).resolve().parents[2]
        missing = [
            relative_name
            for relative_name in knowledge_compare.comparison_paths()
            if not (repository_root / relative_name).is_file()
        ]
        self.assertEqual([], missing)

    def test_invoke_trial_process_accepts_one_strict_json_object(self):
        captured = {}

        def run(_command, **kwargs):
            captured.update(kwargs)
            trial_id = kwargs["env"]["SMART_ASSISTANT_KNOWLEDGE_TRIAL_ID"]
            return subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {"measurement_environment": {"process_instance_id": trial_id}}
                ),
                stderr="",
            )

        with patch.object(knowledge_compare.subprocess, "run", side_effect=run):
            report = knowledge_compare._invoke_trial_process(
                Path("real.json"), "legacy", "index"
            )
        self.assertIn("measurement_environment", report)
        self.assertIs(captured["shell"], False)
        self.assertEqual(knowledge_compare._TRIAL_TIMEOUT_SECONDS, captured["timeout"])
        self.assertEqual("utf-8", captured["encoding"])
        self.assertEqual("strict", captured["errors"])
        self.assertIs(captured["capture_output"], True)
        self.assertIs(captured["check"], False)

    def test_invoke_trial_process_rejects_nonstandard_or_ambiguous_json(self):
        cases = (
            ('log\n{"measurement_environment":{"process_instance_id":"TOKEN"}}', "严格 JSON"),
            ('{"measurement_environment":{"process_instance_id":"TOKEN"}}\n{}', "严格 JSON"),
            ("[]", "JSON 对象"),
            ('{"measurement_environment":{"process_instance_id":"TOKEN"},"x":NaN}', "严格 JSON"),
            ('{"measurement_environment":{"process_instance_id":"TOKEN"},"x":Infinity}', "严格 JSON"),
            ('{"measurement_environment":{"process_instance_id":"TOKEN"},"x":1e999}', "严格 JSON"),
            ('{"measurement_environment":{"process_instance_id":"TOKEN"},"x":1,"x":2}', "严格 JSON"),
        )
        for template, message in cases:
            with self.subTest(payload=template):
                def run(_command, **kwargs):
                    trial_id = kwargs["env"]["SMART_ASSISTANT_KNOWLEDGE_TRIAL_ID"]
                    return subprocess.CompletedProcess(
                        [], 0, stdout=template.replace("TOKEN", trial_id), stderr=""
                    )

                with patch.object(
                    knowledge_compare.subprocess, "run", side_effect=run
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        knowledge_compare._invoke_trial_process(
                            Path("real.json"), "legacy", "index"
                        )

    def test_invoke_trial_process_rejects_timeout_and_instance_mismatch(self):
        with patch.object(
            knowledge_compare.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["python"], 1),
        ):
            with self.assertRaisesRegex(RuntimeError, "独立试验超时"):
                knowledge_compare._invoke_trial_process(
                    Path("real.json"), "legacy", "index"
                )

    def test_dataset_load_failure_closes_both_engine_types(self):
        for runner in (
            knowledge_evaluate.run_knowledge_engine,
            knowledge_evaluate.run_knowledge_index_trial,
        ):
            with self.subTest(runner=runner.__name__):
                engine = Mock()
                with patch.object(
                    knowledge_evaluate,
                    "load_cmrc2018_dev",
                    side_effect=ValueError("bad dataset"),
                ):
                    with self.assertRaisesRegex(ValueError, "bad dataset"):
                        runner(Path("bad.json"), engine)
                engine.close.assert_called_once_with()

        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {"measurement_environment": {"process_instance_id": "0" * 32}}
            ),
            stderr="",
        )
        with patch.object(knowledge_compare.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "实例标识不匹配"):
                knowledge_compare._invoke_trial_process(
                    Path("real.json"), "legacy", "index"
                )

    def test_legacy_baseline_is_bound_to_original_smart_assistant_ast(self):
        evidence = verify_original_memory_storage()

        self.assertTrue(evidence["original_memory_storage_ast_verified"])
        self.assertEqual(
            ORIGINAL_MEMORY_STORAGE_AST_SHA256,
            evidence["original_memory_storage_ast_sha256"],
        )
        self.assertEqual(SMART_ASSISTANT_SOURCE_ZIP_SHA256, evidence["smart_assistant_source_zip_sha256"])


class GovernedKnowledgeEngineCitationTest(unittest.TestCase):
    """验证评测适配器执行产品引用回读，而不只是检查字典字段。"""

    def test_search_resolves_each_citation_and_outputs_source_ref_hash(self):
        source_ref = "cmrc2018:DEV_42"
        source_ref_hash = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()
        citation = SimpleNamespace(
            uri="knowledge://tenant/doc/v/1/section/sec/evidence/ev?bytes=0-4"
            "&content_hash=%s&quote_hash=%s&source_ref_hash=%s&citation_version=3"
            % ("1" * 64, "2" * 64, source_ref_hash),
            document_id="doc",
            document_version=1,
            section_id="sec",
            evidence_id="ev",
            source_ref=source_ref,
            source_ref_hash=source_ref_hash,
            citation_version=3,
            byte_start=0,
            byte_end=4,
            content_hash="1" * 64,
            quote_hash="2" * 64,
            quote="正文",
        )
        runtime = Mock()
        runtime.search.return_value = [SimpleNamespace(citation=citation)]
        runtime.resolve_verified_citation.return_value = citation
        engine = object.__new__(GovernedKnowledgeEngine)
        engine._runtime = runtime
        engine._identity = SimpleNamespace()

        hits = engine.search("问题", limit=3)

        runtime.resolve_verified_citation.assert_called_once_with(
            engine._identity, citation.uri
        )
        self.assertEqual(source_ref_hash, hits[0].citation["source_ref_hash"])
        self.assertEqual(3, hits[0].citation["citation_version"])
        self.assertTrue(hits[0].citation_resolution_valid)
        self.assertTrue(hits[0].citation_source_binding_valid)


class _TrialFactory:
    """为 schema 4 控制流生成结构完整、进程身份唯一的报告。"""

    def __init__(
        self,
        full_mutator=None,
        index_mutator=None,
        repeated_identity_field=None,
    ):
        self.full_mutator = full_mutator
        self.index_mutator = index_mutator
        self.repeated_identity_field = repeated_identity_field
        self.calls = []
        self.mode_counts = Counter()
        self.full_occurrences = Counter()
        self.index_occurrences = Counter()
        self._serial = 0

    def __call__(self, _path, engine_name, mode, max_queries=None):
        self._serial += 1
        self.calls.append((engine_name, mode, max_queries))
        self.mode_counts[mode] += 1
        pid = 10000 if self.repeated_identity_field == "pid" else 10000 + self._serial
        instance_serial = 1 if self.repeated_identity_field == "process_instance_id" else self._serial
        if mode == "full":
            occurrence = self.full_occurrences[engine_name]
            self.full_occurrences[engine_name] += 1
            report = _full_trial(
                engine_name, pid, instance_serial, max_queries
            )
            if self.full_mutator:
                self.full_mutator(report, engine_name, occurrence, max_queries)
            return report
        occurrence = self.index_occurrences[engine_name]
        self.index_occurrences[engine_name] += 1
        report = _index_trial(engine_name, pid, instance_serial)
        if self.index_mutator:
            self.index_mutator(report, engine_name, occurrence)
        return report


def _full_trial(engine_name, pid, instance_serial, max_queries):
    """构造父进程能够独立复核的查询试验报告。"""

    governed = engine_name == "governed"
    engine_id = knowledge_compare._EXPECTED_ENGINE_IDS[engine_name]
    available = 3219
    evaluated = max_queries if max_queries is not None else available
    latency = 6.0 if governed else 8.0
    if governed:
        metrics = {
            "query_count": evaluated,
            "recall_at_1": 0.90,
            "recall_at_5": 0.94,
            "recall_at_10": 0.95,
            "mrr_at_10": 0.92,
            "empty_result_rate": 0.01,
            "citation_coverage": 1.0,
            "citation_location_accuracy": 1.0,
            "citation_document_accuracy": 1.0,
            "citation_resolution_accuracy": 1.0,
            "citation_source_binding_accuracy": 1.0,
            "answer_span_citation_rate_at_10": 0.95,
            "returned_hit_count": 500,
            "citation_hit_count": 500,
            "citation_resolution_count": 500,
            "citation_source_binding_count": 500,
        }
        capabilities = {
            "immutable_versions": True,
            "verified_citations": True,
            "fact_source_recheck": True,
            "tenant_user_collection_filtering": True,
            "citation_protocol_v3": True,
            "citation_protocol_version": 3,
            "source_ref_hash_binding": True,
            "citation_resolution_recheck": True,
        }
        implementation_sha256 = knowledge_compare.governed_implementation_fingerprint()
        implementation_paths = list(knowledge_compare.implementation_paths())
    else:
        metrics = {
            "query_count": evaluated,
            "recall_at_1": 0.10,
            "recall_at_5": 0.13,
            "recall_at_10": 0.14,
            "mrr_at_10": 0.12,
            "empty_result_rate": 0.79,
            "citation_coverage": 0.0,
            "citation_location_accuracy": 0.0,
            "citation_document_accuracy": 0.0,
            "citation_resolution_accuracy": 0.0,
            "citation_source_binding_accuracy": 0.0,
            "answer_span_citation_rate_at_10": 0.0,
            "returned_hit_count": 100,
            "citation_hit_count": 0,
            "citation_resolution_count": 0,
            "citation_source_binding_count": 0,
        }
        capabilities = {
            "immutable_versions": False,
            "verified_citations": False,
            "fact_source_recheck": False,
        }
        implementation_sha256 = knowledge_compare.legacy_implementation_fingerprint()
        implementation_paths = list(knowledge_compare.legacy_implementation_paths())
    _set_timing_metrics(metrics, [latency] * evaluated)
    source = load_source_manifest()[SOURCE_ID]
    return {
        "schema_version": 3,
        "generated_at": "2026-07-31T00:00:00+00:00",
        "engine": {"id": engine_id, "capabilities": capabilities},
        "dataset": {
            "id": SOURCE_ID,
            "sha256": source["sha256"],
            "repository": source["repository"],
            "commit": source["commit"],
            "document_count": 848,
            "available_query_count": available,
            "evaluated_query_count": evaluated,
            "query_selection_sha256": "a" * 64,
            "real_data_ratio": 1.0,
        },
        "implementation_sha256": implementation_sha256,
        "implementation_paths": implementation_paths,
        "environment": {"python": "test", "sqlite": "test", "platform": "test"},
        "measurement_environment": _measurement(pid, instance_serial),
        "index_latency_ms": 900.0 if governed else 1000.0,
        "query_latency_samples_ms": [latency] * evaluated,
        "query_latency_samples": [
            {"query_id": "Q_%d" % index, "latency_ms": latency}
            for index in range(evaluated)
        ],
        "metrics": metrics,
    }


def _index_trial(engine_name, pid, instance_serial):
    """构造包含计时后完整性证据的建库试验报告。"""

    governed = engine_name == "governed"
    latency_ms = 900.0 if governed else 1000.0
    source = load_source_manifest()[SOURCE_ID]
    if governed:
        implementation_sha256 = knowledge_compare.governed_implementation_fingerprint()
        implementation_paths = list(knowledge_compare.implementation_paths())
    else:
        implementation_sha256 = knowledge_compare.legacy_implementation_fingerprint()
        implementation_paths = list(knowledge_compare.legacy_implementation_paths())
    return {
        "schema_version": 1,
        "engine_id": knowledge_compare._EXPECTED_ENGINE_IDS[engine_name],
        "implementation_sha256": implementation_sha256,
        "implementation_paths": implementation_paths,
        "dataset_id": SOURCE_ID,
        "dataset_sha256": source["sha256"],
        "document_count": 848,
        "latency_ns": int(latency_ms * 1_000_000),
        "latency_ms": latency_ms,
        "validation": {
            "expected_document_count": 848,
            "active_document_count": 848,
            "document_ids_match": True,
            "sqlite_integrity_ok": True,
            "index_matches": True,
            "pending_derivative_count": 0,
            "query_probe_count": 3,
            "query_probes_passed": True,
        },
        "measurement_environment": _measurement(pid, instance_serial),
    }


def _measurement(pid, instance_serial):
    """构造与当前控制进程平台一致的调度快照。"""

    windows = os.name == "nt"
    return {
        "fresh_process": True,
        "process_instance_id": "%032x" % instance_serial,
        "pid": pid,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity": [0],
        "priority": "above_normal" if windows else "platform_default",
        "power_plan": (
            "00000000-0000-0000-0000-000000000001" if windows else None
        ),
        "background_load": {
            "cpu_busy_ratio": 0.1 if windows else None,
            "available_memory_bytes": 2**30 if windows else None,
        },
    }


def _replace_latencies(report, latency):
    """同步替换原始延迟、逐查询映射和子报告计时指标。"""

    count = int(report["metrics"]["query_count"])
    values = [latency] * count
    report["query_latency_samples_ms"] = values
    report["query_latency_samples"] = [
        {"query_id": "Q_%d" % index, "latency_ms": latency}
        for index in range(count)
    ]
    _set_timing_metrics(report["metrics"], values)


def _set_timing_metrics(metrics, values):
    """从原始样本同步计算测试报告中的三个计时字段。"""

    metrics.update(knowledge_compare._latency_metrics(values))


def _query_evidence_for_report(report):
    """从测试报告生成父进程预期的问题集合证据。"""

    return {
        "evaluated_query_count": report["dataset"]["evaluated_query_count"],
        "query_selection_sha256": report["dataset"]["query_selection_sha256"],
        "query_ids": [
            sample["query_id"] for sample in report["query_latency_samples"]
        ],
    }


def _fake_query_evidence(_path, max_queries):
    """为控制流测试提供与夹具一致的问题选择，不冒充真实数据。"""

    count = max_queries if max_queries is not None else 3219
    return {
        "evaluated_query_count": count,
        "query_selection_sha256": "a" * 64,
        "query_ids": ["Q_%d" % index for index in range(count)],
    }


def _security_report(context_index):
    """构造二十四文档和身份维度均不重复的安全报告。"""

    source = load_source_manifest()[SOURCE_ID]
    indices = [context_index * 3 + offset for offset in range(3)]
    return {
        "dataset_id": SOURCE_ID,
        "dataset_sha256": source["sha256"],
        "sample_document_ids": ["DEV_%d" % index for index in indices],
        "sample_document_indices": indices,
        "context_index": context_index,
        "tenant_id": "tenant-%d" % context_index,
        "owner_user_id": "owner-%d" % context_index,
        "other_user_id": "other-%d" % context_index,
        "private_scope": ("user", "session", "shared")[context_index % 3],
        "private_sensitivity": ("private", "internal", "restricted")[
            context_index % 3
        ],
        "private_collection_id": "collection-%d" % context_index,
        "synthetic_content_used": False,
        "owner_precondition_hit": True,
        "cross_tenant_rejected": True,
        "revoke_precondition_hit": True,
        "unauthorized_result_count": 0,
        "permission_leakage_rate": 0.0,
        "revoked_result_count": 0,
        "revoked_pollution_rate": 0.0,
        "stale_index_delete_was_injected": True,
        "derivative_failure_observed": True,
        "source_ref_tamper_precondition_hit": True,
        "source_ref_tamper_was_injected": True,
        "source_ref_tamper_rejected": True,
        "source_ref_tamper_resolution_count": 0,
    }


def _run_fake_security(_path, sample_document_indices=None, context_index=0):
    """让默认安全夹具服从比较器传入的轮次和文档计划。"""

    report = _security_report(context_index)
    indices = list(sample_document_indices)
    report["sample_document_indices"] = indices
    report["sample_document_ids"] = ["DEV_%d" % index for index in indices]
    return report


def _index_benchmark_for_ratios(ratios):
    """从指定区组比率构造可由原始四次计时复算的基准。"""

    blocks = []
    legacy_samples = []
    governed_samples = []
    for block_number, (order, ratio) in enumerate(
        zip(knowledge_compare._balanced_index_orders(), ratios), start=1
    ):
        trials = []
        for position, engine_name in enumerate(order, start=1):
            latency = 1000.0 if engine_name == "legacy" else 1000.0 * ratio
            trials.append({"engine": engine_name, "latency_ms": latency})
            (legacy_samples if engine_name == "legacy" else governed_samples).append(
                latency
            )
        blocks.append(
            {
                "block": block_number,
                "order": list(order),
                "trials": trials,
                "paired_ratios": [ratio, ratio],
                "block_ratio": ratio,
                "legacy_median_ms": 1000.0,
                "governed_median_ms": 1000.0 * ratio,
            }
        )
    legacy_median = statistics.median(legacy_samples)
    governed_median = statistics.median(governed_samples)
    paired_ratios = [ratio for ratio in ratios for _ in range(2)]
    return {
        "blocks": blocks,
        "legacy": {"samples_ms": legacy_samples, "median_ms": legacy_median},
        "governed": {
            "samples_ms": governed_samples,
            "median_ms": governed_median,
        },
        "governed_to_legacy_ratio": governed_median / legacy_median,
        "paired_ratios": paired_ratios,
        "block_ratios": list(ratios),
        "block_statistics": knowledge_compare._paired_ratio_statistics(
            ratios, seed=knowledge_compare._BOOTSTRAP_SEED + 100
        ),
        "block_win_count": sum(int(ratio < 1.0) for ratio in ratios),
        "pair_win_count": sum(int(ratio < 1.0) for ratio in paired_ratios),
    }


def _latency_gate_inputs(index_benchmark):
    """构造只包含延迟门禁所需字段的两引擎聚合输入。"""

    verified = knowledge_compare._recompute_index_benchmark(index_benchmark)
    legacy = {
        "metrics": {
            "latency_ms_mean": 8.0,
            "latency_ms_p50": 8.0,
            "latency_ms_p95": 8.0,
        },
        "timing_samples": {
            "latency_ms_mean": [8.0] * 8,
            "latency_ms_p50": [8.0] * 8,
            "latency_ms_p95": [8.0] * 8,
        },
        "index_latency_ms": verified["legacy_median_ms"],
    }
    governed = {
        "metrics": {
            "latency_ms_mean": 6.0,
            "latency_ms_p50": 6.0,
            "latency_ms_p95": 6.0,
        },
        "timing_samples": {
            "latency_ms_mean": [6.0] * 8,
            "latency_ms_p50": [6.0] * 8,
            "latency_ms_p95": [6.0] * 8,
        },
        "index_latency_ms": verified["governed_median_ms"],
    }
    return (
        legacy,
        governed,
        knowledge_compare._build_paired_query_benchmark(legacy, governed),
    )


def _patched_benchmark(factory, security_side_effect=None):
    """替换耗时边界，同时保留 schema 4 父进程的全部验证逻辑。"""

    security = patch.object(
        knowledge_compare,
        "run_real_data_security_checks",
        side_effect=(
            security_side_effect
            if security_side_effect is not None
            else _run_fake_security
        ),
    )
    return _CombinedPatches(
        patch.object(
            knowledge_compare, "_invoke_trial_process", side_effect=factory
        ),
        security,
        patch.object(
            knowledge_compare, "_comparison_fingerprint", return_value="f" * 64
        ),
        patch.object(
            knowledge_compare,
            "_expected_query_evidence",
            side_effect=_fake_query_evidence,
        ),
        patch.object(knowledge_compare, "_BOOTSTRAP_REPETITIONS", 200),
    )


class _CombinedPatches:
    """把多个补丁组合成一个确定性上下文。"""

    def __init__(self, *patchers):
        self._patchers = patchers

    def __enter__(self):
        for patcher in self._patchers:
            patcher.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for patcher in reversed(self._patchers):
            patcher.stop()


def _gate(report, name):
    """按名称读取报告中的门禁。"""

    return next(gate for gate in report["gates"] if gate["name"] == name)


def _gate_list(gates, name):
    """按名称读取未封装报告的门禁列表。"""

    return next(gate for gate in gates if gate["name"] == name)


if __name__ == "__main__":
    unittest.main()




