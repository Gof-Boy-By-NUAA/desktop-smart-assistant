"""严格加载内容寻址的客户验收包。"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from common.path_safety import (
    has_link_or_reparse_component,
    is_link_or_reparse_point,
)

from .attestation import clean_ed25519_public_key
from .contracts import (
    CustomerCase,
    CustomerPackage,
    CustomerPackageError,
    CustomerThresholds,
)
from .json_utils import clean_sha256, clean_text, sha256_json, strict_json_loads


_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_CASES_BYTES = 8 * 1024 * 1024
_MAX_CASE_COUNT = 10000


def load_customer_package(
    package_root: Path,
    expected_manifest_sha256: str,
) -> CustomerPackage:
    """加载固定清单及其绑定的完整客户任务集。"""

    root = _resolve_root(Path(package_root))
    manifest_path = _resolve_file(root, "manifest.json")
    manifest_payload = _read_limited(
        manifest_path, _MAX_MANIFEST_BYTES, "客户清单"
    )
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    expected_manifest_sha256 = clean_sha256(
        expected_manifest_sha256, "manifest_sha256"
    )
    if manifest_sha256 != expected_manifest_sha256:
        raise CustomerPackageError("客户清单 SHA-256 与运行请求不一致")
    manifest = strict_json_loads(manifest_payload, "客户清单")
    _require_keys(
        manifest,
        {
            "schema_version",
            "package_id",
            "tenant_id",
            "model",
            "attestation",
            "oracle",
            "cases",
            "skills",
            "thresholds",
        },
        "客户清单",
    )
    if manifest["schema_version"] != 1:
        raise CustomerPackageError("客户清单 schema_version 必须为 1")

    model = _object_with_keys(
        manifest["model"],
        {
            "id", "parameters", "endpoint_sha256",
            "prompt_sha256", "tools_sha256",
        },
        "model",
    )
    if not isinstance(model["parameters"], dict):
        raise CustomerPackageError("model.parameters 必须是对象")
    sha256_json(model["parameters"])

    oracle = _object_with_keys(
        manifest["oracle"], {"id", "kind"}, "oracle"
    )
    oracle_kind = clean_text(oracle["kind"], "oracle.kind")
    if oracle_kind not in {"deterministic", "customer_blind_review"}:
        raise CustomerPackageError("oracle.kind 不受支持")

    attestation = _object_with_keys(
        manifest["attestation"], {"executor", "judge"}, "attestation"
    )
    executor_attestation = _parse_attestation_identity(
        attestation["executor"], "attestation.executor"
    )
    judge_attestation = None
    if attestation["judge"] is not None:
        judge_attestation = _parse_attestation_identity(
            attestation["judge"], "attestation.judge"
        )
    if oracle_kind == "deterministic" and judge_attestation is not None:
        raise CustomerPackageError("确定性 Oracle 不能配置外部判定器证明")
    if oracle_kind == "customer_blind_review" and judge_attestation is None:
        raise CustomerPackageError("客户盲评必须绑定判定器公钥和制品")

    cases_manifest = _object_with_keys(
        manifest["cases"], {"path", "sha256", "count"}, "cases"
    )
    cases_path = _resolve_file(
        root, clean_text(cases_manifest["path"], "cases.path", 512)
    )
    cases_payload = _read_limited(cases_path, _MAX_CASES_BYTES, "客户场景")
    cases_sha256 = hashlib.sha256(cases_payload).hexdigest()
    if cases_sha256 != clean_sha256(cases_manifest["sha256"], "cases.sha256"):
        raise CustomerPackageError("客户场景 SHA-256 与清单不一致")
    parsed_cases = _parse_cases(cases_payload)
    if not isinstance(cases_manifest["count"], int) or isinstance(
        cases_manifest["count"], bool
    ):
        raise CustomerPackageError("cases.count 必须是整数")
    if cases_manifest["count"] != len(parsed_cases):
        raise CustomerPackageError("客户场景数量与清单不一致")

    skills = _object_with_keys(
        manifest["skills"], {"allowed", "forbidden", "candidate"}, "skills"
    )
    allowed = _text_tuple(skills["allowed"], "skills.allowed")
    forbidden = _text_tuple(skills["forbidden"], "skills.forbidden")
    if set(allowed).intersection(forbidden):
        raise CustomerPackageError("允许和禁止技能列表不能重叠")
    candidate = _object_with_keys(
        skills["candidate"],
        {"skill_id", "version", "content_sha256"},
        "skills.candidate",
    )
    candidate_skill_id = clean_text(
        candidate["skill_id"], "skills.candidate.skill_id", 128
    )
    candidate_skill_version = candidate["version"]
    if (
        not isinstance(candidate_skill_version, int)
        or isinstance(candidate_skill_version, bool)
        or candidate_skill_version <= 0
    ):
        raise CustomerPackageError("skills.candidate.version 必须大于零")
    candidate_skill_content_sha256 = clean_sha256(
        candidate["content_sha256"], "skills.candidate.content_sha256"
    )
    if (
        candidate_skill_id not in allowed
        or candidate_skill_id in forbidden
    ):
        raise CustomerPackageError("固定候选技能不满足允许边界")

    thresholds = _parse_thresholds(manifest["thresholds"])
    return CustomerPackage(
        root=root,
        package_id=clean_text(manifest["package_id"], "package_id"),
        tenant_id=clean_text(manifest["tenant_id"], "tenant_id"),
        model_id=clean_text(model["id"], "model.id"),
        model_parameters=dict(model["parameters"]),
        endpoint_sha256=clean_sha256(
            model["endpoint_sha256"], "endpoint_sha256"
        ),
        prompt_sha256=clean_sha256(model["prompt_sha256"], "prompt_sha256"),
        tools_sha256=clean_sha256(model["tools_sha256"], "tools_sha256"),
        executor_id=executor_attestation["id"],
        executor_version=executor_attestation["version"],
        executor_artifact_sha256=executor_attestation["artifact_sha256"],
        executor_ed25519_public_key=executor_attestation[
            "ed25519_public_key"
        ],
        judge_id=(judge_attestation or {}).get("id"),
        judge_version=(judge_attestation or {}).get("version"),
        judge_artifact_sha256=(judge_attestation or {}).get(
            "artifact_sha256"
        ),
        judge_ed25519_public_key=(judge_attestation or {}).get(
            "ed25519_public_key"
        ),
        oracle_id=clean_text(oracle["id"], "oracle.id"),
        oracle_kind=oracle_kind,
        allowed_skill_ids=allowed,
        forbidden_skill_ids=forbidden,
        candidate_skill_id=candidate_skill_id,
        candidate_skill_version=candidate_skill_version,
        candidate_skill_content_sha256=(
            candidate_skill_content_sha256
        ),
        thresholds=thresholds,
        cases=parsed_cases,
        manifest_sha256=manifest_sha256,
        cases_sha256=cases_sha256,
    )


def _parse_cases(payload: bytes) -> Tuple[CustomerCase, ...]:
    document = strict_json_loads(payload, "客户场景")
    _require_keys(document, {"cases"}, "客户场景")
    raw_cases = document["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CustomerPackageError("客户场景 cases 必须是非空数组")
    if len(raw_cases) > _MAX_CASE_COUNT:
        raise CustomerPackageError("客户场景数量超过上限")
    cases = []
    seen = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        _require_keys(
            raw_case, {"case_id", "input", "oracle", "critical"},
            "第 %d 个客户场景" % index,
        )
        case_id = clean_text(raw_case["case_id"], "case_id", 128)
        if case_id in seen:
            raise CustomerPackageError("客户场景 case_id 重复: %s" % case_id)
        if not isinstance(raw_case["critical"], bool):
            raise CustomerPackageError("critical 必须是布尔值")
        seen.add(case_id)
        cases.append(
            CustomerCase(
                case_id=case_id,
                case_input=raw_case["input"],
                oracle=raw_case["oracle"],
                critical=raw_case["critical"],
                case_sha256=sha256_json(raw_case),
            )
        )
    return tuple(cases)


def _parse_attestation_identity(value: Any, field_name: str) -> Dict[str, str]:
    raw = _object_with_keys(
        value,
        {"id", "version", "artifact_sha256", "ed25519_public_key"},
        field_name,
    )
    return {
        "id": clean_text(raw["id"], field_name + ".id", 128),
        "version": clean_text(
            raw["version"], field_name + ".version", 128
        ),
        "artifact_sha256": clean_sha256(
            raw["artifact_sha256"], field_name + ".artifact_sha256"
        ),
        "ed25519_public_key": clean_ed25519_public_key(
            raw["ed25519_public_key"],
            field_name + ".ed25519_public_key",
        ),
    }


def _parse_thresholds(value: Any) -> CustomerThresholds:
    raw = _object_with_keys(
        value,
        {
            "minimum_success_rate_delta",
            "maximum_regressions",
            "maximum_latency_ratio",
            "maximum_total_tokens",
        },
        "thresholds",
    )
    delta = raw["minimum_success_rate_delta"]
    latency = raw["maximum_latency_ratio"]
    regressions = raw["maximum_regressions"]
    tokens = raw["maximum_total_tokens"]
    if (
        not isinstance(delta, (int, float))
        or isinstance(delta, bool)
        or not math.isfinite(float(delta))
        or delta <= 0
        or delta > 1
    ):
        raise CustomerPackageError(
            "minimum_success_rate_delta 必须是 (0, 1] 内有限值"
        )
    if (
        not isinstance(latency, (int, float))
        or isinstance(latency, bool)
        or not math.isfinite(float(latency))
        or latency <= 0
    ):
        raise CustomerPackageError("maximum_latency_ratio 必须是正有限值")
    if not isinstance(regressions, int) or isinstance(regressions, bool) or regressions < 0:
        raise CustomerPackageError("maximum_regressions 必须是非负整数")
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0:
        raise CustomerPackageError("maximum_total_tokens 必须大于零")
    return CustomerThresholds(float(delta), regressions, float(latency), tokens)


def _resolve_root(root: Path) -> Path:
    lexical = Path(os.path.abspath(str(root)))
    if has_link_or_reparse_component(lexical):
        raise CustomerPackageError("客户包路径不能包含符号链接或重解析点")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise CustomerPackageError("客户包根目录不存在") from error
    if not resolved.is_dir():
        raise CustomerPackageError("客户包根路径必须是目录")
    return resolved


def _resolve_file(root: Path, relative_value: str) -> Path:
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise CustomerPackageError("客户包文件必须使用根目录内相对路径")
    current = root
    for part in relative.parts:
        current = current / part
        if is_link_or_reparse_point(current):
            raise CustomerPackageError("客户包路径不能包含符号链接或重解析点")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise CustomerPackageError("客户包文件不存在或越出根目录") from error
    if not resolved.is_file():
        raise CustomerPackageError("客户包路径必须是普通文件")
    return resolved


def _read_limited(path: Path, maximum: int, field_name: str) -> bytes:
    if path.stat().st_size > maximum:
        raise CustomerPackageError("%s 文件超过大小上限" % field_name)
    return path.read_bytes()


def _require_keys(value: Any, expected: set, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise CustomerPackageError(
            "%s 字段必须严格等于: %s"
            % (field_name, ", ".join(sorted(expected)))
        )
    return value


def _object_with_keys(value: Any, expected: set, field_name: str) -> Dict[str, Any]:
    return _require_keys(value, expected, field_name)


def _text_tuple(value: Any, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise CustomerPackageError("%s 必须是数组" % field_name)
    result = tuple(clean_text(item, field_name, 128) for item in value)
    if len(result) != len(set(result)):
        raise CustomerPackageError("%s 不能包含重复值" % field_name)
    return result
