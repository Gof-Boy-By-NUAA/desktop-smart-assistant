import os
from pathlib import Path

import pytest

from agent.knowledge import GovernedKnowledgeRuntime, KnowledgeWriteCommand
from agent.knowledge.contracts import KnowledgeAuthorizationError
from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.tools.knowledge import KnowledgeGetTool, KnowledgeSearchTool
from agent.tools.ls.ls import Ls
from agent.tools.memory.memory_get import MemoryGetTool
from agent.tools.read.read import Read
from agent.tools.search_files.search_files import SearchFiles


_CANARY = "knowledge-boundary-canary-7319"


class _MemoryConfig:
    tenant_id = "tenant-local"

    def __init__(self, workspace: Path):
        self._workspace = workspace

    def get_workspace(self) -> Path:
        return self._workspace


class _MemoryManager:
    def __init__(self, workspace: Path):
        self.config = _MemoryConfig(workspace)


def _identity() -> IdentityContext:
    return IdentityContext(
        tenant_id="tenant-local",
        actor_user_id="admin",
        roles=frozenset({"knowledge:write_shared"}),
        trace_id="trace-knowledge-boundary",
        auth_source="test",
    )


def _reader_identity() -> IdentityContext:
    return IdentityContext(
        tenant_id="tenant-local",
        actor_user_id="reader",
        roles=frozenset(),
        trace_id="trace-knowledge-reader",
        auth_source="test",
    )


def _write_shared_projection(runtime, identity):
    return runtime.write(
        identity,
        KnowledgeWriteCommand(
            content="# 共享知识\n%s" % _CANARY,
            title="共享知识",
            source_ref="knowledge/shared/page.md",
            collection_id="shared",
            idempotency_key="knowledge-boundary-write",
            projection_path="shared/page.md",
            scope=MemoryScope.SHARED,
            sensitivity=Sensitivity.INTERNAL,
        ),
    )


def _assert_knowledge_denied(result) -> None:
    assert result.status == "error"
    assert "knowledge_search" in str(result.result)
    assert "knowledge_get" in str(result.result)


def test_generic_tools_block_knowledge_but_dedicated_tools_remain_available(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path), migrate_legacy=False)
    writer_identity = _identity()
    reader_identity = _reader_identity()
    try:
        record = _write_shared_projection(runtime, writer_identity)
        projection = tmp_path / "knowledge/shared/page.md"
        assert projection.is_file()

        config = {"cwd": str(tmp_path)}
        memory_get = MemoryGetTool(_MemoryManager(tmp_path), identity=writer_identity)
        file_variants = (
            "knowledge/shared/page.md",
            "./knowledge/shared/../shared/page.md",
            str(projection),
        )
        for path in file_variants:
            _assert_knowledge_denied(Read(config).execute({"path": path}))
            _assert_knowledge_denied(memory_get.execute({"path": path}))
            _assert_knowledge_denied(
                SearchFiles(config).execute({"path": path, "pattern": _CANARY})
            )

        for path in ("knowledge", "./knowledge/shared/..", str(projection.parent)):
            _assert_knowledge_denied(Ls(config).execute({"path": path}))

        search_files = SearchFiles(config)
        search_files._pick_backend = lambda: search_files._backend_python
        broad_content = search_files.execute(
            {"path": ".", "pattern": _CANARY, "no_ignore": True}
        )
        broad_names = search_files.execute(
            {
                "path": ".",
                "pattern": "page.md",
                "target": "files",
                "no_ignore": True,
            }
        )
        assert broad_content.status == "success"
        assert broad_content.result["match_count"] == 0
        assert broad_names.status == "success"
        assert broad_names.result["files"] == []

        governed_search = KnowledgeSearchTool(runtime, reader_identity).execute(
            {"query": _CANARY, "limit": 5}
        )
        assert governed_search.status == "success"
        assert governed_search.result["result_count"] == 1
        citation = governed_search.result["results"][0]["citation"]["uri"]
        governed_get = KnowledgeGetTool(runtime, reader_identity).execute(
            {"uri": citation}
        )
        assert governed_get.status == "success"
        assert governed_get.result["document_id"] == record.document_id
        assert _CANARY in governed_get.result["content"]
    finally:
        runtime.close()


