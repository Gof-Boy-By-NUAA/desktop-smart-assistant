"""受治理记忆的写入、撤销和回滚工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from agent.memory.governance import MemoryScope, MemoryWriteCommand, Sensitivity
from agent.tools.base_tool import BaseTool, ToolResult


def _stable_idempotency_key(operation: str, payload: dict) -> str:
    """为同一个规范化工具请求生成稳定幂等键。"""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "tool-%s-%s" % (operation, hashlib.sha256(encoded).hexdigest())


def _record_result(record) -> dict:
    """返回足以审计操作、但不重复泄露正文的结果。"""

    return {
        "memory_id": record.memory_id,
        "version": record.version,
        "status": record.status.value,
        "scope": record.scope.value,
        "sensitivity": record.sensitivity.value,
        "content_hash": record.content_hash,
        "source_ref": record.source_ref,
    }


class MemoryWriteTool(BaseTool):
    """通过可信运行时身份新增或更新记忆。"""

    name = "memory_write"
    description = (
        "Write a durable governed memory with immutable versions, audit history, "
        "and tenant/user/session access control. Use memory_id to update an existing memory. "
        "source_ref is a source declaration, not independently verified evidence."
    )
    params = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The exact durable fact, preference, decision, or event to remember",
            },
            "scope": {
                "type": "string",
                "enum": ["user", "session", "shared"],
                "description": "Visibility scope; defaults to user",
                "default": "user",
            },
            "sensitivity": {
                "type": "string",
                "enum": ["public", "internal", "private", "restricted"],
                "description": "Sensitivity label; defaults to private",
                "default": "private",
            },
            "memory_id": {
                "type": "string",
                "description": "Existing memory ID when creating a new immutable version",
            },
            "source_ref": {
                "type": "string",
                "description": "Source location such as a message ID or document reference",
            },
            "evidence_quote": {
                "type": "string",
                "description": "Optional exact source excerpt; it remains an unverified source claim",
            },
            "title": {
                "type": "string",
                "description": "Optional short title used by retrieval",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Optional retry key; the runtime derives a stable key when omitted",
            },
        },
        "required": ["content"],
    }

    def __init__(self, memory_manager, identity, session_id: Optional[str] = None):
        super().__init__()
        self.memory_manager = memory_manager
        self.identity = identity
        self.session_id = session_id

    def execute(self, args: dict) -> ToolResult:
        content = args.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return ToolResult.fail("Error: content parameter is required")

        try:
            scope = MemoryScope(args.get("scope", "user"))
            sensitivity = Sensitivity(args.get("sensitivity", "private"))
            if scope is MemoryScope.SESSION and not self.session_id:
                return ToolResult.fail("Error: session-scoped memory requires a session identity")

            source_ref = args.get("source_ref") or "session:%s" % (
                self.session_id or "local"
            )
            metadata = {}
            if args.get("title"):
                metadata["title"] = args["title"]
            if args.get("evidence_quote"):
                metadata["evidence_quote"] = args["evidence_quote"]

            key_payload = {
                "actor_user_id": self.identity.actor_user_id,
                "content": content.strip(),
                "scope": scope.value,
                "sensitivity": sensitivity.value,
                "memory_id": args.get("memory_id"),
                "source_ref": source_ref,
                "metadata": metadata,
                "session_id": self.session_id if scope is MemoryScope.SESSION else None,
            }
            idempotency_key = args.get("idempotency_key") or _stable_idempotency_key(
                "write", key_payload
            )
            command = MemoryWriteCommand(
                content=content,
                scope=scope,
                source_type="conversation",
                source_ref=source_ref,
                idempotency_key=idempotency_key,
                memory_id=args.get("memory_id"),
                session_id=self.session_id if scope is MemoryScope.SESSION else None,
                sensitivity=sensitivity,
                metadata=metadata,
            )
            return ToolResult.success(
                _record_result(self.memory_manager.remember(self.identity, command))
            )
        except (TypeError, ValueError) as error:
            return ToolResult.fail("Error: invalid memory option: %s" % error)
        except Exception as error:
            return ToolResult.fail("Error writing governed memory: %s" % error)


class MemoryRevokeTool(BaseTool):
    """撤销当前有效版本，保留不可变历史和审计记录。"""

    name = "memory_revoke"
    description = (
        "Revoke an active governed memory so it is no longer readable or searchable. "
        "The immutable history remains available for an authorized rollback."
    )
    params = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory ID to revoke"},
            "reason": {"type": "string", "description": "Reason for revocation"},
            "idempotency_key": {
                "type": "string",
                "description": "Optional retry key; the runtime derives a stable key when omitted",
            },
        },
        "required": ["memory_id", "reason"],
    }

    def __init__(self, memory_manager, identity):
        super().__init__()
        self.memory_manager = memory_manager
        self.identity = identity

    def execute(self, args: dict) -> ToolResult:
        memory_id = args.get("memory_id", "")
        reason = args.get("reason", "")
        if not memory_id or not reason:
            return ToolResult.fail("Error: memory_id and reason are required")
        payload = {
            "actor_user_id": self.identity.actor_user_id,
            "memory_id": memory_id,
            "reason": reason.strip(),
        }
        key = args.get("idempotency_key") or _stable_idempotency_key("revoke", payload)
        try:
            record = self.memory_manager.revoke(
                self.identity,
                memory_id,
                key,
                reason,
            )
            return ToolResult.success(_record_result(record))
        except Exception as error:
            return ToolResult.fail("Error revoking governed memory: %s" % error)


class MemoryRollbackTool(BaseTool):
    """把历史内容恢复为新的有效版本。"""

    name = "memory_rollback"
    description = (
        "Restore a historical governed-memory version as a new active immutable version. "
        "Use only after the user or an authorized workflow explicitly requests restoration."
    )
    params = {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory ID to restore"},
            "target_version": {
                "type": "integer",
                "description": "Historical version whose content should be restored",
            },
            "reason": {"type": "string", "description": "Reason for restoration"},
            "idempotency_key": {
                "type": "string",
                "description": "Optional retry key; the runtime derives a stable key when omitted",
            },
        },
        "required": ["memory_id", "target_version", "reason"],
    }

    def __init__(self, memory_manager, identity):
        super().__init__()
        self.memory_manager = memory_manager
        self.identity = identity

    def execute(self, args: dict) -> ToolResult:
        memory_id = args.get("memory_id", "")
        target_version = args.get("target_version")
        reason = args.get("reason", "")
        if (
            not memory_id
            or not isinstance(target_version, int)
            or isinstance(target_version, bool)
            or not reason
        ):
            return ToolResult.fail(
                "Error: memory_id, integer target_version, and reason are required"
            )
        payload = {
            "actor_user_id": self.identity.actor_user_id,
            "memory_id": memory_id,
            "target_version": target_version,
            "reason": reason.strip(),
        }
        key = args.get("idempotency_key") or _stable_idempotency_key("rollback", payload)
        try:
            record = self.memory_manager.rollback(
                self.identity,
                memory_id,
                target_version,
                key,
                reason,
            )
            return ToolResult.success(_record_result(record))
        except Exception as error:
            return ToolResult.fail("Error rolling back governed memory: %s" % error)
