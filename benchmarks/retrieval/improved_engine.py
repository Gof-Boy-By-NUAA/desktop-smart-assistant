"""租户化中文词法检索评测适配器。"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Dict, Sequence

from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.retrieval import IndexedDocument, TenantAwareLexicalIndex

from .dataset import RetrievalDocument


_IMPLEMENTATION_PATHS = (
    "benchmarks/retrieval/improved_engine.py",
    "agent/retrieval/lexical.py",
    "agent/memory/governance/contracts.py",
)


def implementation_fingerprint() -> str:
    """绑定评测适配器、词法索引和治理数据契约。"""

    repository_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative_path in _IMPLEMENTATION_PATHS:
        path = repository_root / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class ImprovedLexicalEngine:
    """把同一评测语料接入新的租户化词法索引。"""

    engine_id = "tenant-aware-lexical-trigram-v1"

    def __init__(self, candidate_limit: int = 40):
        self._temporary_dir = tempfile.TemporaryDirectory()
        self._index = TenantAwareLexicalIndex(
            Path(self._temporary_dir.name) / "improved-lexical.db",
            candidate_limit=candidate_limit,
        )
        self._candidate_limit = candidate_limit
        self._identity = IdentityContext(
            tenant_id="benchmark-tenant",
            actor_user_id="benchmark-user",
            roles=frozenset(),
            trace_id="trace-cmrc2018-benchmark",
            auth_source="benchmark-runner",
        )

    @property
    def capabilities(self) -> Dict[str, object]:
        """返回评测引擎能力。"""

        return {
            "fts5_available": True,
            "trigram_fts5_available": True,
            "vector_enabled": False,
            "tenant_filtering": True,
            "scope_filtering": True,
            "sensitivity_filtering": True,
            "candidate_limit": self._candidate_limit,
        }

    @property
    def implementation_sha256(self) -> str:
        """返回改进引擎及其生产依赖的联合指纹。"""

        return implementation_fingerprint()

    def index(self, documents: Sequence[RetrievalDocument]) -> None:
        """把公开语料作为同一租户内的共享知识索引。"""

        self._index.index_documents(
            [
                IndexedDocument(
                    tenant_id=self._identity.tenant_id,
                    document_id=document.document_id,
                    scope=MemoryScope.SHARED,
                    title=document.title,
                    text=document.text,
                    source_ref="cmrc2018:%s" % document.document_id,
                    sensitivity=Sensitivity.PUBLIC,
                )
                for document in documents
            ]
        )

    def search(self, query: str, limit: int = 10) -> Sequence[str]:
        """执行权限化搜索并返回文档标识。"""

        return [
            result.document_id
            for result in self._index.search(self._identity, query, limit=limit)
        ]

    def close(self) -> None:
        """释放索引和临时目录。"""

        self._index.close()
        self._temporary_dir.cleanup()
