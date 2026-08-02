"""知识事实库、兼容投影和检索索引的运行时闭环。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sqlite3
import tempfile
import time
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.retrieval import IndexedDocument, TenantAwareLexicalIndex

from .contracts import (
    KnowledgeAuthorizationError,
    KnowledgeCitation,
    KnowledgeCitationIntegrityError,
    KnowledgeCitationVersionError,
    KnowledgeDocumentRecord,
    KnowledgeSearchResult,
    KnowledgeStatus,
    KnowledgeValidationError,
    KnowledgeWriteCommand,
)
from .parser import parse_markdown_source
from .repository import (
    KnowledgeNewDocumentBatchItem,
    KnowledgeRepository,
    make_document_record,
)


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
        "content_hash",
    }
)
_KNOWLEDGE_CANDIDATE_FLOOR = 10
_KNOWLEDGE_CITATION_URI_CORE = (
    r"\Aknowledge://(?P<document_id>[^/?#]+)/v/"
    r"(?P<document_version>[1-9][0-9]*)/sections/"
    r"(?P<section_id>[0-9a-f]{64})/evidence/"
    r"(?P<evidence_id>[0-9a-f]{64})#bytes="
    r"(?P<byte_start>0|[1-9][0-9]*)-(?P<byte_end>[1-9][0-9]*)"
)
_KNOWLEDGE_CITATION_URI_RE = re.compile(
    _KNOWLEDGE_CITATION_URI_CORE
    + r"&content_hash=(?P<content_hash>[0-9a-f]{64})"
    r"&quote_hash=(?P<quote_hash>[0-9a-f]{64})"
    r"&source_ref_hash=(?P<source_ref_hash>[0-9a-f]{64})"
    r"(?:&session_binding=(?P<session_binding>[A-Za-z0-9_-]{1,684}\.[0-9a-f]{1,16}\.[0-9a-f]{64}))?"
    r"&citation_version=(?P<citation_version>3)\Z"
)
_KNOWLEDGE_CITATION_VERSION_RE = re.compile(
    r"(?:\A|[?&#])citation_version=(?P<citation_version>[0-9]+)(?:&|\Z)"
)
_SESSION_BINDING_RE = re.compile(
    r"\A(?P<session>[A-Za-z0-9_-]{1,684})\.(?P<expires>[0-9a-f]{1,16})\.(?P<signature>[0-9a-f]{64})\Z"
)
_CITATION_SECRET_BYTES = 32
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {"com%s" % index for index in range(1, 10)}
    | {"lpt%s" % index for index in range(1, 10)}
)


class _CoordinatedDerivativeSync:
    """让检索事务等待事实提交决定，同时允许两边并行准备。"""

    def __init__(
        self,
        index: TenantAwareLexicalIndex,
        tenant_id: str,
        documents: Sequence[IndexedDocument],
    ):
        self._index = index
        self._tenant_id = tenant_id
        self._documents = tuple(documents)
        self._prepared = threading.Event()
        self._decision = threading.Event()
        self._commit_allowed = False
        self._result = None
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="knowledge-derivative-sync",
        )

    def start(self) -> None:
        self._thread.start()

    def wait_until_prepared(self) -> None:
        self._prepared.wait()
        if self._error is not None:
            raise self._error

    def permit_commit(self) -> None:
        """在事实 SQL 已完成后允许检索事务与事实事务并发提交。"""

        self._commit_allowed = True
        self._decision.set()

    def finish(self, commit: bool, raise_error: bool = True) -> None:
        if not self._decision.is_set():
            self._commit_allowed = bool(commit)
            self._decision.set()
        self._thread.join()
        if raise_error and self._error is not None:
            raise self._error
        if raise_error and commit and self._result is not True:
            raise RuntimeError("知识派生索引没有在事实提交后完成提交")

    def _run(self) -> None:
        try:
            self._result = self._index.replace_tenant_coordinated(
                self._tenant_id,
                self._documents,
                self._allow_commit,
            )
        except BaseException as error:
            self._error = error
        finally:
            self._prepared.set()

    def _allow_commit(self) -> bool:
        self._prepared.set()
        self._decision.wait()
        return self._commit_allowed


class GovernedKnowledgeRuntime:
    """以 SQLite 为事实源管理知识版本、引用和派生索引。"""

    def __init__(
        self,
        workspace_root: str,
        tenant_id: str = "tenant-local",
        migrate_legacy: bool = True,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.knowledge_dir = self.workspace_root / "knowledge"
        self.system_dir = self.knowledge_dir / ".system"
        self.system_dir.mkdir(parents=True, exist_ok=True)
        self._citation_capability_secret = _load_or_create_secret(
            self.system_dir / "citation-capability.key"
        )
        self.tenant_id = tenant_id
        self._lock = threading.RLock()
        self._closed = False
        self.repository = KnowledgeRepository(self.system_dir / "knowledge.db")
        try:
            self.index = TenantAwareLexicalIndex(
                self.system_dir / "retrieval.db",
                candidate_limit=_KNOWLEDGE_CANDIDATE_FLOOR,
            )
            if migrate_legacy:
                self._migrate_legacy_projections_once()
            self.rebuild_derivatives()
        except Exception:
            if hasattr(self, "index"):
                self.index.close()
            self.repository.close()
            self._closed = True
            raise

    def write(
        self,
        identity: IdentityContext,
        command: KnowledgeWriteCommand,
        sync_derivatives: bool = True,
    ) -> KnowledgeDocumentRecord:
        """写入新文档版本，并在提交后刷新投影和索引。"""

        self._assert_identity(identity)
        owner_user_id, session_id = self._validate_write(identity, command)
        if command.projection_path:
            self._resolve_projection(command.projection_path)
        request_hash = self._write_request_hash(
            command, owner_user_id, session_id
        )

        with self._lock:
            with self.repository.transaction() as conn:
                record, _, needs_sync = self._write_in_transaction(
                    conn,
                    identity,
                    command,
                    owner_user_id,
                    session_id,
                    request_hash,
                )

            if sync_derivatives and needs_sync:
                self._drain_derivative_job(
                    identity.tenant_id, record.document_id
                )
            return record

    def write_batch(
        self,
        identity: IdentityContext,
        commands: Sequence[KnowledgeWriteCommand],
        sync_derivatives: bool = True,
    ) -> Sequence[KnowledgeDocumentRecord]:
        """在同一个 SQLite 事务中原子写入一批知识文档。"""

        self._assert_identity(identity)
        commands = tuple(commands)
        prepared = []
        for command in commands:
            if not isinstance(command, KnowledgeWriteCommand):
                raise KnowledgeValidationError(
                    "commands 只能包含 KnowledgeWriteCommand"
                )
            owner_user_id, session_id = self._validate_write(identity, command)
            if command.projection_path:
                self._resolve_projection(command.projection_path)
            prepared.append(
                (
                    command,
                    owner_user_id,
                    session_id,
                    self._write_request_hash(
                        command, owner_user_id, session_id
                    ),
                )
            )
        if not prepared:
            return ()

        coordinated_sync = None
        coordinated_batch_id = None
        derivatives_already_synchronized = False
        with self._lock:
            try:
                outcomes = []
                with self.repository.transaction() as conn:
                    batch_plan = self._prepare_new_document_batch(
                        conn, identity, prepared
                    )
                    if batch_plan is not None:
                        items, batch_outcomes, tenant_was_empty = batch_plan
                        if sync_derivatives and tenant_was_empty:
                            indexed_documents = (
                                self._indexed_documents_from_batch_items(items)
                            )
                            coordinated_sync = _CoordinatedDerivativeSync(
                                self.index,
                                identity.tenant_id,
                                indexed_documents,
                            )
                            coordinated_sync.start()
                            coordinated_batch_id = str(uuid.uuid4())
                        self.repository.insert_new_documents_batch(conn, items)
                        if coordinated_batch_id is not None:
                            self.repository.enqueue_derivative_batch(
                                conn,
                                identity.tenant_id,
                                coordinated_batch_id,
                            )
                        else:
                            self.repository.enqueue_derivative_jobs_batch(
                                conn,
                                [
                                    (
                                        item.record.tenant_id,
                                        item.record.document_id,
                                        item.record.version,
                                    )
                                    for item in items
                                ],
                            )
                        # 事实行和可恢复批次任务已完成 SQL 写入后，才允许派生库提交。
                        # 事实事务最终 commit 仍可能失败；此时搜索会回查事实源并过滤
                        # 孤立派生行，重启时 rebuild_derivatives 会清理它们。
                        if coordinated_sync is not None:
                            coordinated_sync.permit_commit()
                        outcomes.extend(batch_outcomes)
                        if coordinated_sync is not None:
                            coordinated_sync.wait_until_prepared()
                            coordinated_sync.finish(True)
                            if not self.repository.complete_derivative_batch(
                                conn,
                                identity.tenant_id,
                                coordinated_batch_id,
                            ):
                                raise RuntimeError("知识批次派生任务完成标记缺失")
                            derivatives_already_synchronized = True
                    else:
                        for command, owner_user_id, session_id, request_hash in prepared:
                            outcomes.append(
                                self._write_in_transaction(
                                    conn,
                                    identity,
                                    command,
                                    owner_user_id,
                                    session_id,
                                    request_hash,
                                )
                            )
            except BaseException:
                if coordinated_sync is not None:
                    coordinated_sync.finish(False, raise_error=False)
                raise

            if sync_derivatives and not derivatives_already_synchronized:
                sync_outcomes = [outcome for outcome in outcomes if outcome[2]]
                document_ids = []
                seen_document_ids = set()
                for record, _, _ in sync_outcomes:
                    if record.document_id not in seen_document_ids:
                        seen_document_ids.add(record.document_id)
                        document_ids.append(record.document_id)
                for document_id in document_ids:
                    self._drain_derivative_job(
                        identity.tenant_id, document_id
                    )
            return tuple(outcome[0] for outcome in outcomes)

    def _prepare_new_document_batch(
        self,
        conn: sqlite3.Connection,
        identity: IdentityContext,
        prepared: Sequence[tuple],
    ) -> Optional[tuple]:
        """为无批内依赖的新文档构建集合式原子写入计划。"""

        if any(
            command.document_id is not None or command.projection_path is not None
            for command, _, _, _ in prepared
        ):
            return None
        idempotency_keys = [command.idempotency_key for command, _, _, _ in prepared]
        sources = [
            (command.source_ref.strip(), owner_user_id, session_id)
            for command, owner_user_id, session_id, _ in prepared
        ]
        if len(idempotency_keys) != len(set(idempotency_keys)):
            return None
        if len(sources) != len(set(sources)):
            return None
        if self.repository.has_any_idempotency_key(
            conn,
            identity.tenant_id,
            identity.actor_user_id,
            "write",
            idempotency_keys,
        ):
            return None
        if self.repository.has_any_active_source(
            conn, identity.tenant_id, sources
        ):
            return None

        tenant_was_empty = not self.repository.has_any_active_document(
            conn, identity.tenant_id
        )

        items = []
        outcomes = []
        generated_document_ids = set()
        for command, owner_user_id, session_id, request_hash in prepared:
            document_id = str(uuid.uuid4())
            if document_id in generated_document_ids:
                return None
            generated_document_ids.add(document_id)
            parsed = parse_markdown_source(
                command.content,
                command.title,
                identity.tenant_id,
                document_id,
                1,
            )
            record = make_document_record(
                document_id=document_id,
                tenant_id=identity.tenant_id,
                version=1,
                status=KnowledgeStatus.ACTIVE,
                scope=command.scope,
                owner_user_id=owner_user_id,
                session_id=session_id,
                sensitivity=command.sensitivity,
                collection_id=command.collection_id.strip(),
                title=parsed.title,
                source_ref=command.source_ref.strip(),
                projection_path=None,
                content=command.content,
                metadata=command.metadata,
                created_by=identity.actor_user_id,
                trace_id=identity.trace_id,
            )
            audit_details = {
                "auth_source": identity.auth_source,
                "source_ref": record.source_ref,
                "content_hash": record.content_hash,
                "section_count": len(parsed.sections),
                "evidence_count": len(parsed.evidence),
            }
            items.append(
                KnowledgeNewDocumentBatchItem(
                    record=record,
                    parser_version=parsed.parser_version,
                    sections=parsed.sections,
                    evidence=parsed.evidence,
                    audit_event_id=str(uuid.uuid4()),
                    audit_action="knowledge.created",
                    audit_details=audit_details,
                    idempotency_key=command.idempotency_key,
                    request_hash=request_hash,
                )
            )
            outcomes.append((record, None, True))
        return tuple(items), tuple(outcomes), tenant_was_empty

    @staticmethod
    def _indexed_documents_from_batch_items(
        items: Sequence[KnowledgeNewDocumentBatchItem],
    ) -> Sequence[IndexedDocument]:
        """复用已解析证据构造派生输入，避免提交后再次读取事实库。"""

        documents = []
        for item in items:
            record = item.record
            for evidence in item.evidence:
                documents.append(
                    IndexedDocument(
                        tenant_id=record.tenant_id,
                        document_id=evidence.evidence_id,
                        scope=record.scope,
                        owner_user_id=record.owner_user_id,
                        session_id=record.session_id,
                        sensitivity=record.sensitivity,
                        title=record.title,
                        text=evidence.quote,
                        source_ref=record.source_ref,
                        collection_id=record.collection_id,
                        metadata={
                            "source": "governed-knowledge",
                            "document_id": record.document_id,
                            "document_version": record.version,
                            "section_id": evidence.section_id,
                            "evidence_id": evidence.evidence_id,
                            "byte_start": evidence.byte_start,
                            "byte_end": evidence.byte_end,
                            "content_hash": record.content_hash,
                            "quote_hash": evidence.quote_hash,
                        },
                    )
                )
        return tuple(documents)

    def _write_in_transaction(
        self,
        conn: sqlite3.Connection,
        identity: IdentityContext,
        command: KnowledgeWriteCommand,
        owner_user_id: Optional[str],
        session_id: Optional[str],
        request_hash: str,
    ) -> Tuple[KnowledgeDocumentRecord, Optional[str], bool]:
        """执行一次已校验写入；事务的开启、提交和回滚由调用方负责。"""

        existing = self.repository.find_idempotent_result(
            conn,
            identity.tenant_id,
            identity.actor_user_id,
            "write",
            command.idempotency_key,
            request_hash,
        )
        if existing is not None:
            latest = self.repository.get_latest(
                conn, identity.tenant_id, existing.document_id
            )
            self.repository.enqueue_derivative_job(
                conn,
                identity.tenant_id,
                existing.document_id,
                latest.version,
            )
            return existing, None, True

        previous = None
        if command.document_id:
            previous = self.repository.get_latest(
                conn, identity.tenant_id, command.document_id
            )
            self._assert_can_manage(identity, previous)
        else:
            previous = self.repository.get_active_by_source(
                conn,
                identity.tenant_id,
                command.source_ref.strip(),
                owner_user_id,
                session_id,
            )
        if previous is None:
            document_id = str(uuid.uuid4())
            version = 1
            action = "knowledge.created"
            previous_projection = None
        else:
            self._assert_can_manage(identity, previous)
            document_id = previous.document_id
            version = self.repository.next_version(
                conn, identity.tenant_id, document_id
            )
            previous_projection = previous.projection_path
            self.repository.supersede_active(
                conn, identity.tenant_id, document_id
            )

            action = "knowledge.updated"

        projection_path = (
            self._canonical_projection_path(command.projection_path)
            if command.projection_path
            else None
        )
        if projection_path:
            logical_owner = self.repository.get_active_by_logical_path(
                conn,
                identity.tenant_id,
                projection_path,
                command.scope,
                owner_user_id,
                session_id,
            )
            if (
                logical_owner is not None
                and logical_owner.document_id != document_id
            ):
                raise KnowledgeValidationError(
                    "projection_path（知识逻辑路径）已被同一身份作用域内的其他文档占用"
                )
            if self._is_compatibility_projection_values(
                command.scope, command.sensitivity
            ):
                projection_owner = self.repository.get_active_by_projection(
                    conn,
                    projection_path,
                )
                if (
                    projection_owner is not None
                    and (
                        projection_owner.tenant_id != identity.tenant_id
                        or projection_owner.document_id != document_id
                    )
                ):
                    raise KnowledgeValidationError(
                        "projection_path 已被其他知识文档占用"
                    )

        parsed = parse_markdown_source(
            command.content,
            command.title,
            identity.tenant_id,
            document_id,
            version,
        )
        record = make_document_record(
            document_id=document_id,
            tenant_id=identity.tenant_id,
            version=version,
            status=KnowledgeStatus.ACTIVE,
            scope=command.scope,
            owner_user_id=owner_user_id,
            session_id=session_id,
            sensitivity=command.sensitivity,
            collection_id=command.collection_id.strip(),
            title=parsed.title,
            source_ref=command.source_ref.strip(),
            projection_path=projection_path,
            content=command.content,
            metadata=command.metadata,
            created_by=identity.actor_user_id,
            trace_id=identity.trace_id,
        )
        self.repository.insert_document(
            conn,
            record,
            parsed.parser_version,
            parsed.sections,
            parsed.evidence,
        )
        self.repository.append_audit(
            conn,
            str(uuid.uuid4()),
            identity.tenant_id,
            identity.trace_id,
            identity.actor_user_id,
            action,
            document_id,
            version,
            {
                "auth_source": identity.auth_source,
                "source_ref": record.source_ref,
                "content_hash": record.content_hash,
                "section_count": len(parsed.sections),
                "evidence_count": len(parsed.evidence),
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
            document_id,
            version,
        )
        return record, previous_projection, True

    def _write_request_hash(
        self,
        command: KnowledgeWriteCommand,
        owner_user_id: Optional[str],
        session_id: Optional[str],
    ) -> str:
        """生成与单条和批量路径完全一致的幂等请求摘要。"""

        return self.repository.request_hash(
            {
                "document_id": command.document_id,
                "content": command.content,
                "title": command.title.strip(),
                "source_ref": command.source_ref.strip(),
                "collection_id": command.collection_id.strip(),
                "projection_path": command.projection_path,
                "scope": command.scope.value,
                "owner_user_id": owner_user_id,
                "session_id": session_id,
                "sensitivity": command.sensitivity.value,
                "metadata": command.metadata,
            }
        )

    def revoke(
        self,
        identity: IdentityContext,
        document_id: str,
        idempotency_key: str,
        reason: str,
    ) -> KnowledgeDocumentRecord:
        """撤销当前版本，同时保留原文和全部历史。"""

        self._assert_lifecycle_input(document_id, idempotency_key, reason)
        request_hash = self.repository.request_hash(
            {"document_id": document_id, "reason": reason.strip()}
        )
        with self._lock:
            with self.repository.transaction() as conn:
                existing = self.repository.find_idempotent_result(
                    conn,
                    identity.tenant_id,
                    identity.actor_user_id,
                    "revoke",
                    idempotency_key,
                    request_hash,
                )
                if existing is not None:
                    record = existing
                else:
                    previous = self.repository.get_active(
                        conn, identity.tenant_id, document_id
                    )
                    self._assert_can_manage(identity, previous)
                    version = self.repository.next_version(
                        conn, identity.tenant_id, document_id
                    )
                    self.repository.supersede_active(
                        conn, identity.tenant_id, document_id
                    )
                    record = make_document_record(
                        document_id=document_id,
                        tenant_id=identity.tenant_id,
                        version=version,
                        status=KnowledgeStatus.REVOKED,
                        scope=previous.scope,
                        owner_user_id=previous.owner_user_id,
                        session_id=previous.session_id,
                        sensitivity=previous.sensitivity,
                        collection_id=previous.collection_id,
                        title=previous.title,
                        source_ref=previous.source_ref,
                        projection_path=previous.projection_path,
                        content=previous.content,
                        metadata=previous.metadata,
                        created_by=identity.actor_user_id,
                        trace_id=identity.trace_id,
                    )
                    self.repository.insert_document(
                        conn, record, "lifecycle-v1", (), ()
                    )
                    self.repository.append_audit(
                        conn,
                        str(uuid.uuid4()),
                        identity.tenant_id,
                        identity.trace_id,
                        identity.actor_user_id,
                        "knowledge.revoked",
                        document_id,
                        version,
                        {
                            "auth_source": identity.auth_source,
                            "reason": reason.strip(),
                        },
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
                latest = self.repository.get_latest(
                    conn, identity.tenant_id, document_id
                )
                self.repository.enqueue_derivative_job(
                    conn,
                    identity.tenant_id,
                    document_id,
                    latest.version,
                )
            self._drain_derivative_job(identity.tenant_id, document_id)
            return record

    def rollback(
        self,
        identity: IdentityContext,
        document_id: str,
        target_version: int,
        idempotency_key: str,
        reason: str,
    ) -> KnowledgeDocumentRecord:
        """把历史正文恢复成新的有效版本，不覆盖旧记录。"""

        self._assert_lifecycle_input(document_id, idempotency_key, reason)
        if not isinstance(target_version, int) or isinstance(target_version, bool) or target_version <= 0:
            raise KnowledgeValidationError("target_version 必须是正整数")
        request_hash = self.repository.request_hash(
            {
                "document_id": document_id,
                "target_version": target_version,
                "reason": reason.strip(),
            }
        )
        with self._lock:
            with self.repository.transaction() as conn:
                existing = self.repository.find_idempotent_result(
                    conn,
                    identity.tenant_id,
                    identity.actor_user_id,
                    "rollback",
                    idempotency_key,
                    request_hash,
                )
                if existing is not None:
                    record = existing
                else:
                    latest = self.repository.get_latest(
                        conn, identity.tenant_id, document_id
                    )
                    self._assert_can_manage(identity, latest)
                    target = self.repository.get_version(
                        conn, identity.tenant_id, document_id, target_version
                    )
                    if target.projection_path:
                        logical_owner = self.repository.get_active_by_logical_path(
                            conn,
                            identity.tenant_id,
                            target.projection_path,
                            target.scope,
                            target.owner_user_id,
                            target.session_id,
                        )
                        if (
                            logical_owner is not None
                            and logical_owner.document_id != document_id
                        ):
                            raise KnowledgeValidationError(
                                "projection_path（知识逻辑路径）已被同一身份作用域内的其他文档占用"
                            )
                    if target.projection_path and self._is_compatibility_projection_values(
                        target.scope, target.sensitivity
                    ):
                        projection_owner = self.repository.get_active_by_projection(
                            conn, target.projection_path
                        )
                        if (
                            projection_owner is not None
                            and (
                                projection_owner.tenant_id != identity.tenant_id
                                or projection_owner.document_id != document_id
                            )
                        ):
                            raise KnowledgeValidationError(
                                "projection_path 已被其他知识文档占用"
                            )
                    version = self.repository.next_version(
                        conn, identity.tenant_id, document_id
                    )
                    self.repository.supersede_active(
                        conn, identity.tenant_id, document_id
                    )
                    parsed = parse_markdown_source(
                        target.content,
                        target.title,
                        identity.tenant_id,
                        document_id,
                        version,
                    )
                    record = make_document_record(
                        document_id=document_id,
                        tenant_id=identity.tenant_id,
                        version=version,
                        status=KnowledgeStatus.ACTIVE,
                        scope=target.scope,
                        owner_user_id=target.owner_user_id,
                        session_id=target.session_id,
                        sensitivity=target.sensitivity,
                        collection_id=target.collection_id,
                        title=parsed.title,
                        source_ref=target.source_ref,
                        projection_path=target.projection_path,
                        content=target.content,
                        metadata=target.metadata,
                        created_by=identity.actor_user_id,
                        trace_id=identity.trace_id,
                    )
                    self.repository.insert_document(
                        conn,
                        record,
                        parsed.parser_version,
                        parsed.sections,
                        parsed.evidence,
                    )
                    self.repository.append_audit(
                        conn,
                        str(uuid.uuid4()),
                        identity.tenant_id,
                        identity.trace_id,
                        identity.actor_user_id,
                        "knowledge.rolled_back",
                        document_id,
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
                latest = self.repository.get_latest(
                    conn, identity.tenant_id, document_id
                )
                self.repository.enqueue_derivative_job(
                    conn,
                    identity.tenant_id,
                    document_id,
                    latest.version,
                )
            self._drain_derivative_job(identity.tenant_id, document_id)
            return record

    def get(
        self,
        identity: IdentityContext,
        document_id: str,
        version: Optional[int] = None,
        session_id: Optional[str] = None,
    ) -> KnowledgeDocumentRecord:
        """读取当前版本；读取历史版本还需要管理权限。"""

        self._assert_identity(identity)
        if version is None:
            record = self.repository.read_active(identity.tenant_id, document_id)
        else:
            record = self.repository.read_version(
                identity.tenant_id, document_id, version
            )
            self._assert_can_manage(identity, record)
        self._assert_can_read(identity, record, session_id)
        return record

    def find_by_source(
        self,
        identity: IdentityContext,
        source_ref: str,
        session_id: Optional[str] = None,
    ) -> Optional[KnowledgeDocumentRecord]:
        """按来源查找当前可见版本。"""

        ownership_candidates = [
            (identity.actor_user_id, None),
            (identity.actor_user_id, session_id),
            (None, None),
        ]
        seen = set()
        for owner_user_id, candidate_session_id in ownership_candidates:
            key = (owner_user_id, candidate_session_id)
            if key in seen:
                continue
            seen.add(key)
            record = self.repository.find_active_by_source(
                identity.tenant_id,
                source_ref,
                owner_user_id,
                candidate_session_id,
            )
            if record is None:
                continue
            try:
                self._assert_can_read(identity, record, session_id)
            except KnowledgeAuthorizationError:
                continue
            return record
        return None

    def find_by_logical_path(
        self,
        identity: IdentityContext,
        logical_path: str,
        session_id: Optional[str] = None,
    ) -> Optional[KnowledgeDocumentRecord]:
        """按会话、用户、共享的优先级查找当前身份可见路径。"""

        self._assert_identity(identity)
        canonical = self._canonical_projection_path(logical_path)
        candidates = []
        if session_id:
            candidates.append(
                (MemoryScope.SESSION, identity.actor_user_id, session_id)
            )
        candidates.extend(
            (
                (MemoryScope.USER, identity.actor_user_id, None),
                (MemoryScope.SHARED, None, None),
            )
        )
        if identity.has_any_role("admin", "knowledge:manage"):
            visible = [
                record
                for record in self.list_active(identity, session_id=session_id)
                if record.projection_path
                and self.repository.projection_key(record.projection_path)
                == self.repository.projection_key(canonical)
            ]
            if visible:
                visible.sort(
                    key=lambda record: (
                        record.owner_user_id != identity.actor_user_id,
                        record.scope is MemoryScope.SHARED,
                        record.document_id,
                    )
                )
                return visible[0]
        for scope, owner_user_id, candidate_session_id in candidates:
            record = self.repository.find_active_by_logical_path(
                identity.tenant_id,
                canonical,
                scope,
                owner_user_id,
                candidate_session_id,
            )
            if record is None:
                continue
            try:
                self._assert_can_read(identity, record, session_id)
            except KnowledgeAuthorizationError:
                continue
            return record
        return None

    def search(
        self,
        identity: IdentityContext,
        query: str,
        limit: int = 10,
        collection_ids: Optional[Sequence[str]] = None,
        session_id: Optional[str] = None,
    ) -> Sequence[KnowledgeSearchResult]:
        """先在索引层过滤权限，再回查事实库并核验每条引用。"""

        self._assert_identity(identity)
        if not isinstance(query, str) or not query.strip():
            raise KnowledgeValidationError("query 不能为空")
        if len(query) > 512:
            raise KnowledgeValidationError("query 超过 512 个字符")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise KnowledgeValidationError("limit 必须在 1 到 20 之间")
        candidates = self.index.search(
            identity,
            query.strip(),
            limit=max(_KNOWLEDGE_CANDIDATE_FLOOR, limit),
            session_id=session_id,
            collection_ids=collection_ids,
        )
        rows = self.repository.resolve_active_evidence(
            identity.tenant_id,
            [candidate.document_id for candidate in candidates],
        )
        results = []
        returned_documents = set()
        for candidate in candidates:
            row = rows.get(candidate.document_id)
            if row is None or not self._can_read_row(identity, row, session_id):
                continue
            document_id = str(row["document_id"])
            if document_id in returned_documents:
                continue
            citation = self._verified_citation(row, session_id=session_id)
            if citation is None:
                continue
            returned_documents.add(document_id)
            results.append(
                KnowledgeSearchResult(
                    title=str(row["title"]),
                    collection_id=str(row["collection_id"]),
                    score=candidate.score,
                    bm25_score=candidate.bm25_score,
                    query_coverage=candidate.query_coverage,
                    citation=citation,
                )
            )
            if len(results) >= limit:
                break
        return results

    def resolve_verified_citation(
        self,
        identity: IdentityContext,
        uri: str,
        session_id: Optional[str] = None,
    ) -> KnowledgeCitation:
        """把完整引用回查到当前有效证据，并重新执行权限与完整性校验。"""

        self._assert_identity(identity)
        if not isinstance(uri, str) or not uri or len(uri) > 4096:
            raise KnowledgeCitationIntegrityError("knowledge:// 引用格式无效")
        version_match = _KNOWLEDGE_CITATION_VERSION_RE.search(uri)
        if (
            version_match is not None
            and version_match.group("citation_version") != "3"
        ):
            raise KnowledgeCitationVersionError(
                "knowledge:// 引用协议版本不受支持，请重新检索"
            )
        match = _KNOWLEDGE_CITATION_URI_RE.fullmatch(uri)
        if match is None:
            raise KnowledgeCitationIntegrityError("knowledge:// 引用格式无效")

        document_id = match.group("document_id")
        document_version = int(match.group("document_version"))
        section_id = match.group("section_id")
        evidence_id = match.group("evidence_id")
        byte_start = int(match.group("byte_start"))
        byte_end = int(match.group("byte_end"))
        content_hash = match.group("content_hash")
        quote_hash = match.group("quote_hash")
        source_ref_hash = match.group("source_ref_hash")
        session_binding = match.group("session_binding")
        citation_version = int(match.group("citation_version"))

        row = self.repository.resolve_active_evidence(
            identity.tenant_id, (evidence_id,)
        ).get(evidence_id)
        if row is None:
            raise KnowledgeCitationIntegrityError(
                "knowledge:// 引用不存在或已失效"
            )
        row_scope = MemoryScope(str(row["scope"]))
        effective_session_id = session_id
        if row_scope is MemoryScope.SESSION:
            if not session_binding:
                raise KnowledgeCitationIntegrityError(
                    "session-scoped citation is missing its server capability"
                )
            bound_session_id = self._verify_session_binding(row, session_binding)
            if session_id is not None and session_id != bound_session_id:
                raise KnowledgeAuthorizationError("session boundary mismatch")
            effective_session_id = bound_session_id
        elif session_binding is not None:
            raise KnowledgeCitationIntegrityError(
                "non-session citation contains a session capability"
            )

        if not self._can_read_row(identity, row, effective_session_id):
            raise KnowledgeAuthorizationError("citation read is not authorized")

        citation = self._verified_citation(
            row,
            session_id=effective_session_id,
            session_binding_override=session_binding,
        )
        if citation is None:
            raise KnowledgeCitationIntegrityError(
                "knowledge:// 引用完整性校验失败"
            )
        parsed_identity = (
            document_id,
            document_version,
            section_id,
            evidence_id,
            byte_start,
            byte_end,
            content_hash,
            quote_hash,
            source_ref_hash,
            citation_version,
        )
        verified_identity = (
            citation.document_id,
            citation.document_version,
            citation.section_id,
            citation.evidence_id,
            citation.byte_start,
            citation.byte_end,
            citation.content_hash,
            citation.quote_hash,
            citation.source_ref_hash,
            citation.citation_version,
        )
        if parsed_identity != verified_identity or citation.uri != uri:
            raise KnowledgeCitationIntegrityError(
                "knowledge:// 引用内容不匹配或已失效"
            )
        return citation

    def list_active(
        self,
        identity: IdentityContext,
        session_id: Optional[str] = None,
    ) -> Sequence[KnowledgeDocumentRecord]:
        """列出当前身份可见的有效知识文档。"""

        self._assert_identity(identity)
        records = []
        for record in self.repository.list_active_records(identity.tenant_id):
            try:
                self._assert_can_read(identity, record, session_id)
            except KnowledgeAuthorizationError:
                continue
            records.append(record)
        return records

    def create_category(
        self,
        identity: IdentityContext,
        category_path: str,
        scope: MemoryScope = MemoryScope.USER,
        session_id: Optional[str] = None,
    ) -> bool:
        """在事实库中创建身份作用域内的空分类。"""

        self._assert_identity(identity)
        canonical = self._canonical_category_path(category_path)
        owner_user_id, bound_session_id = self._category_ownership(
            identity, scope, session_id
        )
        with self._lock:
            return self.repository.create_category(
                identity.tenant_id,
                scope,
                owner_user_id,
                bound_session_id,
                canonical,
            )

    def list_categories(
        self,
        identity: IdentityContext,
        session_id: Optional[str] = None,
    ) -> Sequence[str]:
        """列出当前身份可见的分类事实。"""

        self._assert_identity(identity)
        paths = set()
        for row in self.repository.list_categories(identity.tenant_id):
            if self._can_read_category_row(identity, row, session_id):
                paths.add(str(row["category_path"]))
        return tuple(sorted(paths, key=self.repository.projection_key))

    def rename_category_facts(
        self,
        identity: IdentityContext,
        old_path: str,
        new_path: str,
        session_id: Optional[str] = None,
    ) -> int:
        """原子重命名当前身份可管理的分类事实及其子分类。"""

        self._assert_identity(identity)
        old_canonical = self._canonical_category_path(old_path)
        new_canonical = self._canonical_category_path(new_path)
        old_key = self.repository.projection_key(old_canonical)
        prefix = old_key + "/"
        with self._lock:
            with self.repository.transaction() as conn:
                rows = [
                    row
                    for row in self.repository.list_categories_in_transaction(
                        conn, identity.tenant_id
                    )
                    if (
                        str(row["category_key"]) == old_key
                        or str(row["category_key"]).startswith(prefix)
                    )
                    and self._can_manage_category_row(identity, row, session_id)
                ]
                replacements = []
                for row in rows:
                    suffix = str(row["category_path"])[len(old_canonical):]
                    replacement = new_canonical + suffix
                    replacements.append((row, replacement))
                for row, _ in replacements:
                    self.repository.delete_category_in_transaction(
                        conn,
                        identity.tenant_id,
                        MemoryScope(str(row["scope"])),
                        str(row["owner_user_id"]) or None,
                        str(row["session_id"]) or None,
                        str(row["category_path"]),
                        include_descendants=False,
                    )
                for row, replacement in replacements:
                    self.repository.insert_category(
                        conn,
                        identity.tenant_id,
                        MemoryScope(str(row["scope"])),
                        str(row["owner_user_id"]) or None,
                        str(row["session_id"]) or None,
                        replacement,
                    )
                return len(replacements)

    def delete_category_facts(
        self,
        identity: IdentityContext,
        category_path: str,
        session_id: Optional[str] = None,
    ) -> int:
        """删除当前身份可管理分类及其子分类，不触碰文档事实。"""

        self._assert_identity(identity)
        canonical = self._canonical_category_path(category_path)
        key = self.repository.projection_key(canonical)
        prefix = key + "/"
        deleted = 0
        with self._lock:
            with self.repository.transaction() as conn:
                rows = [
                    row
                    for row in self.repository.list_categories_in_transaction(
                        conn, identity.tenant_id
                    )
                    if (
                        str(row["category_key"]) == key
                        or str(row["category_key"]).startswith(prefix)
                    )
                    and self._can_manage_category_row(identity, row, session_id)
                ]
                for row in rows:
                    deleted += self.repository.delete_category_in_transaction(
                        conn,
                        identity.tenant_id,
                        MemoryScope(str(row["scope"])),
                        str(row["owner_user_id"]) or None,
                        str(row["session_id"]) or None,
                        str(row["category_path"]),
                        include_descendants=False,
                    )
        return deleted

    def rebuild_derivatives(self) -> None:
        """从事实库重建全部兼容投影和知识检索索引。"""

        with self._lock:
            with self.repository.transaction() as conn:
                stale_paths = (
                    self.repository.list_stale_projection_paths_in_transaction(
                        conn
                    )
                )
                for projection_path in stale_paths:
                    self._remove_projection(projection_path)
                records = self.repository.list_active_records_in_transaction(
                    conn, self.tenant_id
                )
                for record in records:
                    if self.is_compatibility_projection(record):
                        self._write_projection(record)
                    elif record.projection_path:
                        owner = self.repository.get_active_by_projection(
                            conn, record.projection_path
                        )
                        if owner is None:
                            self._remove_projection(record.projection_path)
                rows = self.repository.list_evidence_for_active_in_transaction(
                    conn, self.tenant_id
                )
                indexed_documents = [
                    self._indexed_document(row) for row in rows
                ]
                self.index.replace_tenant(
                    self.tenant_id,
                    indexed_documents,
                )
                if not self.index.matches_tenant(
                    self.tenant_id, indexed_documents
                ):
                    raise RuntimeError(
                        "知识派生索引重建后缺失、多余或内容不一致"
                    )
                self.repository.clear_derivative_jobs(conn, self.tenant_id)

    def close(self) -> None:
        """释放索引连接；事实库只使用短连接。"""

        with self._lock:
            if self._closed:
                return
            self.index.close()
            self.repository.close()
            self._closed = True

    def _migrate_legacy_projections_once(self) -> None:
        """只在首次启用事实库时导入旧 Markdown 页面。"""

        state_key = "legacy_projection_migration_v1:%s" % self.tenant_id
        if self.repository.get_state(state_key) == "completed":
            return
        identity = IdentityContext(
            tenant_id=self.tenant_id,
            actor_user_id="knowledge-migration",
            roles=frozenset({"admin", "knowledge:write_shared", "knowledge:manage"}),
            trace_id="knowledge-migration-%s" % uuid.uuid4().hex,
            auth_source="smart-assistant-knowledge-migration",
        )
        if self.knowledge_dir.exists():
            for path in sorted(self.knowledge_dir.rglob("*.md")):
                relative = path.relative_to(self.knowledge_dir)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                rel_path = relative.as_posix()
                if rel_path in {"index.md", "log.md"}:
                    continue
                content = path.read_text(encoding="utf-8")
                source_ref = "knowledge/%s" % rel_path
                if self.repository.find_active_by_source(
                    self.tenant_id, source_ref, None, None
                ):
                    continue
                title = _first_heading(content) or path.stem
                self.write(
                    identity,
                    KnowledgeWriteCommand(
                        content=content,
                        title=title,
                        source_ref=source_ref,
                        collection_id=relative.parts[0] if len(relative.parts) > 1 else "root",
                        idempotency_key="legacy-%s" % _sha256(source_ref + "\n" + content),
                        projection_path=rel_path,
                        scope=MemoryScope.SHARED,
                        sensitivity=Sensitivity.INTERNAL,
                        metadata={"migration": "legacy_projection_v1"},
                    ),
                    sync_derivatives=False,
                )
            for directory in sorted(self.knowledge_dir.rglob("*")):
                if not directory.is_dir():
                    continue
                relative = directory.relative_to(self.knowledge_dir)
                if not relative.parts or any(
                    part.startswith(".") for part in relative.parts
                ):
                    continue
                self.create_category(
                    identity,
                    relative.as_posix(),
                    scope=MemoryScope.SHARED,
                )
        self.repository.set_state(state_key, "completed")

    def _drain_derivative_job(
        self, tenant_id: str, document_id: str
    ) -> None:
        """在事实库写锁内把一个派生任务收敛到当前不可变版本。"""

        with self._lock:
            with self.repository.transaction() as conn:
                target_version = self.repository.get_derivative_job(
                    conn, tenant_id, document_id
                )
                if target_version is None:
                    return
                latest = self.repository.get_latest(
                    conn, tenant_id, document_id
                )
                if latest.version != target_version:
                    raise RuntimeError(
                        "知识派生任务版本与事实库最新版本不一致"
                    )

                projection_paths = (
                    self.repository.list_projection_paths_in_transaction(
                        conn, tenant_id, document_id
                    )
                )
                active_projection_key = (
                    self.repository.projection_key(latest.projection_path)
                    if latest.status is KnowledgeStatus.ACTIVE
                    and self.is_compatibility_projection(latest)
                    else None
                )
                for projection_path in projection_paths:
                    if (
                        active_projection_key is not None
                        and self.repository.projection_key(projection_path)
                        == active_projection_key
                    ):
                        continue
                    owner = self.repository.get_active_by_projection(
                        conn, projection_path
                    )
                    if owner is None or not self.is_compatibility_projection(owner):
                        self._remove_projection(projection_path)

                if latest.status is KnowledgeStatus.ACTIVE:
                    self._write_projection(latest)
                for evidence_id in (
                    self.repository.list_evidence_ids_in_transaction(
                        conn, tenant_id, document_id
                    )
                ):
                    self.index.delete_document(tenant_id, evidence_id)
                    if self.index.contains_document(tenant_id, evidence_id):
                        raise RuntimeError("知识派生索引删除后仍有残留")
                if latest.status is KnowledgeStatus.ACTIVE:
                    rows = (
                        self.repository.list_active_evidence_for_document_in_transaction(
                            conn, tenant_id, document_id
                        )
                    )
                    indexed_documents = [self._indexed_document(row) for row in rows]
                    self.index.index_documents(indexed_documents)
                    for indexed_document in indexed_documents:
                        if not self.index.matches_document(indexed_document):
                            raise RuntimeError(
                                "知识派生索引写入后缺失或内容不一致: %s"
                                % indexed_document.document_id
                            )
                if not self.repository.complete_derivative_job(
                    conn, tenant_id, document_id, target_version
                ):
                    raise RuntimeError("知识派生任务完成标记发生并发冲突")

    def _write_projection(self, record: KnowledgeDocumentRecord) -> None:
        if not self.is_compatibility_projection(record):
            return
        path = self._resolve_projection(record.projection_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=str(path.parent),
            prefix=".%s." % path.name,
            suffix=".tmp",
        )
        temporary_path = Path(handle.name)
        try:
            with handle:
                handle.write(record.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def is_compatibility_projection(record: KnowledgeDocumentRecord) -> bool:
        """只允许租户共享且非受限的事实生成明文兼容投影。"""

        return (
            record.status is KnowledgeStatus.ACTIVE
            and bool(record.projection_path)
            and GovernedKnowledgeRuntime._is_compatibility_projection_values(
                record.scope, record.sensitivity
            )
        )

    @staticmethod
    def _is_compatibility_projection_values(
        scope: MemoryScope, sensitivity: Sensitivity
    ) -> bool:
        return scope is MemoryScope.SHARED and sensitivity in {
            Sensitivity.PUBLIC,
            Sensitivity.INTERNAL,
        }

    def _remove_projection(self, projection_path: str) -> None:
        path = self._resolve_projection(projection_path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _resolve_projection(self, projection_path: str) -> Path:
        normalized = unicodedata.normalize(
            "NFC", projection_path.replace("\\", "/").strip("/")
        )
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.casefold() in {"index.md", "log.md"}
            or not normalized.lower().endswith(".md")
            or any(
                not part.strip()
                or part in {".", ".."}
                or part.startswith(".")
                or part.rstrip(" .") != part
                or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
                or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
                for part in parts
            )
        ):
            raise KnowledgeValidationError("projection_path 无效")
        candidate = (self.knowledge_dir.joinpath(*parts)).resolve()
        try:
            candidate.relative_to(self.knowledge_dir.resolve())
        except ValueError as error:
            raise KnowledgeValidationError("projection_path 超出知识目录") from error
        return candidate

    def _canonical_projection_path(self, projection_path: str) -> str:
        """验证并返回与物理文件系统一致的规范投影路径。"""

        normalized = unicodedata.normalize(
            "NFC", projection_path.replace("\\", "/").strip("/")
        )
        self._resolve_projection(normalized)
        return normalized

    def _canonical_category_path(self, category_path: str) -> str:
        """验证并返回不依赖物理目录存在性的规范分类路径。"""

        if not isinstance(category_path, str):
            raise KnowledgeValidationError("category_path 必须是字符串")
        normalized = unicodedata.normalize(
            "NFC", category_path.replace("\\", "/").strip("/")
        )
        parts = normalized.split("/")
        if (
            not normalized
            or any(
                not part.strip()
                or part in {".", ".."}
                or part.startswith(".")
                or part.rstrip(" .") != part
                or any(
                    character in '<>:"|?*' or ord(character) < 32
                    for character in part
                )
                or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES
                for part in parts
            )
        ):
            raise KnowledgeValidationError("category_path 无效")
        return normalized

    @staticmethod
    def _category_ownership(
        identity: IdentityContext,
        scope: MemoryScope,
        session_id: Optional[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        if not isinstance(scope, MemoryScope):
            raise KnowledgeValidationError("scope 必须是 MemoryScope")
        if scope is MemoryScope.SHARED:
            if not identity.has_any_role("admin", "knowledge:write_shared"):
                raise KnowledgeAuthorizationError("无权创建共享知识分类")
            return None, None
        if scope is MemoryScope.USER:
            return identity.actor_user_id, None
        if not session_id or not session_id.strip():
            raise KnowledgeValidationError("会话分类必须绑定 session_id")
        return identity.actor_user_id, session_id.strip()

    @staticmethod
    def _can_read_category_row(
        identity: IdentityContext, row, session_id: Optional[str]
    ) -> bool:
        if identity.has_any_role("admin", "knowledge:manage"):
            return True
        scope = MemoryScope(str(row["scope"]))
        if scope is MemoryScope.SHARED:
            return True
        if str(row["owner_user_id"]) != identity.actor_user_id:
            return False
        return scope is not MemoryScope.SESSION or str(row["session_id"]) == session_id

    @staticmethod
    def _can_manage_category_row(
        identity: IdentityContext, row, session_id: Optional[str]
    ) -> bool:
        if identity.has_any_role("admin", "knowledge:manage"):
            return True
        scope = MemoryScope(str(row["scope"]))
        if scope is MemoryScope.SHARED:
            return identity.has_any_role("knowledge:write_shared")
        if str(row["owner_user_id"]) != identity.actor_user_id:
            return False
        return scope is not MemoryScope.SESSION or str(row["session_id"]) == session_id

    @staticmethod
    def _indexed_document(row) -> IndexedDocument:
        return IndexedDocument(
            tenant_id=str(row["tenant_id"]) if "tenant_id" in row.keys() else "",
            document_id=str(row["evidence_id"]),
            scope=MemoryScope(str(row["scope"])),
            owner_user_id=row["owner_user_id"],
            session_id=row["session_id"],
            sensitivity=Sensitivity(str(row["sensitivity"])),
            title=str(row["title"]),
            text=str(row["quote"]),
            source_ref=str(row["source_ref"]),
            collection_id=str(row["collection_id"]),
            metadata={
                "source": "governed-knowledge",
                "document_id": str(row["document_id"]),
                "document_version": int(row["document_version"]),
                "section_id": str(row["section_id"]),
                "evidence_id": str(row["evidence_id"]),
                "byte_start": int(row["byte_start"]),
                "byte_end": int(row["byte_end"]),
                "content_hash": str(row["content_hash"]),
                "quote_hash": str(row["quote_hash"]),
            },
        )

    @staticmethod
    def _session_binding_message(
        row, encoded_session: str, expires_hex: str
    ) -> bytes:
        payload = {
            "tenant_id": str(row["tenant_id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "session_id": str(row["session_id"]),
            "document_id": str(row["document_id"]),
            "document_version": int(row["document_version"]),
            "section_id": str(row["section_id"]),
            "evidence_id": str(row["evidence_id"]),
            "content_hash": str(row["content_hash"]),
            "quote_hash": str(row["quote_hash"]),
            "source_ref_hash": _sha256(str(row["source_ref"])),
            "encoded_session": encoded_session,
            "expires_at": expires_hex,
        }
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _session_binding(
        self, row, session_id: Optional[str]
    ) -> Optional[str]:
        if MemoryScope(str(row["scope"])) is not MemoryScope.SESSION:
            return None
        row_session_id = str(row["session_id"] or "")
        if not session_id or session_id != row_session_id:
            raise KnowledgeAuthorizationError("???????")
        encoded = base64.urlsafe_b64encode(
            row_session_id.encode("utf-8")
        ).decode("ascii").rstrip("=")
        try:
            from config import conf
            ttl = int(conf().get("knowledge_session_citation_ttl_seconds", 86400))
        except Exception:
            ttl = 86400
        ttl = max(60, min(ttl, 30 * 86400))
        expires_hex = format(int(time.time()) + ttl, "x")
        signature = hmac.new(
            self._citation_capability_secret,
            self._session_binding_message(row, encoded, expires_hex),
            hashlib.sha256,
        ).hexdigest()
        return f"{encoded}.{expires_hex}.{signature}"

    def _verify_session_binding(self, row, binding: str) -> str:
        match = _SESSION_BINDING_RE.fullmatch(binding or "")
        if match is None:
            raise KnowledgeCitationIntegrityError("????????????")
        encoded = match.group("session")
        expires_hex = match.group("expires")
        try:
            expires_at = int(expires_hex, 16)
        except ValueError:
            raise KnowledgeCitationIntegrityError("session capability expiry is invalid")
        if expires_hex != format(expires_at, "x") or time.time() >= expires_at:
            raise KnowledgeCitationIntegrityError("session capability has expired")
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
            if not 1 <= len(raw) <= 512:
                raise ValueError("session size")
            session_id = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise KnowledgeCitationIntegrityError("????????????")
        canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if canonical != encoded or session_id != str(row["session_id"] or ""):
            raise KnowledgeCitationIntegrityError("???????????")
        expected = hmac.new(
            self._citation_capability_secret,
            self._session_binding_message(row, encoded, expires_hex),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(match.group("signature"), expected):
            raise KnowledgeCitationIntegrityError("????????????")
        return session_id

    def _verified_citation(
        self,
        row,
        session_id: Optional[str] = None,
        session_binding_override: Optional[str] = None,
    ) -> Optional[KnowledgeCitation]:
        content = str(row["content"])
        encoded = content.encode("utf-8")
        section_byte_start = int(row["section_byte_start"])
        section_byte_end = int(row["section_byte_end"])
        byte_start = int(row["byte_start"])
        byte_end = int(row["byte_end"])
        if (
            section_byte_start < 0
            or section_byte_end <= section_byte_start
            or section_byte_end > len(encoded)
            or byte_start < section_byte_start
            or byte_end <= byte_start
            or byte_end > section_byte_end
        ):
            return None
        try:
            section_content = encoded[
                section_byte_start:section_byte_end
            ].decode("utf-8")
            quote = encoded[byte_start:byte_end].decode("utf-8")
        except UnicodeDecodeError:
            return None
        stored_quote = str(row["quote"])
        content_hash = _sha256(content)
        quote_hash = _sha256(quote)
        if (
            _sha256(section_content) != str(row["section_content_hash"])
            or quote != stored_quote
            or content_hash != str(row["content_hash"])
            or quote_hash != str(row["quote_hash"])
        ):
            return None
        document_id = str(row["document_id"])
        document_version = int(row["document_version"])
        section_id = str(row["section_id"])
        evidence_id = str(row["evidence_id"])
        source_ref = str(row["source_ref"])
        source_ref_hash = _sha256(source_ref)
        citation = KnowledgeCitation(
            uri="",
            citation_version=3,
            document_id=document_id,
            document_version=document_version,
            section_id=section_id,
            evidence_id=evidence_id,
            source_ref=source_ref,
            source_ref_hash=source_ref_hash,
            byte_start=byte_start,
            byte_end=byte_end,
            content_hash=content_hash,
            quote_hash=quote_hash,
            quote=quote,
            scope=str(row["scope"]),
            session_id=(str(row["session_id"]) if row["session_id"] else None),
        )
        session_binding = (
            session_binding_override
            if session_binding_override is not None
            else self._session_binding(row, session_id)
        )
        return KnowledgeCitation(
            uri=_knowledge_citation_uri(
                citation, session_binding=session_binding
            ),
            citation_version=citation.citation_version,
            document_id=citation.document_id,
            document_version=citation.document_version,
            section_id=citation.section_id,
            evidence_id=citation.evidence_id,
            source_ref=citation.source_ref,
            source_ref_hash=citation.source_ref_hash,
            byte_start=citation.byte_start,
            byte_end=citation.byte_end,
            content_hash=citation.content_hash,
            quote_hash=citation.quote_hash,
            quote=citation.quote,
            scope=citation.scope,
            session_id=citation.session_id,
        )

    def _validate_write(
        self, identity: IdentityContext, command: KnowledgeWriteCommand
    ) -> Tuple[Optional[str], Optional[str]]:
        try:
            encoded_size = len(command.content.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise KnowledgeValidationError("content 不是有效的 UTF-8 文本") from error
        if encoded_size > 10 * 1024 * 1024:
            raise KnowledgeValidationError("content 超过 10 MiB 限制")
        reserved = _RESERVED_METADATA_KEYS.intersection(command.metadata)
        if reserved:
            raise KnowledgeValidationError(
                "metadata 包含保留字段: %s" % ", ".join(sorted(reserved))
            )
        try:
            json.dumps(command.metadata, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise KnowledgeValidationError("metadata 必须可以序列化为 JSON") from error
        if command.sensitivity is Sensitivity.RESTRICTED and not identity.has_any_role(
            "admin", "knowledge:write_restricted"
        ):
            raise KnowledgeAuthorizationError("无权写入受限知识")
        if command.scope is MemoryScope.SHARED:
            if not identity.has_any_role("admin", "knowledge:write_shared"):
                raise KnowledgeAuthorizationError("无权写入共享知识")
            if command.owner_user_id is not None or command.session_id is not None:
                raise KnowledgeValidationError("共享知识不能绑定用户或会话")
            return None, None
        owner_user_id = command.owner_user_id or identity.actor_user_id
        if owner_user_id != identity.actor_user_id and not identity.has_any_role(
            "admin", "knowledge:manage"
        ):
            raise KnowledgeAuthorizationError("无权为其他用户写入知识")
        if command.scope is MemoryScope.USER:
            if command.session_id is not None:
                raise KnowledgeValidationError("用户知识不能绑定会话")
            return owner_user_id, None
        if not command.session_id or not command.session_id.strip():
            raise KnowledgeValidationError("会话知识必须绑定 session_id")
        return owner_user_id, command.session_id.strip()

    def _assert_identity(self, identity: IdentityContext) -> None:
        if identity.tenant_id != self.tenant_id:
            raise KnowledgeAuthorizationError("租户边界不匹配")

    @staticmethod
    def _assert_lifecycle_input(
        document_id: str, idempotency_key: str, reason: str
    ) -> None:
        for field_name, value in (
            ("document_id", document_id),
            ("idempotency_key", idempotency_key),
            ("reason", reason),
        ):
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeValidationError("%s 不能为空" % field_name)

    @staticmethod
    def _assert_can_manage(
        identity: IdentityContext, record: KnowledgeDocumentRecord
    ) -> None:
        if record.tenant_id != identity.tenant_id:
            raise KnowledgeAuthorizationError("租户边界不匹配")
        if identity.has_any_role("admin", "knowledge:manage"):
            return
        if record.owner_user_id == identity.actor_user_id:
            return
        if record.scope is MemoryScope.SHARED and identity.has_any_role(
            "knowledge:write_shared"
        ):
            return
        raise KnowledgeAuthorizationError("无权管理该知识文档")

    @staticmethod
    def _assert_can_read(
        identity: IdentityContext,
        record: KnowledgeDocumentRecord,
        session_id: Optional[str],
    ) -> None:
        if record.tenant_id != identity.tenant_id:
            raise KnowledgeAuthorizationError("租户边界不匹配")
        if record.sensitivity is Sensitivity.RESTRICTED and not identity.has_any_role(
            "admin", "knowledge:read_restricted"
        ):
            raise KnowledgeAuthorizationError("无权读取受限知识")
        if identity.has_any_role("admin", "knowledge:manage"):
            return
        if record.scope is MemoryScope.SHARED:
            return
        if record.owner_user_id != identity.actor_user_id:
            raise KnowledgeAuthorizationError("无权读取其他用户的知识")
        if record.scope is MemoryScope.SESSION and record.session_id != session_id:
            raise KnowledgeAuthorizationError("会话边界不匹配")

    @staticmethod
    def _can_read_row(identity: IdentityContext, row, session_id: Optional[str]) -> bool:
        if str(row["sensitivity"]) == Sensitivity.RESTRICTED.value and not identity.has_any_role(
            "admin", "knowledge:read_restricted"
        ):
            return False
        if identity.has_any_role("admin", "knowledge:manage"):
            return True
        scope = MemoryScope(str(row["scope"]))
        if scope is MemoryScope.SHARED:
            return True
        if row["owner_user_id"] != identity.actor_user_id:
            return False
        return scope is not MemoryScope.SESSION or row["session_id"] == session_id


def _read_secret_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KnowledgeValidationError("????????????")
        chunks = []
        remaining = _CITATION_SECRET_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        secret = b"".join(chunks)
    finally:
        os.close(fd)
    if len(secret) != _CITATION_SECRET_BYTES:
        raise KnowledgeValidationError("??????????")
    return secret


def _read_secret_when_ready(path: Path, timeout_seconds: float = 2.0) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while True:
        try:
            return _read_secret_file(path)
        except (FileNotFoundError, KnowledgeValidationError) as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error
            time.sleep(0.005)


def _load_or_create_secret(path: Path) -> bytes:
    try:
        return _read_secret_file(path)
    except FileNotFoundError:
        pass
    except KnowledgeValidationError:
        # Another process may have won O_EXCL but not finished its fsync yet.
        return _read_secret_when_ready(path)

    secret = secrets.token_bytes(_CITATION_SECRET_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:
        return _read_secret_when_ready(path)

    created = True
    try:
        view = memoryview(secret)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while creating citation secret")
            view = view[written:]
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        finally:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
        raise
    else:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _knowledge_citation_uri(
    citation: KnowledgeCitation, session_binding: Optional[str] = None
) -> str:
    """重建不可降级且绑定来源的 v3 规范知识引用。"""

    if citation.citation_version != 3:
        raise KnowledgeCitationVersionError("知识引用协议版本必须是 3")
    uri = "knowledge://%s/v/%s/sections/%s/evidence/%s#bytes=%s-%s" % (
        citation.document_id,
        citation.document_version,
        citation.section_id,
        citation.evidence_id,
        citation.byte_start,
        citation.byte_end,
    )
    result = (
        "%s&content_hash=%s&quote_hash=%s&source_ref_hash=%s"
    ) % (
        uri,
        citation.content_hash,
        citation.quote_hash,
        citation.source_ref_hash,
    )
    if session_binding is not None:
        result += "&session_binding=%s" % session_binding
    return result + "&citation_version=3"


def _first_heading(content: str) -> Optional[str]:
    for line in content.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return None

