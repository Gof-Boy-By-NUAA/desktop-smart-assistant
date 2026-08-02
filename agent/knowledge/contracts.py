"""受治理知识库的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Sequence

from agent.memory.governance import MemoryScope, Sensitivity


class KnowledgeError(Exception):
    """知识治理操作的基础异常。"""


class KnowledgeValidationError(KnowledgeError):
    """输入没有满足知识数据契约。"""


class KnowledgeCitationIntegrityError(KnowledgeValidationError):
    """知识引用格式、内容或当前事实完整性校验失败。"""

    code = "citation_integrity_failed"

    def __init__(self, message: str):
        super().__init__("%s: %s" % (self.code, message))


class KnowledgeCitationVersionError(KnowledgeValidationError):
    """知识引用使用了当前解析器不支持的协议版本。"""

    code = "unsupported_citation_version"

    def __init__(self, message: str):
        super().__init__("%s: %s" % (self.code, message))


class KnowledgeAuthorizationError(KnowledgeError):
    """调用方没有知识操作所需的权限。"""


class KnowledgeNotFoundError(KnowledgeError):
    """知识文档不存在或对调用方不可见。"""


class KnowledgeIdempotencyConflictError(KnowledgeError):
    """同一个幂等键被用于不同知识请求。"""


class KnowledgeStatus(str, Enum):
    """知识文档版本的生命周期状态。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


@dataclass(frozen=True)
class KnowledgeWriteCommand:
    """新增或更新知识文档的业务命令。"""

    content: str
    title: str
    source_ref: str
    collection_id: str
    idempotency_key: str
    document_id: Optional[str] = None
    projection_path: Optional[str] = None
    scope: MemoryScope = MemoryScope.USER
    owner_user_id: Optional[str] = None
    session_id: Optional[str] = None
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise KnowledgeValidationError("content 必须是字符串")
        for field_name in ("title", "source_ref", "collection_id", "idempotency_key"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeValidationError("%s 不能为空" % field_name)
        if self.document_id is not None and not self.document_id.strip():
            raise KnowledgeValidationError("document_id 不能是空字符串")
        if self.projection_path is not None and not self.projection_path.strip():
            raise KnowledgeValidationError("projection_path 不能是空字符串")
        if not isinstance(self.scope, MemoryScope):
            raise KnowledgeValidationError("scope 必须是 MemoryScope")
        if not isinstance(self.sensitivity, Sensitivity):
            raise KnowledgeValidationError("sensitivity 必须是 Sensitivity")
        if not isinstance(self.metadata, dict):
            raise KnowledgeValidationError("metadata 必须是字典")


@dataclass(frozen=True)
class KnowledgeDocumentRecord:
    """不可变的知识文档版本。"""

    document_id: str
    tenant_id: str
    version: int
    status: KnowledgeStatus
    scope: MemoryScope
    owner_user_id: Optional[str]
    session_id: Optional[str]
    sensitivity: Sensitivity
    collection_id: str
    title: str
    source_ref: str
    projection_path: Optional[str]
    content: str
    content_hash: str
    metadata: Dict[str, Any]
    created_by: str
    trace_id: str
    created_at: str


@dataclass(frozen=True)
class KnowledgeSection:
    """与原始 UTF-8 正文精确绑定的知识章节。"""

    section_id: str
    tenant_id: str
    document_id: str
    document_version: int
    section_index: int
    heading: str
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    content_hash: str


@dataclass(frozen=True)
class KnowledgeEvidence:
    """可独立检索并能回放到原文的证据片段。"""

    evidence_id: str
    tenant_id: str
    document_id: str
    document_version: int
    section_id: str
    evidence_index: int
    quote: str
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    quote_hash: str


@dataclass(frozen=True)
class KnowledgeCitation:
    """返回给模型和用户的一等引用。"""

    uri: str
    citation_version: int
    document_id: str
    document_version: int
    section_id: str
    evidence_id: str
    source_ref: str
    source_ref_hash: str
    byte_start: int
    byte_end: int
    content_hash: str
    quote_hash: str
    quote: str
    scope: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class KnowledgeSearchResult:
    """包含排序分量和已核验引用的检索结果。"""

    title: str
    collection_id: str
    score: float
    bm25_score: float
    query_coverage: float
    citation: KnowledgeCitation
    warnings: Sequence[str] = field(default_factory=tuple)
