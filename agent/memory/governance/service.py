"""受治理记忆核心的业务服务。"""

from __future__ import annotations

import json
import uuid
from typing import Dict, List, Optional, Tuple

from .contracts import (
    AuditEvent,
    AuthorizationError,
    IdentityContext,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryWriteCommand,
    Sensitivity,
    ValidationError,
)
from .repository import GovernedMemoryRepository


_RESERVED_METADATA_KEYS = frozenset(
    {
        "tenant_id",
        "actor_user_id",
        "roles",
        "trace_id",
        "auth_source",
        "scope",
        "owner_user_id",
        "session_id",
        "status",
    }
)


class GovernedMemoryService:
    """在可信身份上下文中执行记忆生命周期操作。"""

    def __init__(self, repository: GovernedMemoryRepository, max_content_chars: int = 131072):
        if max_content_chars <= 0:
            raise ValidationError("max_content_chars 必须大于零")
        self.repository = repository
        self.max_content_chars = max_content_chars

    def write(self, identity: IdentityContext, command: MemoryWriteCommand) -> MemoryRecord:
        """新增或更新记忆，并在同一事务中保存幂等结果和审计事件。"""

        owner_user_id, session_id = self._validate_write(identity, command)
        request_payload = self._write_request_payload(command, owner_user_id, session_id)
        request_hash = self.repository.request_hash(request_payload)

        with self.repository.transaction() as conn:
            existing_result = self.repository.find_idempotent_result(
                conn,
                identity.tenant_id,
                identity.actor_user_id,
                "write",
                command.idempotency_key,
                request_hash,
            )
            if existing_result is not None:
                latest = self.repository.get_latest(
                    conn, identity.tenant_id, existing_result.memory_id
                )
                self.repository.enqueue_derivative_job(
                    conn,
                    identity.tenant_id,
                    existing_result.memory_id,
                    latest.version,
                )
                return existing_result

            if command.memory_id is None:
                memory_id = str(uuid.uuid4())
                version = 1
                action = "memory.created"
            else:
                previous = self.repository.get_active(
                    conn, identity.tenant_id, command.memory_id
                )
                self._assert_can_manage(identity, previous)
                memory_id = previous.memory_id
                version = self.repository.next_version(
                    conn, identity.tenant_id, memory_id
                )
                self.repository.supersede_active(conn, identity.tenant_id, memory_id)
                action = "memory.updated"

            record = self.repository.make_record(
                memory_id=memory_id,
                tenant_id=identity.tenant_id,
                version=version,
                status=MemoryStatus.ACTIVE,
                scope=command.scope,
                owner_user_id=owner_user_id,
                session_id=session_id,
                content=command.content.strip(),
                source_type=command.source_type.strip(),
                source_ref=command.source_ref.strip(),
                sensitivity=command.sensitivity,
                metadata=command.metadata,
                created_by=identity.actor_user_id,
                trace_id=identity.trace_id,
            )
            self.repository.insert_record(conn, record)
            self.repository.append_audit(
                conn,
                identity.tenant_id,
                identity.trace_id,
                identity.actor_user_id,
                action,
                memory_id,
                version,
                {
                    "auth_source": identity.auth_source,
                    "scope": record.scope.value,
                    "source_ref": record.source_ref,
                    "content_hash": record.content_hash,
                },
            )
            self.repository.save_idempotent_result(
                conn,
                identity.tenant_id,
                identity.actor_user_id,
                "write",
                command.idempotency_key,
                request_hash,
                record,
            )
            self.repository.enqueue_derivative_job(
                conn,
                identity.tenant_id,
                memory_id,
                version,
            )
            return record

    def get(
        self,
        identity: IdentityContext,
        memory_id: str,
        session_id: Optional[str] = None,
    ) -> MemoryRecord:
        """按租户、所有者、会话和敏感级别执行受控读取。"""

        record = self.repository.read_active(identity.tenant_id, memory_id)
        self._assert_can_read(identity, record, session_id)
        return record

    def revoke(
        self,
        identity: IdentityContext,
        memory_id: str,
        idempotency_key: str,
        reason: str,
    ) -> MemoryRecord:
        """撤销当前有效版本，并保留完整历史。"""

        self._validate_lifecycle_input(memory_id, idempotency_key, reason)
        request_hash = self.repository.request_hash(
            {"memory_id": memory_id, "reason": reason.strip()}
        )
        with self.repository.transaction() as conn:
            existing_result = self.repository.find_idempotent_result(
                conn,
                identity.tenant_id,
                identity.actor_user_id,
                "revoke",
                idempotency_key,
                request_hash,
            )
            if existing_result is not None:
                latest = self.repository.get_latest(
                    conn, identity.tenant_id, existing_result.memory_id
                )
                self.repository.enqueue_derivative_job(
                    conn,
                    identity.tenant_id,
                    existing_result.memory_id,
                    latest.version,
                )
                return existing_result

            previous = self.repository.get_active(conn, identity.tenant_id, memory_id)
            self._assert_can_manage(identity, previous)
            version = self.repository.next_version(conn, identity.tenant_id, memory_id)
            self.repository.supersede_active(conn, identity.tenant_id, memory_id)
            record = self.repository.make_record(
                memory_id=memory_id,
                tenant_id=identity.tenant_id,
                version=version,
                status=MemoryStatus.REVOKED,
                scope=previous.scope,
                owner_user_id=previous.owner_user_id,
                session_id=previous.session_id,
                content=previous.content,
                source_type=previous.source_type,
                source_ref=previous.source_ref,
                sensitivity=previous.sensitivity,
                metadata=previous.metadata,
                created_by=identity.actor_user_id,
                trace_id=identity.trace_id,
            )
            self.repository.insert_record(conn, record)
            self.repository.append_audit(
                conn,
                identity.tenant_id,
                identity.trace_id,
                identity.actor_user_id,
                "memory.revoked",
                memory_id,
                version,
                {"auth_source": identity.auth_source, "reason": reason.strip()},
            )
            self.repository.save_idempotent_result(
                conn,
                identity.tenant_id,
                identity.actor_user_id,
                "revoke",
                idempotency_key,
                request_hash,
                record,
            )
            self.repository.enqueue_derivative_job(
                conn,
                identity.tenant_id,
                memory_id,
                version,
            )
            return record

    def rollback(
        self,
        identity: IdentityContext,
        memory_id: str,
        target_version: int,
        idempotency_key: str,
        reason: str,
    ) -> MemoryRecord:
        """把历史内容恢复为新的有效版本，不篡改既有历史。"""

        self._validate_lifecycle_input(memory_id, idempotency_key, reason)
        if target_version <= 0:
            raise ValidationError("target_version 必须大于零")
        request_hash = self.repository.request_hash(
            {
                "memory_id": memory_id,
                "target_version": target_version,
                "reason": reason.strip(),
            }
        )

        with self.repository.transaction() as conn:
            existing_result = self.repository.find_idempotent_result(
                conn,
                identity.tenant_id,
                identity.actor_user_id,
                "rollback",
                idempotency_key,
                request_hash,
            )
            if existing_result is not None:
                latest = self.repository.get_latest(
                    conn, identity.tenant_id, existing_result.memory_id
                )
                self.repository.enqueue_derivative_job(
                    conn,
                    identity.tenant_id,
                    existing_result.memory_id,
                    latest.version,
                )
                return existing_result

            latest = self.repository.get_latest(conn, identity.tenant_id, memory_id)
            self._assert_can_manage(identity, latest)
            target = self.repository.get_version(
                conn, identity.tenant_id, memory_id, target_version
            )
            version = self.repository.next_version(conn, identity.tenant_id, memory_id)
            self.repository.supersede_active(conn, identity.tenant_id, memory_id)
            record = self.repository.make_record(
                memory_id=memory_id,
                tenant_id=identity.tenant_id,
                version=version,
                status=MemoryStatus.ACTIVE,
                scope=target.scope,
                owner_user_id=target.owner_user_id,
                session_id=target.session_id,
                content=target.content,
                source_type=target.source_type,
                source_ref=target.source_ref,
                sensitivity=target.sensitivity,
                metadata=target.metadata,
                created_by=identity.actor_user_id,
                trace_id=identity.trace_id,
            )
            self.repository.insert_record(conn, record)
            self.repository.append_audit(
                conn,
                identity.tenant_id,
                identity.trace_id,
                identity.actor_user_id,
                "memory.rolled_back",
                memory_id,
                version,
                {
                    "auth_source": identity.auth_source,
                    "reason": reason.strip(),
                    "target_version": target_version,
                },
            )
            self.repository.save_idempotent_result(
                conn,
                identity.tenant_id,
                identity.actor_user_id,
                "rollback",
                idempotency_key,
                request_hash,
                record,
            )
            self.repository.enqueue_derivative_job(
                conn,
                identity.tenant_id,
                memory_id,
                version,
            )
            return record

    def list_versions(
        self,
        identity: IdentityContext,
        memory_id: str,
        session_id: Optional[str] = None,
    ) -> List[MemoryRecord]:
        """在权限检查后读取版本链。"""

        latest = self.repository.read_latest(identity.tenant_id, memory_id)
        self._assert_can_manage(identity, latest)
        self._assert_can_read(identity, latest, session_id)
        return self.repository.list_versions(identity.tenant_id, memory_id)

    def list_audit(self, identity: IdentityContext, memory_id: str) -> List[AuditEvent]:
        """在权限检查后读取审计轨迹。"""

        latest = self.repository.read_latest(identity.tenant_id, memory_id)
        self._assert_can_manage(identity, latest)
        return self.repository.list_audit(identity.tenant_id, memory_id)

    def _validate_write(
        self, identity: IdentityContext, command: MemoryWriteCommand
    ) -> Tuple[Optional[str], Optional[str]]:
        """验证写入命令，并生成规范化所有者和会话。"""

        content = command.content.strip()
        if len(content) > self.max_content_chars:
            raise ValidationError("content 超过允许长度")
        unknown_reserved = _RESERVED_METADATA_KEYS.intersection(command.metadata)
        if unknown_reserved:
            raise ValidationError(
                "metadata 包含保留字段: %s" % ", ".join(sorted(unknown_reserved))
            )
        try:
            json.dumps(command.metadata, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ValidationError("metadata 必须可以序列化为 JSON") from error

        if command.sensitivity is Sensitivity.RESTRICTED and not identity.has_any_role(
            "admin", "memory:write_restricted"
        ):
            raise AuthorizationError("无权写入受限记忆")

        if command.scope is MemoryScope.SHARED:
            if not identity.has_any_role("admin", "memory:write_shared"):
                raise AuthorizationError("无权写入共享记忆")
            if command.owner_user_id is not None or command.session_id is not None:
                raise ValidationError("共享记忆不能指定 owner_user_id 或 session_id")
            return None, None

        owner_user_id = command.owner_user_id or identity.actor_user_id
        if owner_user_id != identity.actor_user_id and not identity.has_any_role(
            "admin", "memory:manage"
        ):
            raise AuthorizationError("无权为其他用户写入记忆")

        if command.scope is MemoryScope.USER:
            if command.session_id is not None:
                raise ValidationError("用户记忆不能指定 session_id")
            return owner_user_id, None

        if not command.session_id or not command.session_id.strip():
            raise ValidationError("会话记忆必须指定 session_id")
        return owner_user_id, command.session_id.strip()

    @staticmethod
    def _validate_lifecycle_input(
        memory_id: str, idempotency_key: str, reason: str
    ) -> None:
        """验证撤销和回滚的公共输入。"""

        for field_name, value in (
            ("memory_id", memory_id),
            ("idempotency_key", idempotency_key),
            ("reason", reason),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("%s 不能为空" % field_name)

    @staticmethod
    def _assert_can_manage(identity: IdentityContext, record: MemoryRecord) -> None:
        """检查调用方是否可以修改或检查完整历史。"""

        if record.tenant_id != identity.tenant_id:
            raise AuthorizationError("租户边界不匹配")
        if identity.has_any_role("admin", "memory:manage"):
            return
        if record.owner_user_id == identity.actor_user_id:
            return
        if record.scope is MemoryScope.SHARED and identity.has_any_role("memory:write_shared"):
            return
        raise AuthorizationError("无权管理该记忆")

    @staticmethod
    def _assert_can_read(
        identity: IdentityContext,
        record: MemoryRecord,
        session_id: Optional[str],
    ) -> None:
        """检查记忆读取权限。"""

        if record.tenant_id != identity.tenant_id:
            raise AuthorizationError("租户边界不匹配")
        if record.sensitivity is Sensitivity.RESTRICTED and not identity.has_any_role(
            "admin", "memory:read_restricted"
        ):
            raise AuthorizationError("无权读取受限记忆")
        if identity.has_any_role("admin", "memory:manage"):
            return
        if record.scope is MemoryScope.SHARED:
            return
        if record.owner_user_id != identity.actor_user_id:
            raise AuthorizationError("无权读取其他用户的记忆")
        if record.scope is MemoryScope.SESSION and record.session_id != session_id:
            raise AuthorizationError("会话边界不匹配")

    @staticmethod
    def _write_request_payload(
        command: MemoryWriteCommand,
        owner_user_id: Optional[str],
        session_id: Optional[str],
    ) -> Dict[str, object]:
        """生成不包含可信身份字段的幂等请求载荷。"""

        return {
            "memory_id": command.memory_id,
            "content": command.content.strip(),
            "scope": command.scope.value,
            "owner_user_id": owner_user_id,
            "session_id": session_id,
            "source_type": command.source_type.strip(),
            "source_ref": command.source_ref.strip(),
            "sensitivity": command.sensitivity.value,
            "metadata": command.metadata,
        }
