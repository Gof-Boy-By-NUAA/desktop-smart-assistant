"""把会话经验提交为未发布的结构化技能候选。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Tuple

from agent.skills.governance import (
    GovernedSkillService,
    SkillIdentity,
    SkillProposal,
    SourceEvidence,
)
from agent.tools.base_tool import BaseTool, ToolResult


class SkillProposeTool(BaseTool):
    """只创建候选，不评测、不发布，也不生成 SKILL.md。"""

    name = "skill_propose"
    description = (
        "Submit a complete structured skill candidate to the governed work-manual "
        "repository. The candidate stays inactive until an independent trusted "
        "evaluation and publisher approve it. This tool never writes SKILL.md."
    )
    params = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Safe lowercase skill name using letters, digits, and hyphens.",
            },
            "description": {
                "type": "string",
                "description": "What the skill does and when it is useful.",
            },
            "applicability": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete task or state conditions that trigger the skill.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered executable steps.",
            },
            "validation_rules": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Checks that prove each important step stayed on track.",
            },
            "contraindications": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Cases where the skill must not be applied.",
            },
        },
        "required": [
            "name",
            "description",
            "applicability",
            "steps",
            "validation_rules",
            "contraindications",
        ],
    }

    def __init__(
        self,
        service: GovernedSkillService,
        identity: SkillIdentity,
        *,
        source_type: str = "conversation-transcript",
        source_ref: str,
        source_payload: bytes,
        model_id: str,
        protected_skills: Iterable[str] = (),
    ):
        if not isinstance(source_payload, bytes) or not source_payload:
            raise ValueError("source_payload 必须是非空 bytes")
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("source_ref 不能为空")
        if not isinstance(source_type, str) or not source_type.strip():
            raise ValueError("source_type 不能为空")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id 不能为空")
        self.service = service
        self.identity = identity
        self.source_type = source_type.strip()
        self.source_ref = source_ref.strip()
        self.source_payload = source_payload
        self.model_id = model_id.strip()
        self.protected_skills = frozenset(str(name) for name in protected_skills)
        self.proposals = []

    def execute(self, params: dict) -> ToolResult:
        try:
            name = str(params.get("name") or "").strip()
            if name in self.protected_skills:
                return ToolResult.fail("Error: built-in skills cannot be proposed for replacement")

            applicability = self._string_tuple(params.get("applicability"), "applicability")
            steps = self._string_tuple(params.get("steps"), "steps")
            validation_rules = self._string_tuple(
                params.get("validation_rules"), "validation_rules"
            )
            contraindications = self._string_tuple(
                params.get("contraindications"), "contraindications"
            )
            source_hash = hashlib.sha256(self.source_payload).hexdigest()
            sources = [
                SourceEvidence(
                    source_type=self.source_type,
                    source_ref=self.source_ref,
                    payload=self.source_payload,
                    sha256=source_hash,
                )
            ]
            existing = self._existing_skill_source(name)
            if existing is not None:
                sources.append(existing)

            fingerprint = {
                "name": name,
                "description": params.get("description"),
                "applicability": applicability,
                "steps": steps,
                "validation_rules": validation_rules,
                "contraindications": contraindications,
                "model_id": self.model_id,
                "sources": [source.sha256 for source in sources],
            }
            stable = json.dumps(
                fingerprint,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            existing_candidates = {
                (record.skill_id, record.version)
                for record in self.service.list_candidates(self.identity)
            }
            proposal = SkillProposal(
                name=name,
                description=str(params.get("description") or ""),
                applicability=applicability,
                steps=steps,
                validation_rules=validation_rules,
                contraindications=contraindications,
                model_compatibility=(self.model_id,),
                sources=tuple(sources),
                idempotency_key="evolution-%s" % hashlib.sha256(stable).hexdigest(),
            )
            record = self.service.propose(self.identity, proposal)
            if (
                record.status.value == "candidate"
                and (record.skill_id, record.version) not in existing_candidates
            ):
                self.proposals.append(record)
            return ToolResult.success(
                {
                    "skill_id": record.skill_id,
                    "name": record.name,
                    "version": record.version,
                    "status": record.status.value,
                    "published": False,
                    "message": "Candidate recorded; it is not active and no SKILL.md was written.",
                }
            )
        except Exception as error:
            return ToolResult.fail("Error proposing governed skill candidate: %s" % error)

    @staticmethod
    def _string_tuple(value, field_name: str) -> Tuple[str, ...]:
        """拒绝字符串冒充数组，具体文本约束交给治理服务统一执行。"""

        if not isinstance(value, (list, tuple)):
            raise ValueError("%s 必须是字符串数组" % field_name)
        return tuple(value)

    def _existing_skill_source(self, name: str):
        """把既有自定义技能的当前字节绑定到首次接管提案。"""

        skills_dir = self.service.skills_dir
        lexical_path = skills_dir / name / "SKILL.md"
        if lexical_path.parent.is_symlink() or lexical_path.is_symlink():
            raise ValueError("既有技能路径不能包含符号链接")
        path = lexical_path.resolve(strict=False)
        try:
            path.relative_to(skills_dir)
        except ValueError as error:
            raise ValueError("技能路径超出受治理目录") from error
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError("既有技能必须是普通文件")
        payload = path.read_bytes()
        return SourceEvidence(
            source_type="existing-skill",
            source_ref=str(path),
            payload=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def close(self):
        """释放该次后台复盘独占的 SQLite 连接。"""

        self.service.repository.close()
