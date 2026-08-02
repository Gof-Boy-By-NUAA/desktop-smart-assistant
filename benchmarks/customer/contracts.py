"""客户验收包、执行请求和结果的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple


class CustomerAcceptanceError(Exception):
    """客户验收框架的基础异常。"""


class CustomerPackageError(CustomerAcceptanceError):
    """客户包结构、路径或哈希无效。"""


class CustomerExecutionError(CustomerAcceptanceError):
    """可信执行适配器没有满足协议。"""


@dataclass(frozen=True)
class CustomerThresholds:
    """由客户包固定、运行时不能放宽的验收阈值。"""

    minimum_success_rate_delta: float
    maximum_regressions: int
    maximum_latency_ratio: float
    maximum_total_tokens: int


@dataclass(frozen=True)
class CustomerCase:
    """客户任务及其可信判定配置。"""

    case_id: str
    case_input: Any
    oracle: Any
    critical: bool
    case_sha256: str


@dataclass(frozen=True)
class CustomerPackage:
    """经过严格解析和内容寻址的客户验收包。"""

    root: Path
    package_id: str
    tenant_id: str
    model_id: str
    model_parameters: Dict[str, Any]
    endpoint_sha256: str
    prompt_sha256: str
    tools_sha256: str
    executor_id: str
    executor_version: str
    executor_artifact_sha256: str
    executor_ed25519_public_key: str
    judge_id: str | None
    judge_version: str | None
    judge_artifact_sha256: str | None
    judge_ed25519_public_key: str | None
    oracle_id: str
    oracle_kind: str
    allowed_skill_ids: Tuple[str, ...]
    forbidden_skill_ids: Tuple[str, ...]
    candidate_skill_id: str
    candidate_skill_version: int
    candidate_skill_content_sha256: str
    thresholds: CustomerThresholds
    cases: Tuple[CustomerCase, ...]
    manifest_sha256: str
    cases_sha256: str


@dataclass(frozen=True)
class CustomerExecutionRequest:
    """发送给可信执行适配器的单臂请求。"""

    run_id: str
    case_id: str
    arm: str
    tenant_id: str
    model_id: str
    model_parameters: Dict[str, Any]
    endpoint_sha256: str
    prompt_sha256: str
    tools_sha256: str
    case_input: Any
    skill: Dict[str, Any] | None


@dataclass(frozen=True)
class CustomerExecutionResult:
    """可信执行适配器返回的输出与成本。"""

    output: Any
    latency_ms: float
    input_tokens: int
    output_tokens: int
    execution_snapshot_sha256: str
    request_sha256: str
    executor_artifact_sha256: str
    attestation_signature: str


@dataclass(frozen=True)
class CustomerJudgmentRequest:
    """发送给独立判定器的盲化单臂输出。"""

    run_id: str
    case_id: str
    arm_label: str
    oracle_id: str
    oracle: Any
    output: Any


@dataclass(frozen=True)
class CustomerJudgment:
    """独立 Oracle 返回的成功判定和可哈希证据。"""

    success: bool
    evidence: Any
    judge_artifact_sha256: str | None = None
    attestation_signature: str | None = None
