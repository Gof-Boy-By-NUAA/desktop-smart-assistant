"""在 CMRC 2018 官方开发集上评测知识检索与引用。"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from agent.knowledge import GovernedKnowledgeRuntime, KnowledgeWriteCommand
from agent.knowledge.contracts import (
    KnowledgeAuthorizationError,
    KnowledgeError,
    KnowledgeValidationError,
)
from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from benchmarks.retrieval.smart_assistant_baseline import SmartAssistantKeywordBaseline
from benchmarks.retrieval.dataset import (
    RetrievalDocument,
    RetrievalQuery,
    ensure_cmrc2018_dev,
    load_cmrc2018_dev,
    load_source_manifest,
)
from benchmarks.retrieval.evaluate import _query_selection_hash, _select_queries
from benchmarks.retrieval.metrics import QueryEvaluation, calculate_metrics


_IMPLEMENTATION_PATHS = (
    Path("agent/knowledge/contracts.py"),
    Path("agent/knowledge/parser.py"),
    Path("agent/knowledge/repository.py"),
    Path("agent/knowledge/runtime.py"),
    Path("agent/retrieval/lexical.py"),
    Path("agent/tools/knowledge/knowledge_tools.py"),
)


@dataclass(frozen=True)
class KnowledgeBenchmarkHit:
    """统一旧路径和新运行时的评测结果。"""

    document_id: str
    citation: Optional[Dict[str, object]]
    citation_resolution_valid: bool = False
    citation_source_binding_valid: bool = False


class LegacyKnowledgeEngine:
    """不改变实现地调用 SmartAssistant 原始知识索引路径。"""

    engine_id = "smart-assistant-legacy-knowledge"

    def __init__(self):
        self._engine = SmartAssistantKeywordBaseline()

    @property
    def capabilities(self):
        return {
            **self._engine.capabilities,
            "immutable_versions": False,
            "verified_citations": False,
            "fact_source_recheck": False,
        }

    @property
    def implementation_sha256(self) -> str:
        """返回冻结原版基线适配器与算法的联合指纹。"""

        return self._engine.implementation_sha256

    @property
    def implementation_paths(self) -> Sequence[str]:
        """列出原版适配器指纹覆盖的仓库相对路径。"""

        return self._engine.implementation_paths

    def index(self, documents: Sequence[RetrievalDocument]) -> None:
        self._engine.index(documents)

    def search(self, query: str, limit: int = 10) -> Sequence[KnowledgeBenchmarkHit]:
        return [
            KnowledgeBenchmarkHit(document_id=document_id, citation=None)
            for document_id in self._engine.search(query, limit=limit)
        ]

    def validate_index(
        self, documents: Sequence[RetrievalDocument]
    ) -> Dict[str, object]:
        """在计时后核验事实行、SQLite 和确定性查询探针。"""

        expected_ids = {document.document_id for document in documents}
        rows = self._engine._storage.conn.execute(
            "SELECT id FROM chunks ORDER BY id"
        ).fetchall()
        actual_ids = {str(row[0]) for row in rows}
        integrity = self._engine._storage.conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()
        probes = _validation_probe_documents(documents)
        queryable = all(
            document.document_id
            in self._engine.search(_validation_probe_query(document), limit=10)
            for document in probes
        )
        return {
            "expected_document_count": len(expected_ids),
            "active_document_count": len(actual_ids),
            "document_ids_match": actual_ids == expected_ids,
            "sqlite_integrity_ok": bool(integrity and integrity[0] == "ok"),
            "index_matches": actual_ids == expected_ids,
            "pending_derivative_count": 0,
            "query_probe_count": len(probes),
            "query_probes_passed": queryable,
        }

    def close(self) -> None:
        self._engine.close()


class GovernedKnowledgeEngine:
    """通过产品代码写入、检索并读取一等引用。"""

    engine_id = "smart-assistant-governed-knowledge-v1"

    def __init__(self):
        self._temporary_dir = tempfile.TemporaryDirectory()
        self._runtime = GovernedKnowledgeRuntime(
            self._temporary_dir.name,
            tenant_id="benchmark-tenant",
            migrate_legacy=False,
        )
        self._identity = IdentityContext(
            tenant_id="benchmark-tenant",
            actor_user_id="benchmark-user",
            roles=frozenset({"admin", "knowledge:write_shared"}),
            trace_id="trace-cmrc2018-knowledge",
            auth_source="benchmark-runner",
        )

    @property
    def capabilities(self):
        return {
            "fts5_trigram": True,
            "immutable_versions": True,
            "verified_citations": True,
            "fact_source_recheck": True,
            "tenant_user_collection_filtering": True,
            "citation_protocol_v3": True,
            "citation_protocol_version": 3,
            "source_ref_hash_binding": True,
            "citation_resolution_recheck": True,
        }

    @property
    def implementation_sha256(self) -> str:
        """返回当前受治理知识产品路径的实现指纹。"""

        return implementation_fingerprint()

    @property
    def implementation_paths(self) -> Sequence[str]:
        """列出治理 Knowledge 实现指纹覆盖的仓库相对路径。"""

        return implementation_paths()

    def index(self, documents: Sequence[RetrievalDocument]) -> None:
        self._runtime.write_batch(
            self._identity,
            [
                KnowledgeWriteCommand(
                    content=document.text,
                    title=document.title or document.document_id,
                    source_ref="cmrc2018:%s" % document.document_id,
                    collection_id="cmrc2018-dev",
                    idempotency_key="cmrc2018-%s" % document.document_id,
                    scope=MemoryScope.SHARED,
                    sensitivity=Sensitivity.PUBLIC,
                    metadata={"dataset": "cmrc2018_dev"},
                )
                for document in documents
            ],
            sync_derivatives=True,
        )

    def search(self, query: str, limit: int = 10) -> Sequence[KnowledgeBenchmarkHit]:
        results = self._runtime.search(
            self._identity,
            query,
            limit=limit,
            collection_ids=["cmrc2018-dev"],
        )
        hits = []
        for result in results:
            citation = result.citation
            try:
                resolved = self._runtime.resolve_verified_citation(
                    self._identity, citation.uri
                )
            except KnowledgeError:
                resolved = None
            resolution_valid = resolved == citation
            source_binding_valid = False
            if resolved is not None:
                expected_source_ref_hash = hashlib.sha256(
                    resolved.source_ref.encode("utf-8")
                ).hexdigest()
                source_binding_valid = (
                    resolved.source_ref_hash == expected_source_ref_hash
                    and citation.source_ref_hash == expected_source_ref_hash
                )
            source_prefix, separator, source_document_id = citation.source_ref.partition(
                ":"
            )
            document_id = (
                source_document_id
                if separator and source_prefix == "cmrc2018"
                else citation.document_id
            )
            hits.append(
                KnowledgeBenchmarkHit(
                    document_id=document_id,
                    citation={
                        "uri": citation.uri,
                        "document_id": citation.document_id,
                        "document_version": citation.document_version,
                        "section_id": citation.section_id,
                        "evidence_id": citation.evidence_id,
                        "source_ref": citation.source_ref,
                        "source_ref_hash": citation.source_ref_hash,
                        "citation_version": citation.citation_version,
                        "byte_start": citation.byte_start,
                        "byte_end": citation.byte_end,
                        "content_hash": citation.content_hash,
                        "quote_hash": citation.quote_hash,
                        "quote": citation.quote,
                    },
                    citation_resolution_valid=resolution_valid,
                    citation_source_binding_valid=source_binding_valid,
                )
            )
        return hits

    def validate_index(
        self, documents: Sequence[RetrievalDocument]
    ) -> Dict[str, object]:
        """在计时后核验事实、派生索引、任务收敛和查询可用性。"""

        records = self._runtime.repository.list_active_records(
            self._identity.tenant_id
        )
        expected_sources = {
            "cmrc2018:%s" % document.document_id for document in documents
        }
        actual_sources = {record.source_ref for record in records}
        evidence_rows = self._runtime.repository.list_evidence_for_active(
            self._identity.tenant_id
        )
        indexed_documents = [
            self._runtime._indexed_document(row) for row in evidence_rows
        ]
        index_matches = self._runtime.index.matches_tenant(
            self._identity.tenant_id, indexed_documents
        )
        with self._runtime.repository.transaction() as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            pending_jobs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM knowledge_derivative_jobs "
                    "WHERE tenant_id = ?",
                    (self._identity.tenant_id,),
                ).fetchone()[0]
            )
            pending_batches = int(
                conn.execute(
                    "SELECT COUNT(*) FROM knowledge_derivative_batches "
                    "WHERE tenant_id = ?",
                    (self._identity.tenant_id,),
                ).fetchone()[0]
            )
        probes = _validation_probe_documents(documents)
        queryable = True
        for document in probes:
            hits = self.search(_validation_probe_query(document), limit=10)
            if document.document_id not in {hit.document_id for hit in hits}:
                queryable = False
                break
        return {
            "expected_document_count": len(expected_sources),
            "active_document_count": len(records),
            "document_ids_match": actual_sources == expected_sources,
            "sqlite_integrity_ok": bool(integrity and integrity[0] == "ok"),
            "index_matches": bool(index_matches),
            "pending_derivative_count": pending_jobs + pending_batches,
            "query_probe_count": len(probes),
            "query_probes_passed": queryable,
        }

    def close(self) -> None:
        self._runtime.close()
        self._temporary_dir.cleanup()


def run_knowledge_engine(
    dataset_path: Path,
    engine,
    max_queries: Optional[int] = None,
) -> Dict[str, object]:
    """建立真实语料索引并计算检索、引用和延迟指标。"""

    try:
        dataset = load_cmrc2018_dev(dataset_path)
    except BaseException:
        engine.close()
        raise
    queries = _select_queries(dataset.queries, max_queries)
    answers = _load_answers(dataset_path)
    documents = {document.document_id: document for document in dataset.documents}
    evaluations = []
    query_latency_samples = []
    returned_hits = 0
    citation_hits = 0
    valid_citations = 0
    correct_targets = 0
    resolved_citations = 0
    source_bound_citations = 0
    answer_covered_queries = 0
    try:
        index_started = time.perf_counter()
        engine.index(dataset.documents)
        index_latency_ms = (time.perf_counter() - index_started) * 1000.0

        for query in queries:
            started = time.perf_counter()
            hits = engine.search(query.text, limit=10)
            latency_ms = (time.perf_counter() - started) * 1000.0
            evaluations.append(
                QueryEvaluation(
                    query=query,
                    ranked_document_ids=[hit.document_id for hit in hits],
                    latency_ms=latency_ms,
                )
            )
            query_latency_samples.append(
                {"query_id": query.query_id, "latency_ms": latency_ms}
            )
            returned_hits += len(hits)
            query_answer_covered = False
            for hit in hits:
                if hit.citation is None:
                    continue
                citation_hits += 1
                valid = _citation_is_valid(hit, documents)
                valid_citations += int(valid)
                resolved_citations += int(hit.citation_resolution_valid)
                source_bound_citations += int(hit.citation_source_binding_valid)
                correct_targets += int(
                    hit.citation["source_ref"] == "cmrc2018:%s" % hit.document_id
                )
                if (
                    valid
                    and hit.document_id in query.relevant_document_ids
                    and any(
                        answer in str(hit.citation["quote"])
                        for answer in answers.get(query.query_id, ())
                    )
                ):
                    query_answer_covered = True
            answer_covered_queries += int(query_answer_covered)

        metrics = calculate_metrics(evaluations)
        metrics.update(
            {
                "citation_coverage": citation_hits / float(returned_hits) if returned_hits else 0.0,
                "citation_location_accuracy": valid_citations / float(citation_hits) if citation_hits else 0.0,
                "citation_document_accuracy": correct_targets / float(citation_hits) if citation_hits else 0.0,
                "citation_resolution_accuracy": resolved_citations / float(citation_hits) if citation_hits else 0.0,
                "citation_source_binding_accuracy": source_bound_citations / float(citation_hits) if citation_hits else 0.0,
                "answer_span_citation_rate_at_10": answer_covered_queries / float(len(queries)),
                "returned_hit_count": returned_hits,
                "citation_hit_count": citation_hits,
                "citation_resolution_count": resolved_citations,
                "citation_source_binding_count": source_bound_citations,
            }
        )
        source = load_source_manifest()["cmrc2018_dev"]
        return {
            "schema_version": 3,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": {"id": engine.engine_id, "capabilities": engine.capabilities},
            "dataset": {
                "id": dataset.source_id,
                "sha256": dataset.source_sha256,
                "repository": source["repository"],
                "commit": source["commit"],
                "document_count": len(dataset.documents),
                "available_query_count": len(dataset.queries),
                "evaluated_query_count": len(queries),
                "query_selection_sha256": _query_selection_hash(queries),
                "real_data_ratio": 1.0,
            },
            "implementation_sha256": engine.implementation_sha256,
            "implementation_paths": list(engine.implementation_paths),
            "environment": {
                "python": sys.version,
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
            },
            "index_latency_ms": index_latency_ms,
            "query_latency_samples_ms": [
                evaluation.latency_ms for evaluation in evaluations
            ],
            "query_latency_samples": query_latency_samples,
            "metrics": metrics,
        }
    finally:
        engine.close()


def run_knowledge_index_trial(
    dataset_path: Path,
    engine,
) -> Dict[str, object]:
    """在独立新库上只测从摄取开始到索引可查询的墙钟时间。"""

    try:
        dataset = load_cmrc2018_dev(dataset_path)
    except BaseException:
        engine.close()
        raise
    try:
        started = time.perf_counter_ns()
        engine.index(dataset.documents)
        latency_ns = time.perf_counter_ns() - started
        validation = engine.validate_index(dataset.documents)
        return {
            "schema_version": 1,
            "engine_id": engine.engine_id,
            "implementation_sha256": engine.implementation_sha256,
            "implementation_paths": list(engine.implementation_paths),
            "dataset_id": dataset.source_id,
            "dataset_sha256": dataset.source_sha256,
            "document_count": len(dataset.documents),
            "latency_ns": latency_ns,
            "latency_ms": latency_ns / 1_000_000.0,
            "validation": validation,
        }
    finally:
        engine.close()


def run_real_data_security_checks(
    dataset_path: Path,
    sample_document_indices: Optional[Sequence[int]] = None,
    context_index: int = 0,
) -> Dict[str, object]:
    """使用官方上下文验证隔离、撤销和引用来源绑定。"""

    dataset = load_cmrc2018_dev(dataset_path)
    indices = tuple(sample_document_indices or (0, 1, 2))
    if (
        len(indices) != 3
        or len(set(indices)) != 3
        or any(index < 0 or index >= len(dataset.documents) for index in indices)
    ):
        raise ValueError("安全探针必须提供三个不重复的有效文档索引")
    private_document, revoked_document, source_binding_document = (
        dataset.documents[index] for index in indices
    )
    private_probe = private_document.text[:24]
    revoked_probe = revoked_document.text[:24]
    source_binding_probe = source_binding_document.text[:24]
    temporary_dir = tempfile.TemporaryDirectory()
    tenant_id = "security-tenant-%02d" % context_index
    owner_user_id = "alice-%02d" % context_index
    other_user_id = "bob-%02d" % context_index
    session_id = "security-session-%02d" % context_index
    private_scope = (
        MemoryScope.USER,
        MemoryScope.SESSION,
        MemoryScope.SHARED,
    )[context_index % 3]
    private_sensitivity = (
        Sensitivity.PRIVATE,
        Sensitivity.INTERNAL,
        Sensitivity.RESTRICTED,
    )[context_index % 3]
    owner_roles = set()
    if private_scope is MemoryScope.SHARED:
        owner_roles.add("knowledge:write_shared")
    if private_sensitivity is Sensitivity.RESTRICTED:
        owner_roles.update(
            {"knowledge:write_restricted", "knowledge:read_restricted"}
        )
    runtime = GovernedKnowledgeRuntime(
        temporary_dir.name,
        tenant_id=tenant_id,
        migrate_legacy=False,
    )
    alice = IdentityContext(
        tenant_id=tenant_id,
        actor_user_id=owner_user_id,
        roles=frozenset(owner_roles),
        trace_id="security-alice-%02d" % context_index,
        auth_source="benchmark-runner",
    )
    bob = IdentityContext(
        tenant_id=tenant_id,
        actor_user_id=other_user_id,
        roles=frozenset(),
        trace_id="security-bob-%02d" % context_index,
        auth_source="benchmark-runner",
    )
    admin = IdentityContext(
        tenant_id=tenant_id,
        actor_user_id="admin-%02d" % context_index,
        roles=frozenset({"admin", "knowledge:write_shared"}),
        trace_id="security-admin-%02d" % context_index,
        auth_source="benchmark-runner",
    )
    try:
        runtime.write(
            alice,
            KnowledgeWriteCommand(
                content=private_document.text,
                title=private_document.title or private_document.document_id,
                source_ref="cmrc2018:%s" % private_document.document_id,
                collection_id="private-real-data-%02d" % context_index,
                idempotency_key="private-real-data-%02d" % context_index,
                scope=private_scope,
                session_id=(
                    session_id if private_scope is MemoryScope.SESSION else None
                ),
                sensitivity=private_sensitivity,
            ),
        )
        owner_hits = runtime.search(
            alice,
            private_probe,
            limit=5,
            session_id=(
                session_id if private_scope is MemoryScope.SESSION else None
            ),
        )
        leaked_hits = runtime.search(
            bob,
            private_probe,
            limit=5,
            session_id=(
                session_id if private_scope is MemoryScope.SESSION else None
            ),
        )
        cross_tenant_rejected = False
        try:
            runtime.search(
                IdentityContext(
                    tenant_id="other-tenant-%02d" % context_index,
                    actor_user_id=owner_user_id,
                    roles=frozenset(owner_roles),
                    trace_id="security-cross-tenant-%02d" % context_index,
                    auth_source="benchmark-runner",
                ),
                private_probe,
                limit=5,
            )
        except KnowledgeAuthorizationError:
            cross_tenant_rejected = True

        revocable = runtime.write(
            admin,
            KnowledgeWriteCommand(
                content=revoked_document.text,
                title=revoked_document.title or revoked_document.document_id,
                source_ref="cmrc2018:%s" % revoked_document.document_id,
                collection_id="revoked-real-data-%02d" % context_index,
                idempotency_key="revoked-real-data-%02d" % context_index,
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.PUBLIC,
            ),
        )
        before_revoke = runtime.search(admin, revoked_probe, limit=5)
        target_hits_before_revoke = _hits_for_document(
            before_revoke, revocable.document_id
        )
        original_delete = runtime.index.delete_document
        runtime.index.delete_document = lambda *_: False
        derivative_failure_observed = False
        try:
            runtime.revoke(
                admin,
                revocable.document_id,
                "revoke-real-data-%02d" % context_index,
                "官方来源撤销测试",
            )
        except RuntimeError:
            derivative_failure_observed = True
        finally:
            runtime.index.delete_document = original_delete
        polluted_hits = _hits_for_document(
            runtime.search(admin, revoked_probe, limit=5),
            revocable.document_id,
        )

        source_binding_record = runtime.write(
            admin,
            KnowledgeWriteCommand(
                content=source_binding_document.text,
                title=source_binding_document.title
                or source_binding_document.document_id,
                source_ref="cmrc2018:%s" % source_binding_document.document_id,
                collection_id="source-binding-real-data-%02d" % context_index,
                idempotency_key="source-binding-real-data-%02d" % context_index,
                scope=MemoryScope.SHARED,
                sensitivity=Sensitivity.PUBLIC,
            ),
        )
        source_binding_hits = runtime.search(
            admin,
            source_binding_probe,
            limit=5,
            collection_ids=["source-binding-real-data-%02d" % context_index],
        )
        source_binding_citation = (
            source_binding_hits[0].citation if source_binding_hits else None
        )
        source_ref_tamper_precondition_hit = False
        source_ref_tamper_was_injected = False
        source_ref_tamper_rejected = False
        source_ref_tamper_resolution_count = 0
        if source_binding_citation is not None:
            resolved_before_tamper = runtime.resolve_verified_citation(
                admin, source_binding_citation.uri
            )
            source_ref_tamper_precondition_hit = (
                resolved_before_tamper == source_binding_citation
            )
            with runtime.repository.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE knowledge_documents SET source_ref = ? "
                    "WHERE tenant_id = ? AND document_id = ? AND version = ?",
                    (
                        "tampered:%s" % source_binding_document.document_id,
                        source_binding_record.tenant_id,
                        source_binding_record.document_id,
                        source_binding_record.version,
                    ),
                )
                source_ref_tamper_was_injected = cursor.rowcount == 1
            try:
                runtime.resolve_verified_citation(admin, source_binding_citation.uri)
            except KnowledgeValidationError:
                source_ref_tamper_rejected = True
            else:
                source_ref_tamper_resolution_count = 1
        return {
            "dataset_id": dataset.source_id,
            "dataset_sha256": dataset.source_sha256,
            "sample_document_ids": [
                private_document.document_id,
                revoked_document.document_id,
                source_binding_document.document_id,
            ],
            "sample_document_indices": list(indices),
            "context_index": context_index,
            "tenant_id": tenant_id,
            "owner_user_id": owner_user_id,
            "other_user_id": other_user_id,
            "private_scope": private_scope.value,
            "private_sensitivity": private_sensitivity.value,
            "private_collection_id": "private-real-data-%02d" % context_index,
            "synthetic_content_used": False,
            "owner_precondition_hit": bool(owner_hits),
            "cross_tenant_rejected": cross_tenant_rejected,
            "revoke_precondition_hit": bool(target_hits_before_revoke),
            "unauthorized_result_count": len(leaked_hits),
            "permission_leakage_rate": len(leaked_hits) / float(max(1, len(owner_hits))),
            "revoked_result_count": len(polluted_hits),
            "revoked_pollution_rate": len(polluted_hits)
            / float(max(1, len(target_hits_before_revoke))),
            "stale_index_delete_was_injected": True,
            "derivative_failure_observed": derivative_failure_observed,
            "source_ref_tamper_precondition_hit": source_ref_tamper_precondition_hit,
            "source_ref_tamper_was_injected": source_ref_tamper_was_injected,
            "source_ref_tamper_rejected": source_ref_tamper_rejected,
            "source_ref_tamper_resolution_count": source_ref_tamper_resolution_count,
        }
    finally:
        runtime.close()
        temporary_dir.cleanup()


def _hits_for_document(hits: Sequence[object], document_id: str) -> Sequence[object]:
    """只统计目标文档，避免同一探针命中其他有效文档造成安全误报。"""

    def hit_document_id(hit: object) -> Optional[str]:
        direct = getattr(hit, "document_id", None)
        if direct is not None:
            return str(direct)
        citation = getattr(hit, "citation", None)
        bound = getattr(citation, "document_id", None)
        return str(bound) if bound is not None else None

    return tuple(hit for hit in hits if hit_document_id(hit) == document_id)

def _load_answers(dataset_path: Path) -> Dict[str, Sequence[str]]:
    payload = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    answers = {}
    for item in payload:
        for query in item["qas"]:
            answers[str(query["query_id"])] = tuple(
                answer for answer in query.get("answers", []) if isinstance(answer, str) and answer
            )
    return answers


def _citation_is_valid(
    hit: KnowledgeBenchmarkHit,
    documents: Dict[str, RetrievalDocument],
) -> bool:
    citation = hit.citation
    document = documents.get(hit.document_id)
    if citation is None or document is None:
        return False
    encoded = document.text.encode("utf-8")
    start = int(citation["byte_start"])
    end = int(citation["byte_end"])
    if start < 0 or end < start or end > len(encoded):
        return False
    try:
        quote = encoded[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return (
        quote == citation["quote"]
        and hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        == citation["content_hash"]
        and hashlib.sha256(quote.encode("utf-8")).hexdigest()
        == citation["quote_hash"]
    )


def _validation_probe_documents(
    documents: Sequence[RetrievalDocument],
) -> Sequence[RetrievalDocument]:
    """固定选择首、中、尾三个文档验证索引确实可查询。"""

    if not documents:
        return ()
    indices = sorted({0, len(documents) // 2, len(documents) - 1})
    return tuple(documents[index] for index in indices)


def _validation_probe_query(document: RetrievalDocument) -> str:
    """优先使用索引中显式保存的标题，避免正文截断破坏分词。"""

    title = document.title.strip()
    return title if title else document.text[:24]


def implementation_paths() -> Sequence[str]:
    """公开参与 Knowledge 产品指纹的仓库相对路径。"""

    return tuple(path.as_posix() for path in _IMPLEMENTATION_PATHS)


def implementation_fingerprint(
    repository_root: Optional[Path] = None,
) -> str:
    """只绑定仓库相对路径和文件字节，确保跨检出目录可复算。"""

    root = Path(repository_root or Path(__file__).resolve().parents[2])
    digest = hashlib.sha256()
    for relative_path in _IMPLEMENTATION_PATHS:
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative_path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _implementation_fingerprint(
    repository_root: Optional[Path] = None,
) -> str:
    """兼容旧调用方，统一委托给公开的可移植指纹函数。"""

    return implementation_fingerprint(repository_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行真实知识检索与引用评测")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--engine", choices=("legacy", "governed"), default="governed")
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset_path = args.dataset or ensure_cmrc2018_dev()
    engine = LegacyKnowledgeEngine() if args.engine == "legacy" else GovernedKnowledgeEngine()
    report = run_knowledge_engine(dataset_path, engine, args.max_queries)
    if args.engine == "governed":
        report["security"] = run_real_data_security_checks(dataset_path)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes((rendered + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


