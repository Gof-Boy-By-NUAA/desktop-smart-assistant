# encoding:utf-8
"""验证初始化边界不会把会话标识误当成用户身份。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.memory.governance import IdentityContext
from bridge.agent_initializer import AgentInitializer


class AgentInitializerIdentityTest(unittest.TestCase):
    """覆盖单机稳定身份、会话隔离和可信身份透传。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.initializer = AgentInitializer(bridge=None, agent_bridge=None)
        self.managers = []

    def tearDown(self):
        for manager in reversed(self.managers):
            manager.close()
        self.temp_dir.cleanup()

    def _setup_tools(self, session_id, trusted_identity=None):
        with mock.patch.object(
            self.initializer,
            "_init_embedding_provider",
            return_value=None,
        ):
            manager, tools = self.initializer._setup_memory_system(
                self.temp_dir.name,
                session_id=session_id,
                trusted_identity=trusted_identity,
            )
        self.assertIsNotNone(manager)
        self.assertTrue(tools)
        self.managers.append(manager)
        return manager, {tool.name: tool for tool in tools}

    def _close_manager(self, manager):
        manager.close()
        self.managers.remove(manager)

    def test_local_user_scope_crosses_sessions_while_session_scope_is_isolated(self):
        manager_a, tools_a = self._setup_tools("session-a")
        identity_a = tools_a["memory_write"].identity

        self.assertEqual("local-user", identity_a.actor_user_id)
        self.assertEqual("smart-assistant-local-single-user", identity_a.auth_source)
        self.assertEqual("local-user", tools_a["memory_search"].user_id)
        self.assertEqual("session-a", tools_a["memory_search"].session_id)

        user_memory = tools_a["memory_write"].execute(
            {
                "content": "USER_MEMORY_ALPHA_7841 属于本机用户。",
                "scope": "user",
                "idempotency_key": "identity-user-memory",
            }
        )
        session_memory = tools_a["memory_write"].execute(
            {
                "content": "SESSION_MEMORY_ALPHA_7841 只属于会话 A。",
                "scope": "session",
                "idempotency_key": "identity-session-memory",
            }
        )
        user_knowledge = tools_a["knowledge_write"].execute(
            {
                "path": "identity/user-note.md",
                "content": "USER_KNOWLEDGE_ALPHA_7841 属于本机用户。",
                "scope": "user",
                "idempotency_key": "identity-user-knowledge",
            }
        )
        session_knowledge = tools_a["knowledge_write"].execute(
            {
                "path": "identity/session-note.md",
                "content": "SESSION_KNOWLEDGE_ALPHA_7841 只属于会话 A。",
                "scope": "session",
                "idempotency_key": "identity-session-knowledge",
            }
        )

        for result in (
            user_memory,
            session_memory,
            user_knowledge,
            session_knowledge,
        ):
            self.assertEqual("success", result.status, result.result)

        self._close_manager(manager_a)
        _, tools_b = self._setup_tools("session-b")
        identity_b = tools_b["memory_write"].identity

        self.assertEqual("local-user", identity_b.actor_user_id)
        self.assertNotEqual(identity_a.trace_id, identity_b.trace_id)
        self.assertEqual("session-b", tools_b["memory_get"].session_id)
        self.assertEqual("session-b", tools_b["knowledge_get"].session_id)

        user_memory_get = tools_b["memory_get"].execute(
            {"memory_id": user_memory.result["memory_id"]}
        )
        session_memory_get = tools_b["memory_get"].execute(
            {"memory_id": session_memory.result["memory_id"]}
        )
        user_knowledge_get = tools_b["knowledge_get"].execute(
            {"document_id": user_knowledge.result["document_id"]}
        )
        session_knowledge_get = tools_b["knowledge_get"].execute(
            {"document_id": session_knowledge.result["document_id"]}
        )

        self.assertEqual("success", user_memory_get.status, user_memory_get.result)
        self.assertIn("USER_MEMORY_ALPHA_7841", user_memory_get.result)
        self.assertEqual("error", session_memory_get.status)
        self.assertEqual("success", user_knowledge_get.status, user_knowledge_get.result)
        self.assertIn("USER_KNOWLEDGE_ALPHA_7841", user_knowledge_get.result["content"])
        self.assertEqual("error", session_knowledge_get.status)

    def test_trusted_identity_is_used_without_deriving_it_from_session(self):
        trusted_identity = IdentityContext(
            tenant_id="tenant-local",
            actor_user_id="authenticated-user",
            roles=frozenset({"knowledge:read_restricted"}),
            trace_id="trusted-trace",
            auth_source="test-authenticator",
        )

        _, tools = self._setup_tools(
            "unrelated-session-id",
            trusted_identity=trusted_identity,
        )

        self.assertIs(trusted_identity, tools["memory_write"].identity)
        self.assertIs(trusted_identity, tools["knowledge_write"].identity)
        self.assertEqual("authenticated-user", tools["memory_search"].user_id)
        self.assertEqual("unrelated-session-id", tools["memory_search"].session_id)
        self.assertEqual(
            frozenset({"knowledge:read_restricted"}),
            trusted_identity.roles,
        )

    def test_local_identity_has_read_only_skill_governance_role(self):
        manager, tools = self._setup_tools("local-skill-reader")
        identity = manager.identity_context

        self.assertIs(identity, tools["memory_write"].identity)
        self.assertIn("skill:read", identity.roles)
        self.assertFalse(
            identity.roles.intersection(
                {"skill:propose", "skill:validate", "skill:publish"}
            )
        )

    def test_nondefault_trusted_tenant_binds_all_memory_runtimes(self):
        trusted_identity = IdentityContext(
            tenant_id="tenant-external",
            actor_user_id="authenticated-user",
            roles=frozenset(),
            trace_id="trusted-external-trace",
            auth_source="test-authenticator",
        )

        manager, tools = self._setup_tools(
            "external-session",
            trusted_identity=trusted_identity,
        )

        self.assertEqual("tenant-external", manager.config.tenant_id)
        self.assertEqual(
            "tenant-external", manager.knowledge_runtime.tenant_id
        )
        self.assertIs(trusted_identity, manager.identity_context)
        self.assertIs(trusted_identity, tools["memory_write"].identity)

    def test_invalid_trusted_identity_fails_before_workspace_side_effects(self):
        workspace = Path(self.temp_dir.name) / "invalid-identity"

        with self.assertRaisesRegex(TypeError, "trusted_identity"):
            self.initializer._setup_memory_system(
                str(workspace),
                session_id="invalid-session",
                trusted_identity="untrusted-request-value",
            )

        self.assertFalse(workspace.exists())

    def test_skill_manager_receives_the_trusted_memory_tenant(self):
        with mock.patch("agent.skills.SkillManager") as manager_type:
            manager = self.initializer._initialize_skill_manager(
                self.temp_dir.name,
                session_id="unrelated-session",
                tenant_id="trusted-tenant",
            )

        self.assertIs(manager, manager_type.return_value)
        manager_type.assert_called_once_with(
            custom_dir=str(Path(self.temp_dir.name) / "skills"),
            tenant_id="trusted-tenant",
            identity_context=None,
        )


if __name__ == "__main__":
    unittest.main()
