"""有效技能的 active-only 词法影子检索运行时。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Optional, Sequence, Tuple

from agent.memory.governance import IdentityContext, MemoryScope, Sensitivity
from agent.retrieval.lexical import IndexedDocument, TenantAwareLexicalIndex
from agent.skills.governance import (
    GovernedSkillRepository,
    SkillAuthorizationError,
    SkillStatus,
    SkillVersion,
    can_read_governed_skills,
)
from agent.skills.locks import skill_root_lock
from common.path_safety import is_link_or_reparse_point

from .contracts import ShadowCandidate, ShadowRun
from .telemetry import ShadowTelemetryRepository


_COLLECTION_ID = "governed-skills-active"
_RETRIEVER_VERSION = "active-skill-lexical-shadow-v1"


class ActiveSkillShadowRuntime:
    """只观察检索结果，不修改提示词、消息、工具或技能状态。"""

    def __init__(
        self,
        governance_repository: GovernedSkillRepository,
        index_path: Path,
        telemetry_path: Path,
        tenant_id: str,
        candidate_limit: int = 40,
    ):
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id 不能为空")
        self.governance_repository = governance_repository
        self.tenant_id = tenant_id.strip()
        self.index_path = Path(index_path)
        self.telemetry_path = Path(telemetry_path)
        for path in (
            self.index_path.parent,
            self.index_path,
            self.telemetry_path,
        ):
            if is_link_or_reparse_point(path):
                raise ValueError("技能影子检索路径不能是符号链接或重解析点")
        self._lock = threading.RLock()
        self._governance_lock = skill_root_lock(self.index_path.parent.parent)
        self._closed = False
        with self._governance_lock:
            self.index = TenantAwareLexicalIndex(
                self.index_path, candidate_limit=candidate_limit
            )
            telemetry = None
            try:
                telemetry = ShadowTelemetryRepository(
                    self.telemetry_path,
                    self.telemetry_path.with_name("skill-shadow-hmac.key"),
                )
                telemetry.prune(30)
            except Exception:
                if telemetry is not None:
                    telemetry.close()
                self.index.close()
                raise
        self.telemetry = telemetry
        self._index_generation = ""

    @staticmethod
    def _record_text(record: SkillVersion) -> str:
        """按固定字段顺序生成检索正文，不包含来源和身份元数据。"""

        sections = [record.description]
        sections.extend(record.applicability)
        sections.extend(record.steps)
        sections.extend(record.validation_rules)
        sections.extend(record.contraindications)
        return "\n".join(sections)

    def _active_records(self) -> Tuple[SkillVersion, ...]:
        return tuple(
            self.governance_repository.list_active_versions(self.tenant_id)
        )

    @staticmethod
    def _generation(records: Sequence[SkillVersion]) -> str:
        payload = [
            {
                "skill_id": record.skill_id,
                "version": record.version,
                "content_hash": record.content_hash,
            }
            for record in records
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _documents(
        self, records: Sequence[SkillVersion]
    ) -> Tuple[IndexedDocument, ...]:
        return tuple(
            IndexedDocument(
                tenant_id=record.tenant_id,
                document_id=record.skill_id,
                scope=MemoryScope.SHARED,
                title="%s %s" % (record.name, record.description),
                text=self._record_text(record),
                source_ref="skill://%s/v/%d" % (record.skill_id, record.version),
                collection_id=_COLLECTION_ID,
                sensitivity=Sensitivity.INTERNAL,
                metadata={
                    "source": "governed-skill",
                    "skill_id": record.skill_id,
                    "version": record.version,
                    "content_hash": record.content_hash,
                    "model_compatibility": list(record.model_compatibility),
                },
            )
            for record in records
        )

    def rebuild_active_index(self) -> str:
        """从治理事实原子重建租户索引，并核验 FTS 派生面。"""

        with self._governance_lock:
            with self._lock:
                self._require_open()
                records = self._active_records()
                documents = self._documents(records)
                generation = self._generation(records)
                self.index.replace_tenant(self.tenant_id, documents)
                if not self.index.matches_tenant(self.tenant_id, documents):
                    raise RuntimeError("技能影子词法索引完整性核验失败")
                self._index_generation = generation
                return generation

    def _ensure_index(
        self,
    ) -> Tuple[Tuple[SkillVersion, ...], str]:
        records = self._active_records()
        generation = self._generation(records)
        documents = self._documents(records)
        index_matches = (
            generation == self._index_generation
            and self.index.matches_tenant(self.tenant_id, documents)
        )
        if not index_matches:
            self.index.replace_tenant(self.tenant_id, documents)
            if not self.index.matches_tenant(self.tenant_id, documents):
                raise RuntimeError("技能影子词法索引完整性核验失败")
            self._index_generation = generation
        return records, generation

    def start_run(
        self,
        identity: IdentityContext,
        initial_task: str,
        model_id: str,
        session_id: Optional[str],
        top_k: int = 5,
    ) -> ShadowRun:
        """瞬时检索任务，只把脱敏元数据写入遥测库。"""

        if not isinstance(identity, IdentityContext):
            raise TypeError("identity 必须是 IdentityContext")
        if identity.tenant_id != self.tenant_id:
            raise ValueError("影子检索身份租户不匹配")
        if not can_read_governed_skills(identity):
            raise SkillAuthorizationError("无权读取技能治理记录")
        if not isinstance(initial_task, str) or not initial_task.strip():
            raise ValueError("initial_task 不能为空")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id 不能为空")
        if top_k <= 0:
            raise ValueError("top_k 必须大于零")

        with self._governance_lock:
            with self._lock:
                self._require_open()
                started = time.perf_counter_ns()
                _records, generation = self._ensure_index()
                raw_results = self.index.search(
                    identity,
                    initial_task,
                    limit=top_k,
                    session_id=session_id,
                    collection_ids=(_COLLECTION_ID,),
                )
                candidates = []
                for result in raw_results:
                    metadata = result.metadata
                    try:
                        skill_id = str(metadata["skill_id"])
                        version = int(metadata["version"])
                        content_hash = str(metadata["content_hash"])
                        fact = self.governance_repository.read_version(
                            self.tenant_id, skill_id, version
                        )
                    except Exception:
                        continue
                    if fact.status is not SkillStatus.ACTIVE:
                        continue
                    if fact.content_hash != content_hash:
                        continue
                    candidates.append(
                        ShadowCandidate(
                            rank=len(candidates) + 1,
                            skill_id=skill_id,
                            version=version,
                            content_hash=content_hash,
                            score=float(result.score),
                            bm25_score=float(result.bm25_score),
                            query_coverage=float(result.query_coverage),
                            model_compatible=model_id.strip()
                            in fact.model_compatibility,
                        )
                    )
                candidates = self._revalidate_candidates(candidates)
                latency_ms = (time.perf_counter_ns() - started) / 1_000_000.0
                run_id = self.telemetry.create_run(
                    tenant_id=self.tenant_id,
                    task=initial_task,
                    actor_user_id=identity.actor_user_id,
                    session_id=session_id,
                    model_id=model_id.strip(),
                    retriever_version=_RETRIEVER_VERSION,
                    index_generation=generation,
                    top_k=top_k,
                    retrieval_latency_ms=latency_ms,
                    candidates=candidates,
                )
                return ShadowRun(run_id, generation, tuple(candidates))

    def _revalidate_candidates(
        self, candidates: Sequence[ShadowCandidate]
    ) -> list[ShadowCandidate]:
        """在写遥测前再次核验治理状态和内容哈希。"""

        verified = []
        for candidate in candidates:
            try:
                fact = self.governance_repository.read_version(
                    self.tenant_id, candidate.skill_id, candidate.version
                )
            except Exception:
                continue
            if (
                fact.status is SkillStatus.ACTIVE
                and fact.content_hash == candidate.content_hash
            ):
                verified.append(candidate)
        return verified

    def record_tool_use(
        self,
        run: ShadowRun,
        tool_call_id: str,
        tool_name: str,
        arguments: object,
    ) -> None:
        with self._lock:
            self._require_open()
            self.telemetry.record_tool_use(
                run.run_id, tool_call_id, tool_name, arguments
            )

    def record_injection(
        self,
        run: ShadowRun,
        status: str,
        candidates: Sequence[ShadowCandidate],
    ) -> None:
        """记录生产提示词实际采用的脱敏候选元数据。"""

        with self._lock:
            self._require_open()
            self.telemetry.record_injection(run.run_id, status, candidates)

    def record_tool_result(
        self,
        run: ShadowRun,
        tool_call_id: str,
        status: str,
        latency_ms: float,
        result: object,
    ) -> None:
        with self._lock:
            self._require_open()
            self.telemetry.record_tool_result(
                run.run_id, tool_call_id, status, latency_ms, result
            )

    def finish_run(
        self, run: ShadowRun, status: str, final_response: object
    ) -> None:
        with self._lock:
            self._require_open()
            self.telemetry.finish_run(run.run_id, status, final_response)

    def export_evidence(self, run_id: str) -> bytes:
        with self._lock:
            self._require_open()
            return self.telemetry.export_evidence(run_id)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("技能影子检索运行时已关闭")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self.index.close()
            finally:
                try:
                    self.telemetry.close()
                finally:
                    self._closed = True
