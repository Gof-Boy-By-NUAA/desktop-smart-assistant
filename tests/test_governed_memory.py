"""受治理记忆核心的行为测试。"""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.memory.governance import (
    AuthorizationError,
    GovernedMemoryRepository,
    GovernedMemoryService,
    IdentityContext,
    IdempotencyConflictError,
    MemoryNotFoundError,
    MemoryScope,
    MemoryStatus,
    MemoryWriteCommand,
    Sensitivity,
    ValidationError,
)


class GovernedMemoryServiceTest(unittest.TestCase):
    """验证身份边界、版本链、幂等和审计行为。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "governed-memory.db"
        self.repository = GovernedMemoryRepository(db_path)
        self.service = GovernedMemoryService(self.repository)
        self.alice = self._identity("tenant-a", "alice")
        self.bob = self._identity("tenant-a", "bob")
        self.admin = self._identity("tenant-a", "root", "admin")

    def tearDown(self):
        self.repository.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _identity(tenant_id, user_id, *roles):
        return IdentityContext(
            tenant_id=tenant_id,
            actor_user_id=user_id,
            roles=frozenset(roles),
            trace_id="trace-%s-%s" % (tenant_id, user_id),
            auth_source="test-authenticator",
        )

    @staticmethod
    def _command(
        content="用户偏好中文回答",
        idempotency_key="idem-1",
        memory_id=None,
        scope=MemoryScope.USER,
        owner_user_id=None,
        session_id=None,
        sensitivity=Sensitivity.PRIVATE,
        metadata=None,
    ):
        return MemoryWriteCommand(
            content=content,
            scope=scope,
            source_type="conversation",
            source_ref="session:test#message:1",
            idempotency_key=idempotency_key,
            memory_id=memory_id,
            owner_user_id=owner_user_id,
            session_id=session_id,
            sensitivity=sensitivity,
            metadata=metadata or {"evidence": "用户原话"},
        )

    def test_user_memory_is_owned_and_readable_by_actor(self):
        record = self.service.write(self.alice, self._command())

        self.assertEqual("tenant-a", record.tenant_id)
        self.assertEqual("alice", record.owner_user_id)
        self.assertEqual(MemoryStatus.ACTIVE, record.status)
        self.assertEqual(record, self.service.get(self.alice, record.memory_id))

    def test_identity_fields_cannot_be_injected_through_metadata(self):
        command = self._command(metadata={"tenant_id": "tenant-b"})

        with self.assertRaises(ValidationError):
            self.service.write(self.alice, command)

    def test_user_cannot_write_memory_for_another_user(self):
        command = self._command(owner_user_id="bob")

        with self.assertRaises(AuthorizationError):
            self.service.write(self.alice, command)

    def test_shared_memory_requires_explicit_role(self):
        command = self._command(scope=MemoryScope.SHARED)

        with self.assertRaises(AuthorizationError):
            self.service.write(self.alice, command)

        writer = self._identity("tenant-a", "editor", "memory:write_shared")
        record = self.service.write(
            writer,
            self._command(scope=MemoryScope.SHARED, idempotency_key="idem-shared"),
        )
        self.assertIsNone(record.owner_user_id)
        self.assertEqual(record, self.service.get(self.bob, record.memory_id))

    def test_tenant_boundary_returns_not_found(self):
        record = self.service.write(self.alice, self._command())
        other_tenant = self._identity("tenant-b", "alice")

        with self.assertRaises(MemoryNotFoundError):
            self.service.get(other_tenant, record.memory_id)

    def test_session_memory_requires_matching_session(self):
        record = self.service.write(
            self.alice,
            self._command(
                scope=MemoryScope.SESSION,
                session_id="session-1",
                idempotency_key="idem-session",
            ),
        )

        with self.assertRaises(AuthorizationError):
            self.service.get(self.alice, record.memory_id, session_id="session-2")
        self.assertEqual(
            record,
            self.service.get(self.alice, record.memory_id, session_id="session-1"),
        )

    def test_same_idempotency_request_returns_original_result(self):
        command = self._command()

        first = self.service.write(self.alice, command)
        second = self.service.write(self.alice, command)

        self.assertEqual(first, second)
        self.assertEqual(1, len(self.service.list_versions(self.alice, first.memory_id)))

    def test_reused_idempotency_key_with_different_payload_is_rejected(self):
        self.service.write(self.alice, self._command(content="第一条事实"))

        with self.assertRaises(IdempotencyConflictError):
            self.service.write(self.alice, self._command(content="第二条事实"))

    def test_concurrent_idempotent_writes_create_one_version(self):
        command = self._command(content="并发写入保持单版本")

        with ThreadPoolExecutor(max_workers=4) as executor:
            records = list(
                executor.map(
                    lambda _: self.service.write(self.alice, command),
                    range(8),
                )
            )

        self.assertEqual(1, len({record.memory_id for record in records}))
        self.assertEqual(
            1,
            len(self.service.list_versions(self.alice, records[0].memory_id)),
        )

    def test_update_creates_new_version_without_mutating_history(self):
        first = self.service.write(self.alice, self._command(content="旧偏好"))
        second = self.service.write(
            self.alice,
            self._command(
                content="新偏好",
                memory_id=first.memory_id,
                idempotency_key="idem-update",
            ),
        )

        versions = self.service.list_versions(self.alice, first.memory_id)
        self.assertEqual([1, 2], [item.version for item in versions])
        self.assertEqual(MemoryStatus.SUPERSEDED, versions[0].status)
        self.assertEqual("旧偏好", versions[0].content)
        self.assertEqual("新偏好", second.content)

    def test_revoke_hides_memory_and_rollback_restores_historical_content(self):
        first = self.service.write(self.alice, self._command(content="可恢复事实"))
        revoked = self.service.revoke(
            self.alice, first.memory_id, "idem-revoke", "用户要求删除"
        )

        self.assertEqual(MemoryStatus.REVOKED, revoked.status)
        with self.assertRaises(MemoryNotFoundError):
            self.service.get(self.alice, first.memory_id)

        restored = self.service.rollback(
            self.alice,
            first.memory_id,
            target_version=1,
            idempotency_key="idem-rollback",
            reason="用户确认恢复",
        )
        self.assertEqual(3, restored.version)
        self.assertEqual(MemoryStatus.ACTIVE, restored.status)
        self.assertEqual("可恢复事实", restored.content)

    def test_audit_is_committed_with_each_mutation(self):
        first = self.service.write(self.alice, self._command())
        self.service.write(
            self.alice,
            self._command(
                content="更新后的偏好",
                memory_id=first.memory_id,
                idempotency_key="idem-update",
            ),
        )
        self.service.revoke(
            self.alice, first.memory_id, "idem-revoke", "用户撤销"
        )

        events = self.service.list_audit(self.alice, first.memory_id)
        self.assertEqual(
            ["memory.created", "memory.updated", "memory.revoked"],
            [event.action for event in events],
        )
        self.assertTrue(all(event.trace_id == self.alice.trace_id for event in events))

    def test_restricted_memory_requires_explicit_write_and_read_roles(self):
        with self.assertRaises(AuthorizationError):
            self.service.write(
                self.alice,
                self._command(sensitivity=Sensitivity.RESTRICTED),
            )

        writer = self._identity("tenant-a", "security", "memory:write_restricted")
        record = self.service.write(
            writer,
            self._command(
                sensitivity=Sensitivity.RESTRICTED,
                idempotency_key="idem-restricted",
            ),
        )
        with self.assertRaises(AuthorizationError):
            self.service.get(writer, record.memory_id)
        with self.assertRaises(AuthorizationError):
            self.service.list_versions(writer, record.memory_id)

        owner_reader = self._identity(
            "tenant-a", "security", "memory:read_restricted"
        )
        self.assertEqual(record, self.service.get(owner_reader, record.memory_id))

        other_reader = self._identity(
            "tenant-a", "security-reader", "memory:read_restricted"
        )
        with self.assertRaises(AuthorizationError):
            self.service.get(other_reader, record.memory_id)
        self.assertEqual(record, self.service.get(self.admin, record.memory_id))


if __name__ == "__main__":
    unittest.main()
