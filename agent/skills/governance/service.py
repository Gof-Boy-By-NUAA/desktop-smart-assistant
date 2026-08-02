"""外部工作手册技能的提案、验证、发布和回滚服务。"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from agent.skills.locks import skill_root_lock
from common.path_safety import is_link_or_reparse_point

from .contracts import (
    EvaluationPolicy,
    EvaluationRunner,
    EvaluationRunResult,
    PairedSampleResult,
    SkillAuditEvent,
    SkillAuthorizationError,
    SkillEvaluation,
    SkillEvaluationCommand,
    SkillIdentity,
    SkillNotFoundError,
    SkillProposal,
    SkillPublishGateError,
    SkillStatus,
    SkillTamperError,
    SkillValidationError,
    SkillVersion,
    SourceEvidence,
    can_read_governed_skills,
)
from .repository import (
    GovernedSkillRepository,
    canonical_json,
    sha256_bytes,
    utc_now,
)


_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GATE_SCHEMA_VERSION = 1
class GovernedSkillService:
    """在固定租户和固定技能目录内执行技能治理操作。"""

    def __init__(
        self,
        repository: GovernedSkillRepository,
        skills_dir: Path,
        tenant_id: str,
        evaluation_policy: EvaluationPolicy = EvaluationPolicy(),
        evaluation_runner: Optional[EvaluationRunner] = None,
    ):
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise SkillValidationError("tenant_id 不能为空")
        self.repository = repository
        requested_skills_dir = Path(os.path.abspath(os.fspath(skills_dir)))
        if self._is_link_or_reparse_point(requested_skills_dir):
            raise SkillValidationError("skills 目录不能是符号链接或重解析点")
        requested_skills_dir.mkdir(parents=True, exist_ok=True)
        if self._is_link_or_reparse_point(requested_skills_dir):
            raise SkillValidationError("skills 目录不能是符号链接或重解析点")
        self.skills_dir = requested_skills_dir.resolve()
        system_dir = self.skills_dir / ".system"
        if self._is_link_or_reparse_point(system_dir):
            raise SkillValidationError("skills/.system 不能是符号链接或重解析点")
        system_dir.mkdir(parents=True, exist_ok=True)
        repository_path = Path(repository.db_path).resolve(strict=False)
        try:
            repository_path.relative_to(system_dir)
        except ValueError as error:
            raise SkillValidationError("技能治理数据库必须位于 skills/.system") from error
        if self._is_link_or_reparse_point(Path(repository.db_path)):
            raise SkillValidationError("技能治理数据库不能是符号链接或重解析点")
        self.tenant_id = tenant_id.strip()
        self.evaluation_policy = evaluation_policy
        if evaluation_runner is not None and not isinstance(
            evaluation_runner, EvaluationRunner
        ):
            raise SkillValidationError("evaluation_runner 必须实现 EvaluationRunner")
        self.evaluation_runner = evaluation_runner
        self._projection_lock = skill_root_lock(self.skills_dir)
        tenant_hash = sha256_bytes(self.tenant_id.encode("utf-8"))[:16]
        self._projection_journal_path = (
            system_dir / ("projection-%s.journal.json" % tenant_hash)
        )
        with self._projection_lock:
            with self.repository.transaction() as conn:
                self._recover_projection_journal(conn)

    def propose(self, identity: SkillIdentity, proposal: SkillProposal) -> SkillVersion:
        """验证来源哈希并追加一个候选正文版本。"""

        self._require_role(identity, "skill:propose")
        normalized = self._validate_proposal(proposal)
        request_payload = {
            "name": normalized.name,
            "description": normalized.description,
            "applicability": normalized.applicability,
            "steps": normalized.steps,
            "validation_rules": normalized.validation_rules,
            "contraindications": normalized.contraindications,
            "model_compatibility": normalized.model_compatibility,
            "sources": [
                {
                    "source_type": source.source_type,
                    "source_ref": source.source_ref,
                    "sha256": source.sha256,
                }
                for source in normalized.sources
            ],
        }
        request_hash = self.repository.request_hash(request_payload)
        with self.repository.transaction() as conn:
            replay = self.repository.find_idempotent(
                conn,
                self.tenant_id,
                identity.actor_user_id,
                "propose",
                normalized.idempotency_key,
                request_hash,
            )
            if replay is not None:
                return self.repository.get_version(
                    conn, self.tenant_id, replay["skill_id"], int(replay["version"])
                )

            skill_id = self.repository.find_skill_id_by_name(
                conn, self.tenant_id, normalized.name
            ) or str(uuid.uuid4())
            version = self.repository.next_version(conn, self.tenant_id, skill_id)
            provenance = tuple(
                {
                    "source_type": source.source_type.strip(),
                    "source_ref": source.source_ref.strip(),
                    "sha256": source.sha256.lower(),
                }
                for source in normalized.sources
            )
            record = SkillVersion(
                skill_id=skill_id,
                tenant_id=self.tenant_id,
                name=normalized.name,
                version=version,
                status=SkillStatus.CANDIDATE,
                owner_user_id=identity.actor_user_id,
                description=normalized.description,
                applicability=normalized.applicability,
                steps=normalized.steps,
                validation_rules=normalized.validation_rules,
                contraindications=normalized.contraindications,
                model_compatibility=normalized.model_compatibility,
                provenance=provenance,
                content_hash="",
                created_by=identity.actor_user_id,
                trace_id=identity.trace_id,
                created_at=utc_now(),
            )
            record = replace(record, content_hash=self.repository.content_hash(record))
            self.repository.insert_version(conn, record)
            self.repository.append_state(
                conn,
                tenant_id=self.tenant_id,
                skill_id=skill_id,
                version=version,
                status=SkillStatus.CANDIDATE,
                evaluation_id=None,
                reason="候选提案已创建",
                actor_user_id=identity.actor_user_id,
                trace_id=identity.trace_id,
            )
            self.repository.append_audit(
                conn,
                tenant_id=self.tenant_id,
                skill_id=skill_id,
                version=version,
                actor_user_id=identity.actor_user_id,
                action="skill.proposed",
                details={
                    "auth_source": identity.auth_source,
                    "content_hash": record.content_hash,
                    "source_sha256": [item["sha256"] for item in provenance],
                },
                trace_id=identity.trace_id,
            )
            self.repository.save_idempotent(
                conn,
                tenant_id=self.tenant_id,
                actor_user_id=identity.actor_user_id,
                operation="propose",
                idempotency_key=normalized.idempotency_key,
                request_hash=request_hash,
                result_type="version",
                skill_id=skill_id,
                version=version,
            )
            return record

    def evaluate(
        self, identity: SkillIdentity, command: SkillEvaluationCommand
    ) -> SkillEvaluation:
        """计算固定门禁并追加真实套件的配对评测证据。"""

        self._require_role(identity, "skill:validate")
        runner = self._require_evaluation_runner()
        normalized, suite_hash = self._validate_evaluation_command(command)
        candidate_snapshot = self.repository.read_version(
            self.tenant_id, normalized.skill_id, normalized.version
        )
        if candidate_snapshot.status is not SkillStatus.CANDIDATE:
            raise SkillValidationError("只有 candidate 状态可以接受新评测")
        self._assert_not_owner(
            identity, candidate_snapshot, "候选所有者不能验证自己的提案"
        )
        request_payload = {
            "skill_id": normalized.skill_id,
            "version": normalized.version,
            "suite_path": str(Path(normalized.suite_path).resolve()),
            "suite_sha256": suite_hash,
            "model_id": normalized.model_id,
            "runner_id": runner.runner_id,
            "runner_version": runner.runner_version,
            "policy": self._policy_payload(self.evaluation_policy),
        }
        request_hash = self.repository.request_hash(request_payload)
        run_result = runner.run(
            suite_path=normalized.suite_path,
            suite_sha256=suite_hash,
            model_id=normalized.model_id,
            candidate=candidate_snapshot,
        )
        suite_hash_after_run = sha256_bytes(Path(normalized.suite_path).read_bytes())
        if suite_hash_after_run != suite_hash:
            raise SkillTamperError("评测套件在执行期间发生变化")
        run_result = self._validate_run_result(
            run_result, normalized.model_id, suite_hash
        )
        with self.repository.transaction() as conn:
            replay = self.repository.find_idempotent(
                conn,
                self.tenant_id,
                identity.actor_user_id,
                "evaluate",
                normalized.idempotency_key,
                request_hash,
            )
            if replay is not None:
                if not replay["evaluation_id"]:
                    raise SkillTamperError("评测幂等记录缺少 evaluation_id")
                return self.repository.get_evaluation(
                    conn, self.tenant_id, replay["evaluation_id"]
                )

            record = self.repository.get_version(
                conn, self.tenant_id, normalized.skill_id, normalized.version
            )
            if record.status is not SkillStatus.CANDIDATE:
                raise SkillValidationError("只有 candidate 状态可以接受新评测")
            evaluation = self._build_evaluation(
                identity, record, normalized, suite_hash, run_result, runner
            )
            self.repository.insert_evaluation(conn, evaluation)
            self.repository.append_audit(
                conn,
                tenant_id=self.tenant_id,
                skill_id=record.skill_id,
                version=record.version,
                actor_user_id=identity.actor_user_id,
                action="skill.evaluated",
                details={
                    "auth_source": identity.auth_source,
                    "evaluation_id": evaluation.evaluation_id,
                    "suite_sha256": evaluation.suite_sha256,
                    "sample_count": evaluation.sample_count,
                    "runner_id": evaluation.runner_id,
                    "runner_version": evaluation.runner_version,
                    "gate_passed": evaluation.gate_passed,
                    "gate_failures": evaluation.gate_failures,
                },
                trace_id=identity.trace_id,
            )
            self.repository.save_idempotent(
                conn,
                tenant_id=self.tenant_id,
                actor_user_id=identity.actor_user_id,
                operation="evaluate",
                idempotency_key=normalized.idempotency_key,
                request_hash=request_hash,
                result_type="evaluation",
                skill_id=record.skill_id,
                version=record.version,
                evaluation_id=evaluation.evaluation_id,
            )
            return evaluation

    def reject(
        self,
        identity: SkillIdentity,
        skill_id: str,
        version: int,
        reason: str,
        idempotency_key: str,
    ) -> SkillVersion:
        """由独立验证者把候选标记为拒绝，且不删除版本。"""

        self._require_role(identity, "skill:validate")
        self._validate_lifecycle_input(skill_id, version, reason, idempotency_key)
        request_hash = self.repository.request_hash(
            {"skill_id": skill_id, "version": version, "reason": reason.strip()}
        )
        with self.repository.transaction() as conn:
            replay = self.repository.find_idempotent(
                conn,
                self.tenant_id,
                identity.actor_user_id,
                "reject",
                idempotency_key,
                request_hash,
            )
            if replay is not None:
                return self.repository.get_version(
                    conn, self.tenant_id, replay["skill_id"], int(replay["version"])
                )
            record = self.repository.get_version(conn, self.tenant_id, skill_id, version)
            self._assert_not_owner(identity, record, "候选所有者不能拒绝自己的提案")
            if record.status is not SkillStatus.CANDIDATE:
                raise SkillValidationError("只有 candidate 状态可以拒绝")
            self.repository.append_state(
                conn,
                tenant_id=self.tenant_id,
                skill_id=skill_id,
                version=version,
                status=SkillStatus.REJECTED,
                evaluation_id=None,
                reason=reason.strip(),
                actor_user_id=identity.actor_user_id,
                trace_id=identity.trace_id,
            )
            self.repository.append_audit(
                conn,
                tenant_id=self.tenant_id,
                skill_id=skill_id,
                version=version,
                actor_user_id=identity.actor_user_id,
                action="skill.rejected",
                details={"auth_source": identity.auth_source, "reason": reason.strip()},
                trace_id=identity.trace_id,
            )
            self.repository.save_idempotent(
                conn,
                tenant_id=self.tenant_id,
                actor_user_id=identity.actor_user_id,
                operation="reject",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                result_type="version",
                skill_id=skill_id,
                version=version,
            )
            return replace(record, status=SkillStatus.REJECTED)

    def publish(
        self,
        identity: SkillIdentity,
        skill_id: str,
        version: int,
        evaluation_id: str,
        idempotency_key: str,
    ) -> SkillVersion:
        """通过固定门禁后原子投影，并在投影失败时回滚数据库事务。"""

        self._require_role(identity, "skill:publish")
        self._require_evaluation_runner()
        self._validate_publish_input(skill_id, version, evaluation_id, idempotency_key)
        request_hash = self.repository.request_hash(
            {
                "skill_id": skill_id,
                "version": version,
                "evaluation_id": evaluation_id,
            }
        )
        with self._projection_lock:
            return self._publish_locked(
                identity,
                skill_id,
                version,
                evaluation_id,
                idempotency_key,
                request_hash,
            )

    def rollback(
        self,
        identity: SkillIdentity,
        skill_id: str,
        target_version: int,
        reason: str,
        idempotency_key: str,
    ) -> SkillVersion:
        """复制已验证历史正文为新版本，不改写任何既有历史。"""

        self._require_role(identity, "skill:publish")
        self._require_evaluation_runner()
        self._validate_lifecycle_input(
            skill_id, target_version, reason, idempotency_key
        )
        request_hash = self.repository.request_hash(
            {
                "skill_id": skill_id,
                "target_version": target_version,
                "reason": reason.strip(),
            }
        )
        with self._projection_lock:
            return self._rollback_locked(
                identity,
                skill_id,
                target_version,
                reason.strip(),
                idempotency_key,
                request_hash,
            )

    def get_version(
        self, identity: SkillIdentity, skill_id: str, version: int
    ) -> SkillVersion:
        """读取租户内的指定技能版本。"""

        self._require_read(identity)
        return self.repository.read_version(self.tenant_id, skill_id, version)

    def list_versions(
        self, identity: SkillIdentity, skill_id: str
    ) -> Tuple[SkillVersion, ...]:
        """读取完整版本链。"""

        self._require_read(identity)
        return tuple(self.repository.list_versions(self.tenant_id, skill_id))

    def list_candidates(self, identity: SkillIdentity) -> Tuple[SkillVersion, ...]:
        """跨技能列出当前候选，供只读 CLI 或 API 展示。"""

        self._require_read(identity)
        return tuple(self.repository.list_candidates(self.tenant_id))

    def list_evaluations(
        self, identity: SkillIdentity, skill_id: str, version: int
    ) -> Tuple[SkillEvaluation, ...]:
        """读取目标版本的仅追加评测轨迹。"""

        self._require_read(identity)
        return tuple(
            self.repository.list_evaluations(self.tenant_id, skill_id, version)
        )

    def list_audit(
        self, identity: SkillIdentity, skill_id: str
    ) -> Tuple[SkillAuditEvent, ...]:
        """读取并核验技能审计哈希链。"""

        self._require_read(identity)
        return tuple(self.repository.list_audit(self.tenant_id, skill_id))

    def verify_projection(self, identity: SkillIdentity, name: str) -> SkillVersion:
        """核验有效记录与 SKILL.md 投影逐字节一致。"""

        self._require_read(identity)
        self._validate_name(name)
        active = self.repository.read_active_by_name(self.tenant_id, name)
        if active is None:
            raise SkillNotFoundError("有效技能不存在")
        path = self._projection_path(name)
        if path.is_symlink() or not path.is_file():
            raise SkillTamperError("技能投影缺失或不是普通文件")
        actual = path.read_bytes()
        expected = self.render_projection(active).encode("utf-8")
        if actual != expected:
            raise SkillTamperError("技能投影与不可变事实库不一致")
        return active

    def render_projection(self, record: SkillVersion) -> str:
        """把结构化技能确定性渲染为当前 SkillLoader 可读取的 Markdown。"""

        source_hashes = [item["sha256"] for item in record.provenance]
        lines = [
            "---",
            "name: %s" % json.dumps(record.name, ensure_ascii=False),
            "description: %s" % json.dumps(record.description, ensure_ascii=False),
            "governed: true",
            "governed-version: %d" % record.version,
            "governed-content-sha256: %s" % record.content_hash,
            "model-compatibility: %s"
            % json.dumps(record.model_compatibility, ensure_ascii=False),
            "source-sha256: %s" % json.dumps(source_hashes, ensure_ascii=False),
            "---",
            "",
            "# %s" % record.name,
            "",
            record.description,
            "",
            "## 适用条件",
            "",
        ]
        lines.extend("- %s" % item for item in record.applicability)
        lines.extend(["", "## 操作步骤", ""])
        lines.extend("%d. %s" % (index, item) for index, item in enumerate(record.steps, 1))
        lines.extend(["", "## 校验规则", ""])
        lines.extend("- %s" % item for item in record.validation_rules)
        lines.extend(["", "## 禁用条件", ""])
        lines.extend("- %s" % item for item in record.contraindications)
        lines.extend(["", "## 来源轨迹", ""])
        for item in record.provenance:
            lines.append(
                "- `%s` `%s` SHA-256 `%s`"
                % (item["source_type"], item["source_ref"], item["sha256"])
            )
        return "\n".join(lines) + "\n"

    def _publish_locked(
        self,
        identity: SkillIdentity,
        skill_id: str,
        version: int,
        evaluation_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> SkillVersion:
        """在投影互斥区和数据库事务中执行发布。"""

        snapshot_path = None
        snapshot_bytes = None
        snapshot_existed = False
        projection_attempted = False
        database_committed = False
        try:
            with self.repository.transaction() as conn:
                self._recover_projection_journal(conn)
                replay = self.repository.find_idempotent(
                    conn,
                    self.tenant_id,
                    identity.actor_user_id,
                    "publish",
                    idempotency_key,
                    request_hash,
                )
                if replay is not None:
                    return self.repository.get_version(
                        conn,
                        self.tenant_id,
                        replay["skill_id"],
                        int(replay["version"]),
                    )
                record = self.repository.get_version(
                    conn, self.tenant_id, skill_id, version
                )
                if record.status is not SkillStatus.CANDIDATE:
                    raise SkillValidationError("只有 candidate 状态可以发布")
                self._assert_not_owner(identity, record, "候选所有者不能发布自己的提案")
                evaluation = self.repository.get_evaluation(
                    conn, self.tenant_id, evaluation_id
                )
                self._assert_publishable(identity, record, evaluation)
                active = self.repository.get_active_by_name(
                    conn, self.tenant_id, record.name
                )
                snapshot_path, snapshot_bytes, snapshot_existed = self._snapshot_projection(
                    active, record
                )
                if active is not None:
                    self.repository.append_state(
                        conn,
                        tenant_id=self.tenant_id,
                        skill_id=active.skill_id,
                        version=active.version,
                        status=SkillStatus.SUPERSEDED,
                        evaluation_id=None,
                        reason="新版本发布",
                        actor_user_id=identity.actor_user_id,
                        trace_id=identity.trace_id,
                    )
                self.repository.append_state(
                    conn,
                    tenant_id=self.tenant_id,
                    skill_id=record.skill_id,
                    version=record.version,
                    status=SkillStatus.ACTIVE,
                    evaluation_id=evaluation.evaluation_id,
                    reason="发布门禁通过",
                    actor_user_id=identity.actor_user_id,
                    trace_id=identity.trace_id,
                )
                self.repository.append_audit(
                    conn,
                    tenant_id=self.tenant_id,
                    skill_id=record.skill_id,
                    version=record.version,
                    actor_user_id=identity.actor_user_id,
                    action="skill.published",
                    details={
                        "auth_source": identity.auth_source,
                        "evaluation_id": evaluation.evaluation_id,
                        "suite_sha256": evaluation.suite_sha256,
                    },
                    trace_id=identity.trace_id,
                )
                self.repository.save_idempotent(
                    conn,
                    tenant_id=self.tenant_id,
                    actor_user_id=identity.actor_user_id,
                    operation="publish",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_type="version",
                    skill_id=record.skill_id,
                    version=record.version,
                    evaluation_id=evaluation.evaluation_id,
                )
                active_record = replace(record, status=SkillStatus.ACTIVE)
                self._write_projection_journal(
                    active_record,
                    snapshot_bytes,
                    snapshot_existed,
                    "publish",
                )
                projection_attempted = True
                self._project_record(active_record)
            database_committed = True
            self._clear_projection_journal()
            return active_record
        except BaseException:
            if (
                projection_attempted
                and not database_committed
                and snapshot_path is not None
            ):
                self._restore_projection(
                    snapshot_path, snapshot_bytes, snapshot_existed
                )
                self._clear_projection_journal()
            raise

    def _rollback_locked(
        self,
        identity: SkillIdentity,
        skill_id: str,
        target_version: int,
        reason: str,
        idempotency_key: str,
        request_hash: str,
    ) -> SkillVersion:
        """在一个事务中生成并投影回滚版本。"""

        snapshot_path = None
        snapshot_bytes = None
        snapshot_existed = False
        projection_attempted = False
        database_committed = False
        try:
            with self.repository.transaction() as conn:
                self._recover_projection_journal(conn)
                replay = self.repository.find_idempotent(
                    conn,
                    self.tenant_id,
                    identity.actor_user_id,
                    "rollback",
                    idempotency_key,
                    request_hash,
                )
                if replay is not None:
                    return self.repository.get_version(
                        conn,
                        self.tenant_id,
                        replay["skill_id"],
                        int(replay["version"]),
                    )
                target = self.repository.get_version(
                    conn, self.tenant_id, skill_id, target_version
                )
                self._assert_not_owner(identity, target, "候选所有者不能发布回滚版本")
                if not self.repository.version_had_status(
                    conn,
                    self.tenant_id,
                    skill_id,
                    target_version,
                    SkillStatus.ACTIVE,
                ):
                    raise SkillPublishGateError("只能回滚到曾经通过发布门禁的版本")
                evaluation_id = self.repository.active_evaluation_id(
                    conn, self.tenant_id, skill_id, target_version
                )
                if not evaluation_id:
                    raise SkillTamperError("历史有效版本缺少发布评测")
                evaluation = self.repository.get_evaluation(
                    conn, self.tenant_id, evaluation_id
                )
                evaluated_record = self.repository.get_version(
                    conn,
                    self.tenant_id,
                    evaluation.skill_id,
                    evaluation.version,
                )
                if (
                    evaluated_record.skill_id != target.skill_id
                    or evaluated_record.content_hash != target.content_hash
                ):
                    raise SkillPublishGateError(
                        "回滚版本与原始评测正文不一致"
                    )
                self._assert_publishable(identity, evaluated_record, evaluation)
                active = self.repository.get_active_by_name(
                    conn, self.tenant_id, target.name
                )
                if active is None:
                    raise SkillTamperError("回滚前不存在有效技能")
                snapshot_path, snapshot_bytes, snapshot_existed = self._snapshot_projection(
                    active, target
                )
                version = self.repository.next_version(conn, self.tenant_id, skill_id)
                restored = replace(
                    target,
                    version=version,
                    status=SkillStatus.ACTIVE,
                    created_by=identity.actor_user_id,
                    trace_id=identity.trace_id,
                    created_at=utc_now(),
                    rollback_of_version=target.version,
                )
                self.repository.insert_version(conn, restored)
                self.repository.append_state(
                    conn,
                    tenant_id=self.tenant_id,
                    skill_id=active.skill_id,
                    version=active.version,
                    status=SkillStatus.SUPERSEDED,
                    evaluation_id=None,
                    reason="发布回滚版本",
                    actor_user_id=identity.actor_user_id,
                    trace_id=identity.trace_id,
                )
                self.repository.append_state(
                    conn,
                    tenant_id=self.tenant_id,
                    skill_id=skill_id,
                    version=version,
                    status=SkillStatus.ACTIVE,
                    evaluation_id=evaluation_id,
                    reason=reason,
                    actor_user_id=identity.actor_user_id,
                    trace_id=identity.trace_id,
                )
                self.repository.append_audit(
                    conn,
                    tenant_id=self.tenant_id,
                    skill_id=skill_id,
                    version=version,
                    actor_user_id=identity.actor_user_id,
                    action="skill.rolled_back",
                    details={
                        "auth_source": identity.auth_source,
                        "target_version": target_version,
                        "evaluation_id": evaluation_id,
                        "reason": reason,
                    },
                    trace_id=identity.trace_id,
                )
                self.repository.save_idempotent(
                    conn,
                    tenant_id=self.tenant_id,
                    actor_user_id=identity.actor_user_id,
                    operation="rollback",
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    result_type="version",
                    skill_id=skill_id,
                    version=version,
                    evaluation_id=evaluation_id,
                )
                self._write_projection_journal(
                    restored,
                    snapshot_bytes,
                    snapshot_existed,
                    "rollback",
                )
                projection_attempted = True
                self._project_record(restored)
            database_committed = True
            self._clear_projection_journal()
            return restored
        except BaseException:
            if (
                projection_attempted
                and not database_committed
                and snapshot_path is not None
            ):
                self._restore_projection(
                    snapshot_path, snapshot_bytes, snapshot_existed
                )
                self._clear_projection_journal()
            raise

    def _assert_publishable(
        self,
        identity: SkillIdentity,
        record: SkillVersion,
        evaluation: SkillEvaluation,
    ) -> None:
        """核验评测归属、职责分离、门禁结果和套件当前哈希。"""

        if evaluation.skill_id != record.skill_id or evaluation.version != record.version:
            raise SkillPublishGateError("评测不属于目标技能版本")
        if evaluation.validator_user_id == identity.actor_user_id:
            raise SkillAuthorizationError("验证者不能发布自己验证的候选")
        runner = self._require_evaluation_runner()
        if (
            evaluation.runner_id != runner.runner_id
            or evaluation.runner_version != runner.runner_version
        ):
            raise SkillPublishGateError("发布环境未配置生成该证据的可信评测运行器")
        if evaluation.gate_schema_version != _GATE_SCHEMA_VERSION:
            raise SkillPublishGateError("评测门禁版本不是当前版本，必须重新评测")
        if not evaluation.gate_passed:
            raise SkillPublishGateError(
                "技能未通过发布门禁: %s" % ", ".join(evaluation.gate_failures)
            )
        if evaluation.policy != self.evaluation_policy:
            raise SkillPublishGateError("评测策略与当前发布策略不一致，必须重新评测")
        current_failures = self._current_policy_failures(evaluation)
        if current_failures:
            raise SkillPublishGateError(
                "评测证据不满足当前发布策略: %s" % ", ".join(current_failures)
            )
        if evaluation.baseline_model_id != evaluation.candidate_model_id:
            raise SkillPublishGateError("发布评测不是同模型配对基线")
        if evaluation.candidate_model_id not in record.model_compatibility:
            raise SkillPublishGateError("评测模型不在技能兼容模型集合中")
        suite_path = Path(evaluation.suite_path)
        self._assert_no_link_components(suite_path, "评测套件")
        if not suite_path.is_file():
            raise SkillTamperError("评测套件缺失或不是普通文件")
        actual_hash = sha256_bytes(suite_path.read_bytes())
        if actual_hash != evaluation.suite_sha256:
            raise SkillTamperError("评测套件在验证后发生变化")

    def _current_policy_failures(
        self, evaluation: SkillEvaluation
    ) -> Tuple[str, ...]:
        """从逐样本证据重算全部门禁，禁止信任历史汇总字段。"""

        failures = []
        policy = self.evaluation_policy
        samples = evaluation.samples
        if not samples:
            return ("逐样本评测证据为空",)
        sample_ids = [sample.sample_id for sample in samples]
        if len(set(sample_ids)) != len(sample_ids):
            failures.append("逐样本标识重复")
        invalid_latency = any(
            not math.isfinite(sample.baseline_latency_ms)
            or not math.isfinite(sample.candidate_latency_ms)
            or sample.baseline_latency_ms < 0
            or sample.candidate_latency_ms < 0
            for sample in samples
        )
        if invalid_latency:
            failures.append("逐样本延迟无效")
        baseline_passed = sum(sample.baseline_success for sample in samples)
        candidate_passed = sum(sample.candidate_success for sample in samples)
        regression_count = sum(
            sample.baseline_success and not sample.candidate_success
            for sample in samples
        )
        baseline_p95 = self._percentile_95(
            sample.baseline_latency_ms for sample in samples
        )
        candidate_p95 = self._percentile_95(
            sample.candidate_latency_ms for sample in samples
        )
        if evaluation.sample_count != len(samples):
            failures.append("样本数量汇总与逐样本证据不一致")
        if evaluation.baseline_passed != baseline_passed:
            failures.append("基线成功数汇总与逐样本证据不一致")
        if evaluation.candidate_passed != candidate_passed:
            failures.append("候选成功数汇总与逐样本证据不一致")
        if evaluation.regression_count != regression_count:
            failures.append("回归数汇总与逐样本证据不一致")
        if not math.isclose(
            evaluation.baseline_p95_latency_ms,
            baseline_p95,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            failures.append("基线 P95 汇总与逐样本证据不一致")
        if not math.isclose(
            evaluation.candidate_p95_latency_ms,
            candidate_p95,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            failures.append("候选 P95 汇总与逐样本证据不一致")
        if len(samples) < policy.minimum_sample_count:
            failures.append("样本数不足")
        if candidate_passed <= baseline_passed:
            failures.append("配对基线未严格提升")
        if regression_count != 0:
            failures.append("存在回归样本")
        if candidate_p95 > policy.max_candidate_p95_latency_ms:
            failures.append("候选 P95 延迟超过绝对阈值")
        if candidate_p95 > baseline_p95 * policy.max_latency_regression_ratio:
            failures.append("候选 P95 延迟超过相对阈值")
        return tuple(failures)

    def _build_evaluation(
        self,
        identity: SkillIdentity,
        record: SkillVersion,
        command: SkillEvaluationCommand,
        suite_hash: str,
        run_result: EvaluationRunResult,
        runner: EvaluationRunner,
    ) -> SkillEvaluation:
        """根据逐样本结果计算不可由提案方调整的门禁。"""

        samples = run_result.samples
        baseline_passed = sum(sample.baseline_success for sample in samples)
        candidate_passed = sum(sample.candidate_success for sample in samples)
        regressions = sum(
            sample.baseline_success and not sample.candidate_success
            for sample in samples
        )
        baseline_p95 = self._percentile_95(
            sample.baseline_latency_ms for sample in samples
        )
        candidate_p95 = self._percentile_95(
            sample.candidate_latency_ms for sample in samples
        )
        failures = []
        if command.model_id not in record.model_compatibility:
            failures.append("模型不兼容")
        if len(samples) < self.evaluation_policy.minimum_sample_count:
            failures.append("样本数不足")
        if candidate_passed <= baseline_passed:
            failures.append("配对基线未严格提升")
        if regressions != 0:
            failures.append("存在回归样本")
        if candidate_p95 > self.evaluation_policy.max_candidate_p95_latency_ms:
            failures.append("候选 P95 延迟超过绝对阈值")
        if candidate_p95 > (
            baseline_p95 * self.evaluation_policy.max_latency_regression_ratio
        ):
            failures.append("候选 P95 延迟超过相对阈值")
        evaluation = SkillEvaluation(
            evaluation_id=str(uuid.uuid4()),
            tenant_id=self.tenant_id,
            skill_id=record.skill_id,
            version=record.version,
            validator_user_id=identity.actor_user_id,
            runner_id=runner.runner_id,
            runner_version=runner.runner_version,
            suite_path=str(Path(command.suite_path).resolve()),
            suite_sha256=suite_hash,
            sample_count=len(samples),
            baseline_model_id=run_result.baseline_model_id,
            candidate_model_id=run_result.candidate_model_id,
            baseline_passed=baseline_passed,
            candidate_passed=candidate_passed,
            regression_count=regressions,
            baseline_p95_latency_ms=baseline_p95,
            candidate_p95_latency_ms=candidate_p95,
            gate_passed=not failures,
            gate_failures=tuple(failures),
            samples=samples,
            policy=self.evaluation_policy,
            record_hash="",
            trace_id=identity.trace_id,
            created_at=utc_now(),
            gate_schema_version=_GATE_SCHEMA_VERSION,
        )
        record_hash = sha256_bytes(
            canonical_json(self.repository.evaluation_payload(evaluation)).encode("utf-8")
        )
        return replace(evaluation, record_hash=record_hash)

    def _validate_proposal(self, proposal: SkillProposal) -> SkillProposal:
        """规范化结构化提案并逐项核验来源字节。"""

        if not isinstance(proposal, SkillProposal):
            raise SkillValidationError("proposal 必须是 SkillProposal")
        name = proposal.name.strip()
        self._validate_name(name)
        description = self._clean_text(proposal.description, "description", 4096, False)
        applicability = self._validate_text_tuple(
            proposal.applicability, "applicability"
        )
        steps = self._validate_text_tuple(proposal.steps, "steps")
        validation_rules = self._validate_text_tuple(
            proposal.validation_rules, "validation_rules"
        )
        contraindications = self._validate_text_tuple(
            proposal.contraindications, "contraindications"
        )
        model_compatibility = self._validate_text_tuple(
            proposal.model_compatibility, "model_compatibility"
        )
        if len(set(model_compatibility)) != len(model_compatibility):
            raise SkillValidationError("model_compatibility 不能包含重复模型")
        if not isinstance(proposal.sources, tuple) or not proposal.sources:
            raise SkillValidationError("sources 必须是非空 tuple")
        verified_sources = []
        for source in proposal.sources:
            verified_sources.append(self._validate_source(source))
        idempotency_key = self._clean_text(
            proposal.idempotency_key, "idempotency_key", 256, True
        )
        return SkillProposal(
            name=name,
            description=description,
            applicability=applicability,
            steps=steps,
            validation_rules=validation_rules,
            contraindications=contraindications,
            model_compatibility=model_compatibility,
            sources=tuple(verified_sources),
            idempotency_key=idempotency_key,
        )

    def _validate_source(self, source: SourceEvidence) -> SourceEvidence:
        """核验来源元数据和调用时原始字节的 SHA-256。"""

        if not isinstance(source, SourceEvidence):
            raise SkillValidationError("sources 只能包含 SourceEvidence")
        source_type = self._clean_text(source.source_type, "source_type", 128, True)
        source_ref = self._clean_text(source.source_ref, "source_ref", 2048, True)
        if not isinstance(source.payload, bytes) or not source.payload:
            raise SkillValidationError("来源 payload 必须是非空 bytes")
        expected_hash = source.sha256.lower()
        if not _SHA256_RE.fullmatch(expected_hash):
            raise SkillValidationError("来源 sha256 格式无效")
        actual_hash = sha256_bytes(source.payload)
        if actual_hash != expected_hash:
            raise SkillTamperError("来源轨迹 SHA-256 不匹配")
        return SourceEvidence(source_type, source_ref, source.payload, expected_hash)

    def _validate_evaluation_command(
        self, command: SkillEvaluationCommand
    ) -> Tuple[SkillEvaluationCommand, str]:
        """核验评测文件和目标模型，不接受调用方提交成绩。"""

        if not isinstance(command, SkillEvaluationCommand):
            raise SkillValidationError("command 必须是 SkillEvaluationCommand")
        self._clean_text(command.skill_id, "skill_id", 128, True)
        if not isinstance(command.version, int) or isinstance(command.version, bool) or command.version <= 0:
            raise SkillValidationError("version 必须是正整数")
        suite_path_text = self._clean_text(command.suite_path, "suite_path", 4096, True)
        suite_path = Path(suite_path_text)
        self._assert_no_link_components(suite_path, "评测套件")
        if not suite_path.is_file():
            raise SkillValidationError("评测套件必须是已存在的普通文件")
        suite_hash = sha256_bytes(suite_path.read_bytes())
        expected_hash = command.suite_sha256.lower()
        if not _SHA256_RE.fullmatch(expected_hash):
            raise SkillValidationError("suite_sha256 格式无效")
        if suite_hash != expected_hash:
            raise SkillTamperError("评测套件 SHA-256 与声明不一致")
        model_id = self._clean_text(command.model_id, "model_id", 256, True)
        idempotency_key = self._clean_text(
            command.idempotency_key, "idempotency_key", 256, True
        )
        normalized = SkillEvaluationCommand(
            skill_id=command.skill_id.strip(),
            version=command.version,
            suite_path=str(suite_path.resolve()),
            suite_sha256=expected_hash,
            model_id=model_id,
            idempotency_key=idempotency_key,
        )
        return normalized, suite_hash

    def _validate_run_result(
        self,
        result: EvaluationRunResult,
        model_id: str,
        suite_sha256: str,
    ) -> EvaluationRunResult:
        """核验可信运行器返回值与本次套件、模型和配对结构一致。"""

        if not isinstance(result, EvaluationRunResult):
            raise SkillValidationError("评测运行器必须返回 EvaluationRunResult")
        if result.suite_sha256 != suite_sha256:
            raise SkillTamperError("评测运行器返回的套件哈希不匹配")
        if (
            result.baseline_model_id != model_id
            or result.candidate_model_id != model_id
        ):
            raise SkillValidationError("评测运行器没有执行同模型配对基线")
        if not isinstance(result.samples, tuple) or not result.samples:
            raise SkillValidationError("评测运行器没有返回配对样本")
        sample_ids = set()
        normalized_samples = []
        for sample in result.samples:
            normalized = self._validate_sample(sample)
            if normalized.sample_id in sample_ids:
                raise SkillValidationError("评测运行器返回了重复 sample_id")
            sample_ids.add(normalized.sample_id)
            normalized_samples.append(normalized)
        return EvaluationRunResult(
            suite_sha256=suite_sha256,
            baseline_model_id=model_id,
            candidate_model_id=model_id,
            samples=tuple(normalized_samples),
        )

    def _validate_sample(self, sample: PairedSampleResult) -> PairedSampleResult:
        """核验单条配对样本结果。"""

        if not isinstance(sample, PairedSampleResult):
            raise SkillValidationError("samples 只能包含 PairedSampleResult")
        sample_id = self._clean_text(sample.sample_id, "sample_id", 512, True)
        if type(sample.baseline_success) is not bool or type(sample.candidate_success) is not bool:
            raise SkillValidationError("样本 success 字段必须是 bool")
        for field_name, value in (
            ("baseline_latency_ms", sample.baseline_latency_ms),
            ("candidate_latency_ms", sample.candidate_latency_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SkillValidationError("%s 必须是数值" % field_name)
            if not math.isfinite(float(value)) or float(value) < 0:
                raise SkillValidationError("%s 必须是有限非负数" % field_name)
        return PairedSampleResult(
            sample_id=sample_id,
            baseline_success=sample.baseline_success,
            candidate_success=sample.candidate_success,
            baseline_latency_ms=float(sample.baseline_latency_ms),
            candidate_latency_ms=float(sample.candidate_latency_ms),
        )

    def _snapshot_projection(
        self, active: Optional[SkillVersion], candidate: SkillVersion
    ) -> Tuple[Path, Optional[bytes], bool]:
        """核验旧投影并保存发布失败时的恢复快照。"""

        path = self._projection_path(candidate.name)
        if path.is_symlink():
            raise SkillTamperError("技能投影不能是符号链接")
        if active is None:
            if not path.exists():
                return path, None, False
            if not path.is_file():
                raise SkillTamperError("既有同名技能不是普通文件")
            actual = path.read_bytes()
            actual_hash = sha256_bytes(actual)
            takeover = any(
                item.get("source_type") == "existing-skill"
                and Path(item.get("source_ref", "")).is_absolute()
                and Path(item["source_ref"]).resolve() == path
                and item.get("sha256") == actual_hash
                for item in candidate.provenance
            )
            if not takeover:
                raise SkillTamperError("既有同名技能缺少匹配的接管来源证据")
            return path, actual, True
        if not path.is_file():
            raise SkillTamperError("有效技能投影缺失")
        actual = path.read_bytes()
        expected = self.render_projection(active).encode("utf-8")
        if actual != expected:
            raise SkillTamperError("旧技能投影已被外部修改")
        return path, actual, True

    def _write_projection_journal(
        self,
        record: SkillVersion,
        previous_bytes: Optional[bytes],
        previous_existed: bool,
        operation: str,
    ) -> None:
        """在替换投影前持久化恢复信息，供进程硬退出后确定性修复。"""

        projection_bytes = self.render_projection(record).encode("utf-8")
        payload = {
            "schema_version": 1,
            "tenant_id": self.tenant_id,
            "operation": operation,
            "skill_id": record.skill_id,
            "version": record.version,
            "name": record.name,
            "projection_sha256": sha256_bytes(projection_bytes),
            "previous_existed": previous_existed,
            "previous_sha256": (
                sha256_bytes(previous_bytes) if previous_bytes is not None else None
            ),
            "previous_base64": (
                base64.b64encode(previous_bytes).decode("ascii")
                if previous_bytes is not None
                else None
            ),
        }
        envelope = {
            "payload": payload,
            "checksum": sha256_bytes(
                canonical_json(payload).encode("utf-8")
            ),
        }
        self._atomic_replace_private(
            self._projection_journal_path,
            canonical_json(envelope).encode("utf-8"),
        )

    def _recover_projection_journal(self, conn) -> None:
        """依据已提交事实库恢复或确认投影，并清理持久化事务日志。"""

        journal_path = self._projection_journal_path
        if not journal_path.exists():
            return
        if self._is_link_or_reparse_point(journal_path) or not journal_path.is_file():
            raise SkillTamperError("技能投影事务日志不是普通文件")
        try:
            envelope = json.loads(journal_path.read_text(encoding="utf-8"))
            payload = envelope["payload"]
            checksum = envelope["checksum"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise SkillTamperError("技能投影事务日志无法解析") from error
        expected_checksum = sha256_bytes(
            canonical_json(payload).encode("utf-8")
        )
        if checksum != expected_checksum:
            raise SkillTamperError("技能投影事务日志校验失败")
        if payload.get("schema_version") != 1 or payload.get("tenant_id") != self.tenant_id:
            raise SkillTamperError("技能投影事务日志归属无效")
        name = payload.get("name")
        skill_id = payload.get("skill_id")
        version = payload.get("version")
        if not isinstance(name, str) or not isinstance(skill_id, str):
            raise SkillTamperError("技能投影事务日志字段无效")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise SkillTamperError("技能投影事务日志版本无效")
        path = self._projection_path(name)
        previous_existed = payload.get("previous_existed")
        if not isinstance(previous_existed, bool):
            raise SkillTamperError("技能投影事务日志快照状态无效")
        previous_encoded = payload.get("previous_base64")
        previous_bytes = None
        if previous_encoded is not None:
            if not isinstance(previous_encoded, str):
                raise SkillTamperError("技能投影事务日志快照无效")
            try:
                previous_bytes = base64.b64decode(previous_encoded, validate=True)
            except (ValueError, TypeError) as error:
                raise SkillTamperError("技能投影事务日志快照无法解码") from error
        if previous_existed != (previous_bytes is not None):
            raise SkillTamperError("技能投影事务日志快照不完整")
        previous_hash = payload.get("previous_sha256")
        if previous_bytes is not None and sha256_bytes(previous_bytes) != previous_hash:
            raise SkillTamperError("技能投影事务日志快照校验失败")

        target = None
        try:
            target = self.repository.get_version(
                conn, self.tenant_id, skill_id, version
            )
        except SkillNotFoundError:
            target = None
        active = self.repository.get_active_by_name(conn, self.tenant_id, name)
        desired = target if target is not None and target.status is SkillStatus.ACTIVE else active
        if desired is not None:
            desired_bytes = self.render_projection(desired).encode("utf-8")
            if desired is target and sha256_bytes(desired_bytes) != payload.get(
                "projection_sha256"
            ):
                raise SkillTamperError("已提交技能与投影事务日志不一致")
            self._atomic_replace(path, desired_bytes)
        else:
            self._restore_projection(path, previous_bytes, previous_existed)
        self._clear_projection_journal()

    def _clear_projection_journal(self) -> None:
        """只删除当前租户的已完成投影事务日志。"""

        path = self._projection_journal_path
        if not path.exists():
            return
        if self._is_link_or_reparse_point(path) or not path.is_file():
            raise SkillTamperError("技能投影事务日志不是普通文件")
        path.unlink()
        self._fsync_directory(path.parent)

    def _project_record(self, record: SkillVersion) -> None:
        """用同目录临时文件和 os.replace 原子替换 SKILL.md。"""

        path = self._projection_path(record.name)
        self._atomic_replace(path, self.render_projection(record).encode("utf-8"))

    def _restore_projection(
        self, path: Path, previous_bytes: Optional[bytes], existed: bool
    ) -> None:
        """在数据库发布事务失败后恢复原投影。"""

        if existed:
            if previous_bytes is None:
                raise SkillTamperError("投影恢复快照无效")
            self._atomic_replace(path, previous_bytes)
            return
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise SkillTamperError("无法清理失败发布的异常投影")
            path.unlink()

    def _atomic_replace(self, path: Path, payload: bytes) -> None:
        """写入、同步并原子替换一个 UTF-8 投影文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_projection_path_components(path)
        temp_path = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
        try:
            with temp_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp_path), str(path))
            self._fsync_directory(path.parent)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _atomic_replace_private(self, path: Path, payload: bytes) -> None:
        """原子写入 skills/.system 内的私有事务文件。"""

        if self._is_link_or_reparse_point(path.parent):
            raise SkillTamperError("skills/.system 不能是符号链接或重解析点")
        if self._is_link_or_reparse_point(path):
            raise SkillTamperError("技能投影事务日志不能是符号链接或重解析点")
        temp_path = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
        try:
            with temp_path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temp_path), str(path))
            self._fsync_directory(path.parent)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _projection_path(self, name: str) -> Path:
        """解析并验证固定格式的技能投影路径。"""

        self._validate_name(name)
        path = self.skills_dir / name / "SKILL.md"
        self._assert_projection_path_components(path)
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.skills_dir)
        except ValueError as error:
            raise SkillValidationError("技能投影路径超出 skills 目录") from error
        return path

    def _assert_projection_path_components(self, path: Path) -> None:
        """拒绝技能名目录和投影文件上的符号链接、联接点及重解析点。"""

        try:
            relative = path.relative_to(self.skills_dir)
        except ValueError as error:
            raise SkillValidationError("技能投影路径超出 skills 目录") from error
        current = self.skills_dir
        for part in relative.parts:
            current = current / part
            if self._is_link_or_reparse_point(current):
                raise SkillTamperError("技能投影路径包含符号链接或重解析点")

    def _assert_no_link_components(self, path: Path, label: str) -> None:
        """拒绝任意既有路径组件中的符号链接、联接点及重解析点。"""

        absolute = Path(os.path.abspath(os.fspath(path)))
        components = [absolute]
        components.extend(parent for parent in absolute.parents if parent != parent.parent)
        for component in components:
            if self._is_link_or_reparse_point(component):
                raise SkillTamperError("%s路径包含符号链接或重解析点" % label)

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        """同时识别 POSIX 符号链接和 Windows 重解析点。"""

        return is_link_or_reparse_point(path)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """在平台支持时同步目录项；Windows 不支持时保持文件级同步。"""

        flags = getattr(os, "O_RDONLY", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _require_role(self, identity: SkillIdentity, role: str) -> None:
        """同时检查固定租户与操作角色。"""

        self._validate_identity_tenant(identity)
        if not identity.has_any_role("admin", role):
            raise SkillAuthorizationError("缺少角色 %s" % role)

    def _require_read(self, identity: SkillIdentity) -> None:
        """检查治理记录读取权限。"""

        self._validate_identity_tenant(identity)
        if not can_read_governed_skills(identity):
            raise SkillAuthorizationError("无权读取技能治理记录")

    def _require_evaluation_runner(self) -> EvaluationRunner:
        """要求发布链路使用应用启动时配置的可信评测运行器。"""

        runner = self.evaluation_runner
        if runner is None:
            raise SkillValidationError("当前服务仅支持提案，未配置可信评测运行器")
        self._clean_text(runner.runner_id, "runner_id", 256, True)
        self._clean_text(runner.runner_version, "runner_version", 256, True)
        return runner

    def _validate_identity_tenant(self, identity: SkillIdentity) -> None:
        """拒绝跨租户复用工作区技能服务。"""

        if not isinstance(identity, SkillIdentity):
            raise SkillAuthorizationError("identity 必须来自可信身份边界")
        if identity.tenant_id != self.tenant_id:
            raise SkillAuthorizationError("租户边界不匹配")

    @staticmethod
    def _assert_not_owner(
        identity: SkillIdentity, record: SkillVersion, message: str
    ) -> None:
        """强制候选所有者与验证、发布职责隔离。"""

        if identity.actor_user_id == record.owner_user_id:
            raise SkillAuthorizationError(message)

    @staticmethod
    def _validate_name(name: str) -> None:
        """限制技能名称为单目录安全标识符。"""

        if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
            raise SkillValidationError("name 必须是小写字母数字和连字符组成的安全标识符")

    @classmethod
    def _validate_text_tuple(cls, values: Tuple[str, ...], field_name: str) -> Tuple[str, ...]:
        """核验结构化字符串列表，避免投影格式被换行注入。"""

        if not isinstance(values, tuple) or not values:
            raise SkillValidationError("%s 必须是非空 tuple" % field_name)
        cleaned = tuple(cls._clean_text(value, field_name, 4096, True) for value in values)
        if len(set(cleaned)) != len(cleaned):
            raise SkillValidationError("%s 不能包含重复项" % field_name)
        return cleaned

    @staticmethod
    def _clean_text(value: str, field_name: str, maximum: int, single_line: bool) -> str:
        """清理并限制业务文本。"""

        if not isinstance(value, str) or not value.strip():
            raise SkillValidationError("%s 不能为空" % field_name)
        cleaned = value.strip()
        if len(cleaned) > maximum:
            raise SkillValidationError("%s 超过长度限制" % field_name)
        if "\x00" in cleaned or (single_line and ("\n" in cleaned or "\r" in cleaned)):
            raise SkillValidationError("%s 包含不允许的控制字符" % field_name)
        return cleaned

    @classmethod
    def _validate_lifecycle_input(
        cls, skill_id: str, version: int, reason: str, idempotency_key: str
    ) -> None:
        """核验拒绝和回滚的公共输入。"""

        cls._clean_text(skill_id, "skill_id", 128, True)
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise SkillValidationError("version 必须是正整数")
        cls._clean_text(reason, "reason", 2048, True)
        cls._clean_text(idempotency_key, "idempotency_key", 256, True)

    @classmethod
    def _validate_publish_input(
        cls, skill_id: str, version: int, evaluation_id: str, idempotency_key: str
    ) -> None:
        """核验发布命令的公共输入。"""

        cls._clean_text(skill_id, "skill_id", 128, True)
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise SkillValidationError("version 必须是正整数")
        cls._clean_text(evaluation_id, "evaluation_id", 128, True)
        cls._clean_text(idempotency_key, "idempotency_key", 256, True)

    @staticmethod
    def _percentile_95(values: Iterable[float]) -> float:
        """计算离散样本的 nearest-rank P95。"""

        ordered = sorted(float(value) for value in values)
        index = max(0, int(math.ceil(0.95 * len(ordered))) - 1)
        return ordered[index]

    @staticmethod
    def _sample_payload(sample: PairedSampleResult) -> Dict[str, object]:
        """生成配对样本的稳定序列化载荷。"""

        return {
            "sample_id": sample.sample_id,
            "baseline_success": sample.baseline_success,
            "candidate_success": sample.candidate_success,
            "baseline_latency_ms": sample.baseline_latency_ms,
            "candidate_latency_ms": sample.candidate_latency_ms,
        }

    @staticmethod
    def _policy_payload(policy: EvaluationPolicy) -> Dict[str, object]:
        """生成门禁策略的稳定序列化载荷。"""

        return {
            "minimum_sample_count": policy.minimum_sample_count,
            "max_candidate_p95_latency_ms": policy.max_candidate_p95_latency_ms,
            "max_latency_regression_ratio": policy.max_latency_regression_ratio,
        }
