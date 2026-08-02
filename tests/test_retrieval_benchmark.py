"""真实检索评测工具的单元测试。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.retrieval.compare import (
    _aggregate_reports,
    compare_engines,
)
from benchmarks.retrieval.dataset import (
    RetrievalDataset,
    RetrievalDocument,
    RetrievalQuery,
    load_cmrc2018_dev,
)
from benchmarks.retrieval.evaluate import run_engine
from benchmarks.retrieval.improved_engine import (
    _IMPLEMENTATION_PATHS,
    implementation_fingerprint,
)
from benchmarks.retrieval.metrics import QueryEvaluation, calculate_metrics, percentile
from benchmarks.retrieval.verify import verify_report


class RetrievalDatasetTest(unittest.TestCase):
    """验证数据转换和指标计算，不把夹具当性能证据。"""

    def test_cmrc_shape_is_converted_to_retrieval_pairs(self):
        payload = [
            {
                "context_id": "DOC_1",
                "context_text": "北京是中国的首都。",
                "title": "北京",
                "qas": [
                    {
                        "query_id": "QUERY_1",
                        "query_text": "中国的首都是哪里？",
                        "answers": ["北京"],
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            dataset = load_cmrc2018_dev(path, verify_source=False)

        self.assertEqual("DOC_1", dataset.documents[0].document_id)
        self.assertEqual(("DOC_1",), dataset.queries[0].relevant_document_ids)

    def test_empty_title_does_not_discard_valid_context(self):
        payload = [
            {
                "context_id": "DOC_2",
                "context_text": "有效正文",
                "title": "",
                "qas": [
                    {
                        "query_id": "QUERY_2",
                        "query_text": "正文是否有效？",
                        "answers": ["有效"],
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            dataset = load_cmrc2018_dev(path, verify_source=False)

        self.assertEqual("", dataset.documents[0].title)
        self.assertEqual("有效正文", dataset.documents[0].text)

    def test_metrics_use_human_relevance_document_ids(self):
        dataset_query = load_query_fixture()
        evaluations = [
            QueryEvaluation(dataset_query, ["wrong", "DOC_1"], 10.0),
            QueryEvaluation(dataset_query, [], 30.0),
        ]

        metrics = calculate_metrics(evaluations)

        self.assertEqual(0.0, metrics["recall_at_1"])
        self.assertEqual(0.5, metrics["recall_at_5"])
        self.assertEqual(0.25, metrics["mrr_at_10"])
        self.assertEqual(0.5, metrics["empty_result_rate"])
        self.assertEqual(20.0, metrics["latency_ms_p50"])

    def test_percentile_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            percentile([], 95)
        with self.assertRaises(ValueError):
            percentile([1.0], 101)

    def test_repeated_reports_use_median_timing(self):
        reports = [timing_report(10.0), timing_report(50.0), timing_report(20.0)]

        aggregate = _aggregate_reports(reports)

        self.assertEqual(20.0, aggregate["index_latency_ms"])
        self.assertEqual(20.0, aggregate["metrics"]["latency_ms_p95"])
        self.assertEqual(3, aggregate["repetitions"])

    def test_repeated_reports_reject_quality_drift(self):
        reports = [timing_report(10.0), timing_report(20.0)]
        reports[1]["metrics"]["recall_at_10"] = 0.8

        with self.assertRaises(ValueError):
            _aggregate_reports(reports)

    def test_improved_engine_fingerprint_binds_required_files(self):
        self.assertEqual(
            (
                "benchmarks/retrieval/improved_engine.py",
                "agent/retrieval/lexical.py",
                "agent/memory/governance/contracts.py",
            ),
            _IMPLEMENTATION_PATHS,
        )
        fingerprint = implementation_fingerprint()
        self.assertEqual(64, len(fingerprint))
        int(fingerprint, 16)

    def test_run_engine_emits_schema_v2_and_stable_fingerprint(self):
        engine = FakeEngine(["a" * 64])
        with patch(
            "benchmarks.retrieval.evaluate.load_cmrc2018_dev",
            return_value=synthetic_dataset(),
        ):
            report = run_engine(Path("unused.json"), engine)

        self.assertEqual(2, report["schema_version"])
        self.assertEqual("fake-retrieval", report["engine"]["id"])
        self.assertEqual("a" * 64, report["engine"]["implementation_sha256"])
        self.assertTrue(engine.closed)

    def test_run_engine_rejects_implementation_change(self):
        engine = FakeEngine(["a" * 64, "b" * 64])
        with patch(
            "benchmarks.retrieval.evaluate.load_cmrc2018_dev",
            return_value=synthetic_dataset(),
        ):
            with self.assertRaisesRegex(RuntimeError, "实现指纹发生变化"):
                run_engine(Path("unused.json"), engine)

        self.assertTrue(engine.closed)

    def test_repeated_reports_reject_schema_drift(self):
        reports = [timing_report(10.0), timing_report(20.0)]
        reports[1]["schema_version"] = 3

        with self.assertRaisesRegex(ValueError, "schema_version"):
            _aggregate_reports(reports)

    def test_repeated_reports_reject_engine_id_drift(self):
        reports = [timing_report(10.0), timing_report(20.0)]
        reports[1]["engine"]["id"] = "different-engine"

        with self.assertRaisesRegex(ValueError, "engine.id"):
            _aggregate_reports(reports)

    def test_repeated_reports_reject_implementation_drift(self):
        reports = [timing_report(10.0), timing_report(20.0)]
        reports[1]["engine"]["implementation_sha256"] = "b" * 64

        with self.assertRaisesRegex(ValueError, "implementation_sha256"):
            _aggregate_reports(reports)

    def test_compare_report_binds_schema_v3_and_implementation(self):
        baseline = comparison_report("baseline", "a" * 64, 20.0)
        improved = comparison_report("improved", "b" * 64, 10.0)
        with patch(
            "benchmarks.retrieval.compare.run_baseline",
            return_value=baseline,
        ), patch(
            "benchmarks.retrieval.compare.run_improved",
            return_value=improved,
        ), patch(
            "benchmarks.retrieval.compare.comparison_implementation_fingerprint",
            return_value="c" * 64,
        ):
            report = compare_engines(Path("unused.json"), repetitions=1)

        self.assertEqual(3, report["schema_version"])
        self.assertEqual("c" * 64, report["comparison_implementation_sha256"])
        self.assertIn("paired_statistics", report)

    def test_paired_timing_gate_survives_common_mode_slowdown(self):
        baseline_values = [2.0, 2.2, 3.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]
        improved_values = [1.0, 1.1, 1.5, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]
        baseline_reports = [
            comparison_report("baseline", "a" * 64, value)
            for value in baseline_values
        ]
        improved_reports = [
            comparison_report("improved", "b" * 64, value)
            for value in improved_values
        ]
        with patch(
            "benchmarks.retrieval.compare.run_baseline",
            side_effect=baseline_reports,
        ), patch(
            "benchmarks.retrieval.compare.run_improved",
            side_effect=improved_reports,
        ), patch(
            "benchmarks.retrieval.compare.comparison_implementation_fingerprint",
            return_value="c" * 64,
        ):
            report = compare_engines(Path("unused.json"), repetitions=11)

        latency_gate = next(
            gate for gate in report["gates"]
            if gate["metric"] == "latency_ms_mean"
        )
        self.assertTrue(latency_gate["passed"])
        self.assertEqual(11, latency_gate["details"]["strict_win_count"])
        self.assertEqual(0.5, latency_gate["details"]["median_paired_ratio"])

    def test_independent_verifier_accepts_current_formal_report(self):
        root = Path(__file__).resolve().parents[1]
        verification = verify_report(
            root / "benchmarks/results/cmrc2018-comparison.json",
            root / "benchmarks/.cache/cmrc2018-source/data/cmrc2018_dev.json",
        )
        self.assertTrue(verification["passed"])

    def test_independent_verifier_rejects_tampered_timing_sample(self):
        root = Path(__file__).resolve().parents[1]
        report = json.loads(
            (root / "benchmarks/results/cmrc2018-comparison.json").read_text(
                encoding="utf-8"
            )
        )
        report["improved"]["timing_samples"]["latency_ms_mean"][0] *= 10
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered.json"
            tampered.write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )
            verification = verify_report(
                tampered,
                root / "benchmarks/.cache/cmrc2018-source/data/cmrc2018_dev.json",
            )
        self.assertFalse(verification["passed"])
        paired_check = next(
            check for check in verification["checks"]
            if check["name"] == "paired.latency_ms_mean"
        )
        self.assertFalse(paired_check["passed"])

    def test_compare_rejects_implementation_change(self):
        baseline = comparison_report("baseline", "a" * 64, 20.0)
        improved = comparison_report("improved", "b" * 64, 10.0)
        with patch(
            "benchmarks.retrieval.compare.run_baseline",
            return_value=baseline,
        ), patch(
            "benchmarks.retrieval.compare.run_improved",
            return_value=improved,
        ), patch(
            "benchmarks.retrieval.compare.comparison_implementation_fingerprint",
            side_effect=("c" * 64, "d" * 64),
        ):
            with self.assertRaisesRegex(RuntimeError, "基准实现指纹发生变化"):
                compare_engines(Path("unused.json"), repetitions=1)


def load_query_fixture():
    """构造只用于验证公式的查询对象。"""

    from benchmarks.retrieval.dataset import RetrievalQuery

    return RetrievalQuery(
        query_id="QUERY_1",
        text="中国的首都是哪里？",
        relevant_document_ids=("DOC_1",),
    )


def timing_report(value):
    """构造用于验证重复运行聚合的报告。"""

    return {
        "schema_version": 2,
        "engine": {
            "id": "test-engine",
            "implementation_sha256": "a" * 64,
        },
        "index_latency_ms": value,
        "metrics": {
            "query_count": 10,
            "recall_at_1": 0.5,
            "recall_at_5": 0.7,
            "recall_at_10": 0.9,
            "mrr_at_10": 0.6,
            "empty_result_rate": 0.1,
            "latency_ms_mean": value,
            "latency_ms_p50": value,
            "latency_ms_p95": value,
        },
    }


def comparison_report(engine_id, implementation_sha256, value):
    """构造具备可比数据身份的单轮报告。"""

    report = timing_report(value)
    report["engine"] = {
        "id": engine_id,
        "implementation_sha256": implementation_sha256,
    }
    report["dataset"] = {
        "id": "fixture",
        "sha256": "d" * 64,
        "query_selection_sha256": "e" * 64,
    }
    return report


def synthetic_dataset():
    """构造不访问外部数据的单文档检索夹具。"""

    return RetrievalDataset(
        source_id="fixture",
        source_sha256="d" * 64,
        documents=(RetrievalDocument("DOC_1", "北京", "北京是中国的首都。"),),
        queries=(
            RetrievalQuery("QUERY_1", "中国的首都是哪里？", ("DOC_1",)),
        ),
    )


class FakeEngine:
    """提供可控实现指纹的最小评测引擎。"""

    engine_id = "fake-retrieval"
    capabilities = {"fixture": True}

    def __init__(self, fingerprints):
        self._fingerprints = tuple(fingerprints)
        self._fingerprint_reads = 0
        self.closed = False

    @property
    def implementation_sha256(self):
        index = min(self._fingerprint_reads, len(self._fingerprints) - 1)
        self._fingerprint_reads += 1
        return self._fingerprints[index]

    def index(self, documents):
        self.documents = tuple(documents)

    def search(self, query, limit=10):
        return ("DOC_1",)

    def close(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
