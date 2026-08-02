"""受治理记忆核心的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Optional


class MemoryGovernanceError(Exception):
    """记忆治理操作的基础异常。"""


class ValidationError(MemoryGovernanceError):
    """输入没有满足数据契约。"""


class AuthorizationError(MemoryGovernanceError):
    """调用方没有执行操作所需的权限。"""


class MemoryNotFoundError(MemoryGovernanceError):
    """目标记忆不存在或对调用方不可见。"""


class IdempotencyConflictError(MemoryGovernanceError):
    """同一个幂等键被用于不同请求。"""


class MemoryScope(str, Enum):
    """记忆的可见范围。"""

    SHARED = "shared"
    USER = "user"
    SESSION = "session"


class Sensitivity(str, Enum):
    """记忆内容的敏感级别。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    RESTRICTED = "restricted"


class MemoryStatus(str, Enum):
    """记忆版本的生命周期状态。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


@dataclass(frozen=True)
class IdentityContext:
    """由可信认证边界构造的调用身份，不允许从业务请求体推导。"""

    tenant_id: str
    actor_user_id: str
    roles: FrozenSet[str]
    trace_id: str
    auth_source: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "actor_user_id", "trace_id", "auth_source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("%s 不能为空" % field_name)
        if not isinstance(self.roles, frozenset):
            raise ValidationError("roles 必须是 frozenset")
        if any(not isinstance(role, str) or not role.strip() for role in self.roles):
            raise ValidationError("roles 不能包含空角色")

    def has_any_role(self, *roles: str) -> bool:
        """判断身份是否具备任一指定角色。"""

        return bool(self.roles.intersection(roles))


@dataclass(frozen=True)
class MemoryWriteCommand:
    """新增或更新记忆的业务命令。"""

    content: str
    scope: MemoryScope
    source_type: str
    source_ref: str
    idempotency_key: str
    memory_id: Optional[str] = None
    owner_user_id: Optional[str] = None
    session_id: Optional[str] = None
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("content", "source_type", "source_ref", "idempotency_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("%s 不能为空" % field_name)
        if self.memory_id is not None and not self.memory_id.strip():
            raise ValidationError("memory_id 不能是空字符串")
        if not isinstance(self.scope, MemoryScope):
            raise ValidationError("scope 必须是 MemoryScope")
        if not isinstance(self.sensitivity, Sensitivity):
            raise ValidationError("sensitivity 必须是 Sensitivity")
        if not isinstance(self.metadata, dict):
            raise ValidationError("metadata 必须是字典")


@dataclass(frozen=True)
class MemoryRecord:
    """不可变的记忆版本记录。"""

    memory_id: str
    tenant_id: str
    version: int
    status: MemoryStatus
    scope: MemoryScope
    owner_user_id: Optional[str]
    session_id: Optional[str]
    content: str
    content_hash: str
    source_type: str
    source_ref: str
    sensitivity: Sensitivity
    metadata: Dict[str, Any]
    created_by: str
    trace_id: str
    created_at: str


@dataclass(frozen=True)
class AuditEvent:
    """记忆变更的审计事件。"""

    event_id: str
    tenant_id: str
    trace_id: str
    actor_user_id: str
    action: str
    memory_id: str
    version: int
    details: Dict[str, Any]
    created_at: str
