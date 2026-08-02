"""外部工作手册技能治理的数据契约。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple

from agent.memory.governance.contracts import IdentityContext


class SkillGovernanceError(Exception):
    """技能治理操作的基础异常。"""


class SkillValidationError(SkillGovernanceError):
    """输入或持久化数据没有满足治理契约。"""


class SkillAuthorizationError(SkillGovernanceError):
    """可信身份不具备操作权限。"""


class SkillNotFoundError(SkillGovernanceError):
    """目标技能或版本不存在。"""


class SkillPublishGateError(SkillGovernanceError):
    """候选技能没有通过发布门禁。"""


class SkillTamperError(SkillGovernanceError):
    """来源、评测套件、数据库记录或投影发生篡改。"""


class IdempotencyConflictError(SkillGovernanceError):
    """同一个幂等键被用于不同请求。"""


class SkillStatus(str, Enum):
    """技能版本的生命周期状态。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


SkillIdentity = IdentityContext
SKILL_READ_ROLES: FrozenSet[str] = frozenset(
    {"admin", "skill:read", "skill:propose", "skill:validate", "skill:publish"}
)


def can_read_governed_skills(identity: SkillIdentity) -> bool:
    """判断可信身份是否具备治理技能读取权限。"""

    return isinstance(identity, IdentityContext) and bool(
        identity.roles.intersection(SKILL_READ_ROLES)
    )


@dataclass(frozen=True)
class SourceEvidence:
    """提案来源及其调用时可核验的原始字节。"""

    source_type: str
    source_ref: str
    payload: bytes
    sha256: str


@dataclass(frozen=True)
class SkillProposal:
    """结构化候选技能提案。"""

    name: str
    description: str
    applicability: Tuple[str, ...]
    steps: Tuple[str, ...]
    validation_rules: Tuple[str, ...]
    contraindications: Tuple[str, ...]
    model_compatibility: Tuple[str, ...]
    sources: Tuple[SourceEvidence, ...]
    idempotency_key: str


@dataclass(frozen=True)
class SkillVersion:
    """不可变技能正文与由状态事件推导出的当前状态。"""

    skill_id: str
    tenant_id: str
    name: str
    version: int
    status: SkillStatus
    owner_user_id: str
    description: str
    applicability: Tuple[str, ...]
    steps: Tuple[str, ...]
    validation_rules: Tuple[str, ...]
    contraindications: Tuple[str, ...]
    model_compatibility: Tuple[str, ...]
    provenance: Tuple[Dict[str, str], ...]
    content_hash: str
    created_by: str
    trace_id: str
    created_at: str
    rollback_of_version: Optional[int] = None


@dataclass(frozen=True)
class PairedSampleResult:
    """同一模型在同一样本上的基线与候选结果。"""

    sample_id: str
    baseline_success: bool
    candidate_success: bool
    baseline_latency_ms: float
    candidate_latency_ms: float


@dataclass(frozen=True)
class SkillEvaluationCommand:
    """只指定真实套件与目标模型、不接受调用方自报成绩的评测命令。"""

    skill_id: str
    version: int
    suite_path: str
    suite_sha256: str
    model_id: str
    idempotency_key: str


@dataclass(frozen=True)
class EvaluationRunResult:
    """可信运行器实际执行同模型配对套件后返回的结果。"""

    suite_sha256: str
    baseline_model_id: str
    candidate_model_id: str
    samples: Tuple[PairedSampleResult, ...]


class EvaluationRunner(ABC):
    """由应用可信配置注入、不可由提案请求替换的评测执行边界。"""

    @property
    @abstractmethod
    def runner_id(self) -> str:
        """返回稳定的运行器标识。"""

        raise NotImplementedError

    @property
    @abstractmethod
    def runner_version(self) -> str:
        """返回可审计的运行器实现版本。"""

        raise NotImplementedError

    @abstractmethod
    def run(
        self,
        *,
        suite_path: str,
        suite_sha256: str,
        model_id: str,
        candidate: SkillVersion,
    ) -> EvaluationRunResult:
        """实际执行基线与候选，并返回逐样本配对结果。"""

        raise NotImplementedError


@dataclass(frozen=True)
class EvaluationPolicy:
    """由系统固定、候选提案不可自行放宽的发布门禁。"""

    minimum_sample_count: int = 20
    max_candidate_p95_latency_ms: float = 2000.0
    max_latency_regression_ratio: float = 1.10

    def __post_init__(self) -> None:
        if self.minimum_sample_count <= 0:
            raise SkillValidationError("minimum_sample_count 必须大于零")
        if self.max_candidate_p95_latency_ms <= 0:
            raise SkillValidationError("max_candidate_p95_latency_ms 必须大于零")
        if self.max_latency_regression_ratio <= 0:
            raise SkillValidationError("max_latency_regression_ratio 必须大于零")


@dataclass(frozen=True)
class SkillEvaluation:
    """不可修改、不可删除的技能评测证据。"""

    evaluation_id: str
    tenant_id: str
    skill_id: str
    version: int
    validator_user_id: str
    runner_id: str
    runner_version: str
    suite_path: str
    suite_sha256: str
    sample_count: int
    baseline_model_id: str
    candidate_model_id: str
    baseline_passed: int
    candidate_passed: int
    regression_count: int
    baseline_p95_latency_ms: float
    candidate_p95_latency_ms: float
    gate_passed: bool
    gate_failures: Tuple[str, ...]
    samples: Tuple[PairedSampleResult, ...]
    policy: EvaluationPolicy
    record_hash: str
    trace_id: str
    created_at: str
    gate_schema_version: int = 1


@dataclass(frozen=True)
class SkillAuditEvent:
    """带哈希链的仅追加审计事件。"""

    event_id: str
    tenant_id: str
    skill_id: str
    version: int
    actor_user_id: str
    action: str
    details: Dict[str, object]
    previous_hash: str
    event_hash: str
    trace_id: str
    created_at: str