def test_restricted_citation_directly_held_by_reader_still_requires_permission(
    tmp_path,
):
    runtime = GovernedKnowledgeRuntime(str(tmp_path), migrate_legacy=False)
    privileged = IdentityContext(
        tenant_id="tenant-local",
        actor_user_id="privileged",
        roles=frozenset(
            {
                "knowledge:write_shared",
                "knowledge:write_restricted",
                "knowledge:read_restricted",
            }
        ),
        trace_id="trace-knowledge-restricted-writer",
        auth_source="test",
    )
    reader = _reader_identity()
    try:
        runtime.write(
            privileged,
            KnowledgeWriteCommand(
                content="# 受限知识\nrestricted-direct-hold-8426",
                title="受限知识",
                source_ref="knowledge/restricted/direct-hold.md",
                collection_id="restricted",
                idempotency_key="knowledge-restricted-direct-hold",
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.RESTRICTED,
            ),
        )
        citation = runtime.search(
            privileged, "restricted-direct-hold-8426", limit=5
        )[0].citation

        assert runtime.search(reader, "restricted-direct-hold-8426", limit=5) == []
        with pytest.raises(KnowledgeAuthorizationError):
            runtime.resolve_verified_citation(reader, citation.uri)
        denied = KnowledgeGetTool(runtime, reader).execute({"uri": citation.uri})
        assert denied.status == "error"
        assert "restricted-direct-hold-8426" not in str(denied.result)
    finally:
        runtime.close()


@pytest.mark.skipif(
    os.path.normcase("KNOWLEDGE") != os.path.normcase("knowledge"),
    reason="大小写别名只存在于大小写不敏感的文件系统",
)
def test_uppercase_knowledge_alias_is_denied(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path), migrate_legacy=False)
    identity = _identity()
    try:
        _write_shared_projection(runtime, identity)
        config = {"cwd": str(tmp_path)}
        uppercase_file = "KNOWLEDGE/SHARED/PAGE.MD"
        _assert_knowledge_denied(Read(config).execute({"path": uppercase_file}))
        _assert_knowledge_denied(
            MemoryGetTool(_MemoryManager(tmp_path), identity=identity).execute(
                {"path": uppercase_file}
            )
        )
        _assert_knowledge_denied(
            SearchFiles(config).execute({"path": "KNOWLEDGE", "pattern": _CANARY})
        )
        _assert_knowledge_denied(Ls(config).execute({"path": "KNOWLEDGE"}))
    finally:
        runtime.close()


def test_symlink_alias_to_knowledge_is_denied(tmp_path):
    runtime = GovernedKnowledgeRuntime(str(tmp_path), migrate_legacy=False)
    identity = _identity()
    try:
        _write_shared_projection(runtime, identity)
        alias = tmp_path / "knowledge-alias"
        try:
            alias.symlink_to(tmp_path / "knowledge", target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows 当前进程没有创建符号链接的权限")
            raise

        config = {"cwd": str(tmp_path)}
        alias_file = alias / "shared/page.md"
        _assert_knowledge_denied(Read(config).execute({"path": str(alias_file)}))
        _assert_knowledge_denied(
            MemoryGetTool(_MemoryManager(tmp_path), identity=identity).execute(
                {"path": str(alias_file)}
            )
        )
        _assert_knowledge_denied(
            SearchFiles(config).execute({"path": str(alias), "pattern": _CANARY})
        )
        _assert_knowledge_denied(Ls(config).execute({"path": str(alias)}))
    finally:
        runtime.close()


def test_knowledge_skill_only_routes_through_governed_tools():
    skill_path = Path(__file__).resolve().parents[1] / "skills/knowledge-wiki/SKILL.md"
    content = skill_path.read_text(encoding="utf-8")

    for tool_name in (
        "knowledge_search",
        "knowledge_get",
        "knowledge_write",
        "knowledge_rollback",
        "knowledge_revoke",
    ):
        assert tool_name in content
    assert "Read `knowledge/index.md`" not in content
    assert "with the `read` tool" not in content
