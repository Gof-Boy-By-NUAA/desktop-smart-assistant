"""技能选择基准的数据门禁、指标和产品选择链测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from benchmarks.skills.dataset import (
    DEFAULT_DATASET_PATH,
    EXPECTED_DATASET_SHA256,
    AnnotationRule,
    FetchProvenance,
    SkillSelectionCase,
    SkillSelectionDataset,
    SkillSelectionDatasetError,
    load_skill_selection_dataset,
    recompute_silver_labels,
)
from benchmarks.skills.metrics import (
    SelectionObservation,
    calculate_selection_metrics,
)
from benchmarks.skills.runner import (
    implementation_fingerprint,
    main,
    run_skill_selection_benchmark,
    run_skill_selection_benchmark_from_path,
)


def _case(
    number: int,
    title: str,
    expected: tuple[str, ...],
) -> SkillSelectionCase:
    return SkillSelectionCase(
        number=number,
        node_id="I_test_%d" % number,
        title=title,
        html_url="https://github.com/zhayujie/SmartAssistant/issues/%d" % number,
        state="open",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        pull_request=False,
        expected_skill_names=expected,
        fetch_provenance=FetchProvenance(
            http_status=200,
            response_date="Thu, 01 Jan 2026 00:00:00 GMT",
            etag='"test-only"',
            fetched_at="2026-01-01T00:00:01Z",
            raw_response_path="test-only-not-loaded.json",
            raw_response_sha256="0" * 64,
            canonical_sha256="0" * 64,
        ),
    )


def _synthetic_dataset() -> SkillSelectionDataset:
    rules = (
        AnnotationRule("knowledge-wiki", ("知识库", "rag", "knowledge")),
        AnnotationRule("image-generation", ("画图", "文生图", "image")),
    )
    return SkillSelectionDataset(
        schema_version=2,
        dataset_id="test-only-synthetic-skill-selection",
        snapshot_generated_at="2026-01-02T00:00:00Z",
        source={
            "repository": "https://github.com/zhayujie/SmartAssistant",
            "api": "https://api.github.com/repos/zhayujie/SmartAssistant/issues/{number}",
            "license": "test-only synthetic fixture",
            "snapshot_fields": (),
        },
        label_tier="deterministic_silver",
        normalization="NFKC casefold",
        rules=rules,
        design_cases=(
            _case(1, "知识库功能", ("knowledge-wiki",)),
            _case(2, "画图功能", ("image-generation",)),
        ),
        evaluation_cases=(
            _case(10, "请添加知识库", ("knowledge-wiki",)),
            _case(11, "image 模型报错", ("image-generation",)),
            _case(
                12,
                "增加 RAG 和免费文生图",
                ("image-generation", "knowledge-wiki"),
            ),
            _case(13, "QQ Channel 连接失败", ()),
        ),
        sha256="0" * 64,
        design_split_sha256="1" * 64,
        evaluation_split_sha256="2" * 64,
        provenance_complete=True,
    )


def _write_json(path: Path, document: object) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _strict_case(
    root: Path,
    number: int,
    title: str,
    expected: list[str],
) -> dict:
    record = {
        "number": number,
        "node_id": "I_test_%d" % number,
        "title": title,
        "html_url": "https://github.com/zhayujie/SmartAssistant/issues/%d" % number,
        "state": "open",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "pull_request": False,
    }
    raw_record = dict(record)
    raw_record.pop("pull_request")
    raw_record["body"] = None
    raw_path = root / "raw" / ("%d.json" % number)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = json.dumps(
        raw_record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    raw_path.write_bytes(raw_payload)
    canonical_payload = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **record,
        "expected_skill_names": expected,
        "fetch_provenance": {
            "http_status": 200,
            "response_date": "Thu, 01 Jan 2026 00:00:01 GMT",
            "etag": '"fixture-%d"' % number,
            "fetched_at": "2026-01-01T00:00:02Z",
            "raw_response_path": "raw/%d.json" % number,
            "raw_response_sha256": hashlib.sha256(raw_payload).hexdigest(),
            "canonical_sha256": hashlib.sha256(canonical_payload).hexdigest(),
        },
    }


def _strict_document(root: Path) -> dict:
    return {
        "schema_version": 2,
        "dataset_id": "test-only-strict-skill-selection",
        "snapshot_generated_at": "2026-01-01T00:00:03Z",
        "source": {
            "repository": "https://github.com/zhayujie/SmartAssistant",
            "api": "https://api.github.com/repos/zhayujie/SmartAssistant/issues/{number}",
            "license": "public GitHub issue metadata",
            "snapshot_fields": [
                "number",
                "node_id",
                "title",
                "html_url",
                "state",
                "created_at",
                "updated_at",
                "pull_request",
            ],
            "body_or_comments_included": False,
            "provenance_complete": True,
        },
        "annotation_policy": {
            "label_tier": "deterministic_silver",
            "normalization": "NFKC casefold",
            "multi_label_allowed": True,
            "rules": [
                {"skill_name": "knowledge-wiki", "markers": ["知识库"]},
                {"skill_name": "image-generation", "markers": ["image"]},
            ],
        },
        "design_cases": [
            _strict_case(root, 1, "知识库设计", ["knowledge-wiki"])
        ],
        "evaluation_cases": [
            _strict_case(root, 2, "image failure", ["image-generation"])
        ],
    }


def test_current_snapshot_is_frozen_but_missing_fetch_provenance():
    payload = DEFAULT_DATASET_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_DATASET_SHA256
    with pytest.raises(SkillSelectionDatasetError, match="抓取来源证明不完整"):
        load_skill_selection_dataset()
    with pytest.raises(SkillSelectionDatasetError, match="抓取来源证明不完整"):
        run_skill_selection_benchmark_from_path()


def test_all_frozen_labels_are_reproducible_despite_snapshot_failure():
    document = json.loads(DEFAULT_DATASET_PATH.read_text(encoding="utf-8"))
    rules = tuple(
        AnnotationRule(item["skill_name"], tuple(item["markers"]))
        for item in document["annotation_policy"]["rules"]
    )
    cases = document["design_cases"] + document["evaluation_cases"]
    assert len(cases) == 30
    assert all(
        tuple(case["expected_skill_names"])
        == recompute_silver_labels(case["title"], rules)
        for case in cases
    )


def test_loader_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "duplicate.json"
    payload = b'{"schema_version":1,"schema_version":1}'
    path.write_bytes(payload)
    with pytest.raises(SkillSelectionDatasetError, match="重复 JSON 字段"):
        load_skill_selection_dataset(
            path, hashlib.sha256(payload).hexdigest()
        )


def test_loader_recomputes_and_rejects_changed_frozen_label(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["expected_skill_names"] = []
    path = tmp_path / "changed-label.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="冻结标签"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_snapshot_before_latest_update(tmp_path):
    document = _strict_document(tmp_path)
    document["snapshot_generated_at"] = "2026-01-01T00:00:00Z"
    path = tmp_path / "bad-time.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="snapshot_generated_at"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_null_etag_as_incomplete_provenance(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["fetch_provenance"]["etag"] = None
    path = tmp_path / "null-etag.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="etag 为 null"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_incomplete_source_provenance(tmp_path):
    document = _strict_document(tmp_path)
    document["source"]["provenance_complete"] = False
    path = tmp_path / "incomplete-source.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="provenance_complete 必须为 true"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_non_200_http_status(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["fetch_provenance"]["http_status"] = 304
    path = tmp_path / "non-200.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="http_status 必须为 200"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_fetch_before_response_date(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["fetch_provenance"]["fetched_at"] = (
        "2026-01-01T00:00:00Z"
    )
    path = tmp_path / "fetch-before-response.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="fetched_at 早于 response_date"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_empty_etag(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["fetch_provenance"]["etag"] = ""
    path = tmp_path / "empty-etag.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="非空字符串"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_raw_response_hash_mismatch(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["fetch_provenance"]["raw_response_sha256"] = (
        "0" * 64
    )
    path = tmp_path / "raw-hash-mismatch.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="原始响应 SHA-256 不匹配"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_canonical_hash_mismatch(tmp_path):
    document = _strict_document(tmp_path)
    document["evaluation_cases"][0]["fetch_provenance"]["canonical_sha256"] = (
        "0" * 64
    )
    path = tmp_path / "canonical-hash-mismatch.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="规范字段 SHA-256 不匹配"):
        load_skill_selection_dataset(path, sha256)


def test_loader_rejects_raw_response_field_mismatch(tmp_path):
    document = _strict_document(tmp_path)
    provenance = document["evaluation_cases"][0]["fetch_provenance"]
    raw_path = tmp_path / provenance["raw_response_path"]
    raw_document = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_document["title"] = "篡改后的标题"
    raw_payload = json.dumps(
        raw_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    raw_path.write_bytes(raw_payload)
    provenance["raw_response_sha256"] = hashlib.sha256(raw_payload).hexdigest()
    path = tmp_path / "raw-field-mismatch.json"
    sha256 = _write_json(path, document)
    with pytest.raises(SkillSelectionDatasetError, match="与原始 REST 响应字段不一致"):
        load_skill_selection_dataset(path, sha256)


def test_loader_accepts_complete_archived_provenance(tmp_path):
    document = _strict_document(tmp_path)
    path = tmp_path / "valid-v2.json"
    sha256 = _write_json(path, document)
    dataset = load_skill_selection_dataset(path, sha256)
    assert dataset.schema_version == 2
    assert dataset.provenance_complete is True
    assert dataset.evaluation_cases[0].fetch_provenance.http_status == 200


def test_metrics_include_quality_injection_prompt_and_latency():
    dataset = _synthetic_dataset()
    rows = (
        SelectionObservation(
            dataset.evaluation_cases[0],
            ("knowledge-wiki",),
            "知识提示",
            1.0,
        ),
        SelectionObservation(
            dataset.evaluation_cases[3],
            ("image-generation",),
            "image prompt",
            3.0,
        ),
    )
    metrics = calculate_selection_metrics(rows)
    assert metrics["micro_precision"] == 0.5
    assert metrics["micro_recall"] == 1.0
    assert metrics["micro_f1"] == pytest.approx(2.0 / 3.0)
    assert metrics["exact_set_accuracy"] == 0.5
    assert metrics["negative_false_injection_rate"] == 1.0
    assert metrics["average_selected_skills"] == 1.0
    assert metrics["total_prompt_characters"] == len("知识提示image prompt")
    assert metrics["total_prompt_tokens_estimate"] > 0
    assert metrics["latency_ms_p95"] == pytest.approx(2.9)


def test_product_selector_reproduces_rules_on_test_only_fixture():
    report = run_skill_selection_benchmark(_synthetic_dataset(), top_k=2)
    baseline = report["arms"]["all_skills_metadata"]["metrics"]
    selected = report["arms"]["controlled_top_k"]["metrics"]

    assert baseline["micro_precision"] == 0.5
    assert baseline["micro_recall"] == 1.0
    assert baseline["exact_set_accuracy"] == 0.25
    assert baseline["negative_false_injection_rate"] == 1.0
    assert baseline["average_selected_skills"] == 2.0

    assert selected["micro_precision"] == 1.0
    assert selected["micro_recall"] == 1.0
    assert selected["micro_f1"] == 1.0
    assert selected["exact_set_accuracy"] == 1.0
    assert selected["negative_false_injection_rate"] == 0.0
    assert selected["average_selected_skills"] == 1.0
    assert selected["average_prompt_characters"] < baseline["average_prompt_characters"]
    assert selected["average_prompt_tokens_estimate"] < baseline["average_prompt_tokens_estimate"]
    assert selected["latency_ms_p95"] >= 0.0
    assert report["limitations"]["customer_success_rate_measured"] is False
    assert report["limitations"]["label_rule_leakage"] is True
    assert report["limitations"]["production_gate_eligible"] is False
    assert report["limitations"]["governed_subset_only"] is True
    assert "银标不代表" in report["limitations"]["claim"]


def test_cli_writes_structured_blocked_report(tmp_path, monkeypatch, capsys):
    output = tmp_path / "blocked.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["skill-selection-benchmark", "--output", str(output)],
    )
    assert main() == 2
    console_report = json.loads(capsys.readouterr().out)
    file_report = json.loads(output.read_text(encoding="utf-8"))
    assert console_report == file_report
    assert file_report["status"] == "blocked_invalid_dataset"
    assert file_report["passed"] is False
    assert file_report["metrics"] is None
    assert file_report["dataset"]["provenance_complete"] is False
    assert file_report["limitations"]["production_gate_eligible"] is False


def test_metric_rejects_duplicate_selected_skills():
    case = _synthetic_dataset().evaluation_cases[0]
    row = SelectionObservation(
        case,
        ("knowledge-wiki", "knowledge-wiki"),
        "",
        0.0,
    )
    with pytest.raises(ValueError, match="排序且去重"):
        calculate_selection_metrics((row,))


def test_implementation_fingerprint_is_stable_sha256():
    first = implementation_fingerprint()
    second = implementation_fingerprint()
    assert first == second
    assert len(first) == 64
    int(first, 16)
