"""通过可信运行时身份调用知识事实库。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from agent.knowledge import KnowledgeWriteCommand
from agent.memory.governance import MemoryScope, Sensitivity
from agent.tools.base_tool import BaseTool, ToolResult


_CITATION_ARGUMENT_FIELDS = (
    ("citation_version", "citation_version"),
    ("document_id", "document_id"),
    ("version", "document_version"),
    ("document_version", "document_version"),
    ("section_id", "section_id"),
    ("evidence_id", "evidence_id"),
    ("source_ref", "source_ref"),
    ("source_ref_hash", "source_ref_hash"),
    ("byte_start", "byte_start"),
    ("byte_end", "byte_end"),
    ("content_hash", "content_hash"),
    ("quote_hash", "quote_hash"),
    ("quote", "quote"),
)
_CITATION_ONLY_ARGUMENTS = tuple(
    argument_name
    for argument_name, _ in _CITATION_ARGUMENT_FIELDS
    if argument_name not in {"document_id", "version", "document_version"}
)


def _stable_key(operation: str, payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "tool-%s-%s" % (operation, hashlib.sha256(encoded).hexdigest())


class KnowledgeSearchTool(BaseTool):
    """检索当前身份可见的知识证据并返回一等引用。"""

    name = "knowledge_search"
    description = (
        "Search the governed knowledge base. Every result includes a verified "
        "knowledge:// citation, exact source quote, immutable document version, "
        "UTF-8 byte range, and content hashes."
    )
    params = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Knowledge query"},
            "limit": {
                "type": "integer",
                "description": "Maximum results from 1 to 20",
                "default": 5,
            },
            "collection_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional collection filters",
            },
        },
        "required": ["query"],
    }

    def __init__(self, runtime, identity, session_id: Optional[str] = None):
        super().__init__()
        self.runtime = runtime
        self.identity = identity
        self.session_id = session_id

    def execute(self, args: dict) -> ToolResult:
        try:
            results = self.runtime.search(
                self.identity,
                args.get("query", ""),
                limit=args.get("limit", 5),
                collection_ids=args.get("collection_ids"),
                session_id=self.session_id,
            )
            return ToolResult.success(
                {
                    "result_count": len(results),
                    "results": [
                        {
                            "title": item.title,
                            "collection_id": item.collection_id,
                            "score": item.score,
                            "bm25_score": item.bm25_score,
                            "query_coverage": item.query_coverage,
                            "citation": {
                                "uri": item.citation.uri,
                                "citation_version": item.citation.citation_version,
                                "document_id": item.citation.document_id,
                                "document_version": item.citation.document_version,
                                "section_id": item.citation.section_id,
                                "evidence_id": item.citation.evidence_id,
                                "source_ref": item.citation.source_ref,
                                "source_ref_hash": item.citation.source_ref_hash,
                                "byte_start": item.citation.byte_start,
                                "byte_end": item.citation.byte_end,
                                "content_hash": item.citation.content_hash,
                                "quote_hash": item.citation.quote_hash,
                                "quote": item.citation.quote,
                            },
                        }
                        for item in results
                    ],
                }
            )
        except Exception as error:
            return ToolResult.fail("Error searching governed knowledge: %s" % error)


class KnowledgeGetTool(BaseTool):
    """按不可变标识读取知识正文或历史版本。"""

    name = "knowledge_get"
    description = (
        "Read current governed knowledge by document_id or a verified active "
        "knowledge:// citation. "
        "Historical versions require an authorized identity."
    )
    params = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "Knowledge document ID"},
            "uri": {
                "type": "string",
                "description": "Verified active v3 knowledge:// citation URI",
            },
            "citation_version": {
                "type": "integer",
                "enum": [3],
                "description": "Citation protocol version; requires uri",
            },
            "version": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional immutable version",
            },
            "document_version": {
                "type": "integer",
                "minimum": 1,
                "description": "Citation document version; version is a compatibility alias",
            },
            "section_id": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "Citation section ID; requires uri",
            },
            "evidence_id": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "Citation evidence ID; requires uri",
            },
            "source_ref": {
                "type": "string",
                "description": "Citation source reference; requires uri",
            },
            "source_ref_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "Citation source reference hash; requires uri",
            },
            "byte_start": {
                "type": "integer",
                "minimum": 0,
                "description": "Citation UTF-8 byte start; requires uri",
            },
            "byte_end": {
                "type": "integer",
                "minimum": 1,
                "description": "Citation UTF-8 byte end; requires uri",
            },
            "content_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "Citation document hash; requires uri",
            },
            "quote_hash": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": "Citation quote hash; requires uri",
            },
            "quote": {
                "type": "string",
                "description": "Citation exact quote; requires uri",
            },
            "start_line": {"type": "integer", "default": 1},
            "num_lines": {"type": "integer", "description": "Optional line count"},
        },
        "required": [],
    }

    def __init__(self, runtime, identity, session_id: Optional[str] = None):
        super().__init__()
        self.runtime = runtime
        self.identity = identity
        self.session_id = session_id

    def execute(self, args: dict) -> ToolResult:
        document_id = args.get("document_id")
        version = args.get("version")
        document_version = args.get("document_version")
        uri = args.get("uri")
        for version_field in ("version", "document_version"):
            if version_field not in args or args[version_field] is None:
                continue
            version_value = args[version_field]
            if (
                not isinstance(version_value, int)
                or isinstance(version_value, bool)
                or version_value <= 0
            ):
                return ToolResult.fail(
                    "Error: %s must be a positive integer" % version_field
                )
        if (
            version is not None
            and document_version is not None
            and version != document_version
        ):
            return ToolResult.fail(
                "Error: version conflicts with explicit document_version"
            )
        if version is None:
            version = document_version
        if not document_id:
            if uri is None:
                return ToolResult.fail("Error: document_id or uri is required")
        try:
            if uri is not None:
                citation = self.runtime.resolve_verified_citation(
                    self.identity, uri, session_id=self.session_id
                )
                for argument_name, citation_field in _CITATION_ARGUMENT_FIELDS:
                    expected_value = getattr(citation, citation_field)
                    if (
                        argument_name in args
                        and (
                            type(args[argument_name]) is not type(expected_value)
                            or args[argument_name] != expected_value
                        )
                    ):
                        return ToolResult.fail(
                            "Error: uri conflicts with explicit %s" % argument_name
                        )
                record = self.runtime.get(
                    self.identity,
                    citation.document_id,
                    session_id=self.session_id,
                )
                if (
                    record.version != citation.document_version
                    or record.content_hash != citation.content_hash
                    or record.source_ref != citation.source_ref
                    or hashlib.sha256(
                        record.source_ref.encode("utf-8")
                    ).hexdigest()
                    != citation.source_ref_hash
                ):
                    return ToolResult.fail(
                        "Error: knowledge:// 引用在正文读取期间已失效"
                    )
            else:
                for argument_name in _CITATION_ONLY_ARGUMENTS:
                    if argument_name in args:
                        return ToolResult.fail(
                            "Error: %s requires uri" % argument_name
                        )
                record = self.runtime.get(
                    self.identity,
                    document_id,
                    version=version,
                    session_id=self.session_id,
                )
            actual_content_hash = hashlib.sha256(
                record.content.encode("utf-8")
            ).hexdigest()
            if actual_content_hash != record.content_hash:
                return ToolResult.fail(
                    "Error: knowledge document 完整性校验失败或已失效"
                )
            lines = record.content.split("\n")
            start_line = args.get("start_line", 1)
            if not isinstance(start_line, int) or isinstance(start_line, bool):
                return ToolResult.fail("Error: start_line must be an integer")
            start_line = max(1, start_line)
            num_lines = args.get("num_lines")
            selected = lines[start_line - 1:]
            if num_lines is not None:
                if not isinstance(num_lines, int) or isinstance(num_lines, bool) or num_lines <= 0:
                    return ToolResult.fail("Error: num_lines must be a positive integer")
                selected = selected[:num_lines]
            return ToolResult.success(
                {
                    "document_id": record.document_id,
                    "version": record.version,
                    "status": record.status.value,
                    "title": record.title,
                    "source_ref": record.source_ref,
                    "collection_id": record.collection_id,
                    "content_hash": record.content_hash,
                    "start_line": start_line,
                    "shown_lines": len(selected),
                    "total_lines": len(lines),
                    "content": "\n".join(selected),
                }
            )
        except Exception as error:
            return ToolResult.fail("Error reading governed knowledge: %s" % error)


class KnowledgeWriteTool(BaseTool):
    """新增或更新知识文档，并生成只读兼容投影。"""

    name = "knowledge_write"
    description = (
        "Write a governed knowledge document with immutable versions, exact citations, "
        "audit history, and tenant/user/session access control."
    )
    params = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Projection path under knowledge/, for example concepts/rag.md",
            },
            "content": {"type": "string", "description": "Exact Markdown source"},
            "title": {"type": "string", "description": "Document title"},
            "collection_id": {"type": "string", "description": "Knowledge collection"},
            "document_id": {"type": "string", "description": "Existing document ID to update"},
            "scope": {
                "type": "string",
                "enum": ["user", "session", "shared"],
                "default": "user",
            },
            "sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "private", "restricted"],
                "default": "private",
            },
            "idempotency_key": {"type": "string", "description": "Optional retry key"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, runtime, identity, session_id: Optional[str] = None):
        super().__init__()
        self.runtime = runtime
        self.identity = identity
        self.session_id = session_id

    def execute(self, args: dict) -> ToolResult:
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
            return ToolResult.fail("Error: path and string content are required")
        normalized_path = path.replace("\\", "/").strip("/")
        try:
            scope = MemoryScope(args.get("scope", "user"))
            sensitivity = Sensitivity(args.get("sensitivity", "private"))
            if scope is MemoryScope.SESSION and not self.session_id:
                return ToolResult.fail("Error: session-scoped knowledge requires a session identity")
            collection_id = args.get("collection_id") or (
                normalized_path.split("/", 1)[0] if "/" in normalized_path else "root"
            )
            title = args.get("title") or Path(normalized_path).stem
            payload = {
                "actor_user_id": self.identity.actor_user_id,
                "path": normalized_path,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "title": title,
                "collection_id": collection_id,
                "document_id": args.get("document_id"),
                "scope": scope.value,
                "sensitivity": sensitivity.value,
                "session_id": self.session_id if scope is MemoryScope.SESSION else None,
            }
            record = self.runtime.write(
                self.identity,
                KnowledgeWriteCommand(
                    content=content,
                    title=title,
                    source_ref="knowledge/%s" % normalized_path,
                    collection_id=collection_id,
                    idempotency_key=args.get("idempotency_key") or _stable_key("write", payload),
                    document_id=args.get("document_id"),
                    projection_path=normalized_path,
                    scope=scope,
                    session_id=self.session_id if scope is MemoryScope.SESSION else None,
                    sensitivity=sensitivity,
                    metadata={"ingress": "knowledge-write-tool"},
                ),
            )
            return ToolResult.success(_record_summary(record))
        except Exception as error:
            return ToolResult.fail("Error writing governed knowledge: %s" % error)


class KnowledgeRevokeTool(BaseTool):
    """撤销知识文档并清除全部派生候选。"""

    name = "knowledge_revoke"
    description = "Revoke an active governed knowledge document while preserving immutable history."
    params = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "reason": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["document_id", "reason"],
    }

    def __init__(self, runtime, identity):
        super().__init__()
        self.runtime = runtime
        self.identity = identity

    def execute(self, args: dict) -> ToolResult:
        document_id = args.get("document_id")
        reason = args.get("reason")
        if not document_id or not reason:
            return ToolResult.fail("Error: document_id and reason are required")
        payload = {
            "actor_user_id": self.identity.actor_user_id,
            "document_id": document_id,
            "reason": reason.strip(),
        }
        try:
            return ToolResult.success(
                _record_summary(
                    self.runtime.revoke(
                        self.identity,
                        document_id,
                        args.get("idempotency_key") or _stable_key("revoke", payload),
                        reason,
                    )
                )
            )
        except Exception as error:
            return ToolResult.fail("Error revoking governed knowledge: %s" % error)


class KnowledgeRollbackTool(BaseTool):
    """把历史知识正文恢复成新的有效版本。"""

    name = "knowledge_rollback"
    description = "Restore a historical knowledge version as a new active immutable version."
    params = {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "target_version": {"type": "integer"},
            "reason": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
        "required": ["document_id", "target_version", "reason"],
    }

    def __init__(self, runtime, identity):
        super().__init__()
        self.runtime = runtime
        self.identity = identity

    def execute(self, args: dict) -> ToolResult:
        document_id = args.get("document_id")
        target_version = args.get("target_version")
        reason = args.get("reason")
        if not document_id or not isinstance(target_version, int) or isinstance(target_version, bool) or not reason:
            return ToolResult.fail("Error: document_id, integer target_version, and reason are required")
        payload = {
            "actor_user_id": self.identity.actor_user_id,
            "document_id": document_id,
            "target_version": target_version,
            "reason": reason.strip(),
        }
        try:
            return ToolResult.success(
                _record_summary(
                    self.runtime.rollback(
                        self.identity,
                        document_id,
                        target_version,
                        args.get("idempotency_key") or _stable_key("rollback", payload),
                        reason,
                    )
                )
            )
        except Exception as error:
            return ToolResult.fail("Error rolling back governed knowledge: %s" % error)


def _record_summary(record) -> dict:
    return {
        "document_id": record.document_id,
        "version": record.version,
        "status": record.status.value,
        "title": record.title,
        "source_ref": record.source_ref,
        "collection_id": record.collection_id,
        "scope": record.scope.value,
        "sensitivity": record.sensitivity.value,
        "content_hash": record.content_hash,
    }
