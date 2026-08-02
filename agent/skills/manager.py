"""
Skill manager for managing skill lifecycle and operations.
"""

import os
import json
import sqlite3
import threading
from typing import Dict, List, Optional, Sequence, Tuple
from pathlib import Path
from common.log import logger
from agent.skills.types import Skill, SkillEntry, SkillSnapshot
from agent.skills.loader import SkillLoader
from agent.skills.formatter import format_skill_entries_for_prompt
from agent.skills.retrieval.contracts import ShadowCandidate

SKILLS_CONFIG_FILE = "skills_config.json"


class SkillManager:
    """Manages skills for an agent."""

    def __init__(
        self,
        builtin_dir: Optional[str] = None,
        custom_dir: Optional[str] = None,
        config: Optional[Dict] = None,
        tenant_id: Optional[str] = None,
        identity_context=None,
    ):
        """
        Initialize the skill manager.

        :param builtin_dir: Built-in skills directory (project root ``skills/``)
        :param custom_dir: Custom skills directory (workspace ``skills/``)
        :param config: Configuration dictionary
        """
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.builtin_dir = builtin_dir or os.path.join(project_root, 'skills')
        self.custom_dir = custom_dir or os.path.join(project_root, 'workspace', 'skills')
        self.config = config or {}
        self.tenant_id = tenant_id.strip() if isinstance(tenant_id, str) else None
        self.identity_context = identity_context
        self._skills_config_path = os.path.join(self.custom_dir, SKILLS_CONFIG_FILE)
        self._governed_repository = None
        self._governed_service = None
        self._governed_active_names = set()
        self._shadow_runtime = None
        self._runtime_lock = threading.RLock()

        # skills_config: full skill metadata keyed by name
        # { "web-fetch": {"name": ..., "description": ..., "source": ..., "enabled": true}, ... }
        self.skills_config: Dict[str, dict] = {}

        self.loader = SkillLoader()
        self.skills: Dict[str, SkillEntry] = {}

        # Load skills on initialization
        self.refresh_skills()

    def refresh_skills(self):
        """Reload all skills from builtin and custom directories, then sync config."""
        with self.production_injection_lock():
            loaded = self.loader.load_all_skills(
                builtin_dir=self.builtin_dir,
                custom_dir=self.custom_dir,
            )
            self.skills = self._enforce_governed_projections(loaded)
            self._sync_skills_config()
            logger.debug(f"SkillManager: Loaded {len(self.skills)} skills")

    def _governed_tenant_id(self, database_path: Path) -> Optional[str]:
        """读取事实库租户；显式租户缺失时只接受唯一租户。"""

        if self.tenant_id:
            return self.tenant_id
        uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
            rows = connection.execute(
                "SELECT DISTINCT tenant_id FROM governed_skill_versions"
            ).fetchall()
        tenant_ids = tuple(str(row[0]) for row in rows)
        if not tenant_ids:
            return None
        if len(tenant_ids) != 1:
            raise ValueError("技能治理事实库包含多个租户，必须显式指定 tenant_id")
        return tenant_ids[0]

    def _ensure_governed_service(self):
        """延迟创建投影核验服务，并复用其 SQLite 连接。"""

        with self.production_injection_lock():
            with self._runtime_lock:
                database_path = (
                    Path(self.custom_dir)
                    / ".system"
                    / "governed-skills.db"
                )
                if not database_path.is_file():
                    return None
                if self._governed_service is not None:
                    return self._governed_service
                tenant_id = self._governed_tenant_id(database_path)
                if tenant_id is None:
                    return None
                from agent.skills.governance import (
                    GovernedSkillRepository,
                    GovernedSkillService,
                )

                repository = GovernedSkillRepository(database_path)
                try:
                    service = GovernedSkillService(
                        repository, Path(self.custom_dir), tenant_id
                    )
                except Exception:
                    repository.close()
                    raise
                self._governed_repository = repository
                self._governed_service = service
                return service

    @staticmethod
    def _entry_path(entry: SkillEntry) -> str:
        """生成技能文件的 Windows 大小写不敏感规范路径。"""

        return os.path.normcase(os.path.realpath(os.path.abspath(entry.skill.file_path)))

    @staticmethod
    def _frontmatter_marks_governed(entry: SkillEntry) -> bool:
        """识别磁盘投影中的治理标记。"""

        value = entry.skill.frontmatter.get("governed", False)
        return value is True or str(value).strip().lower() == "true"

    def _enforce_governed_projections(
        self, loaded: Dict[str, SkillEntry]
    ) -> Dict[str, SkillEntry]:
        """仅加载与有效事实逐字节一致的治理技能投影。"""

        database_path = Path(self.custom_dir) / ".system" / "governed-skills.db"
        if not database_path.exists():
            self._governed_active_names = set()
            return loaded
        try:
            service = self._ensure_governed_service()
            if service is None:
                self._governed_active_names = set()
                return loaded
            active_records = service.repository.list_active_versions(
                service.tenant_id
            )
        except Exception as error:
            self._governed_active_names = set()
            logger.error(
                "[SkillManager] Governed skill state is unreadable; "
                "custom skills are disabled: %s",
                error,
            )
            return {
                name: entry
                for name, entry in loaded.items()
                if entry.skill.source != "custom"
            }

        self._governed_active_names = {record.name for record in active_records}
        filtered = {
            name: entry
            for name, entry in loaded.items()
            if not (
                entry.skill.source == "custom"
                and self._frontmatter_marks_governed(entry)
            )
        }
        loaded_entries = tuple(loaded.items())
        for record in active_records:
            try:
                projection_path = service._projection_path(record.name)
                projection_key = os.path.normcase(
                    os.path.realpath(os.path.abspath(projection_path))
                )
                matches = [
                    (name, entry)
                    for name, entry in loaded_entries
                    if entry.skill.source == "custom"
                    and self._entry_path(entry) == projection_key
                ]
                filtered.pop(record.name, None)
                for loaded_name, _entry in matches:
                    filtered.pop(loaded_name, None)
                expected = service.render_projection(record)
                if len(matches) != 1:
                    raise ValueError("有效技能投影没有唯一的加载结果")
                loaded_name, entry = matches[0]
                if loaded_name != record.name or entry.skill.name != record.name:
                    raise ValueError("有效技能投影名称与事实库不一致")
                if entry.skill.content != expected:
                    raise ValueError("加载快照与事实库不一致")
                if projection_path.read_bytes() != expected.encode("utf-8"):
                    raise ValueError("磁盘投影与事实库不一致")
                filtered[record.name] = entry
            except Exception as error:
                logger.error(
                    "[SkillManager] Governed skill '%s' failed projection "
                    "verification and was excluded: %s",
                    record.name,
                    error,
                )
        return filtered

    def close(self) -> None:
        """关闭技能治理读取连接。"""

        with self.production_injection_lock():
            with self._runtime_lock:
                if self._shadow_runtime is not None:
                    self._shadow_runtime.close()
                    self._shadow_runtime = None
                if self._governed_repository is not None:
                    self._governed_repository.close()
                    self._governed_repository = None
                    self._governed_service = None

    def get_shadow_runtime(self):
        """按需创建 active-only 技能影子检索运行时。"""

        with self.production_injection_lock():
            with self._runtime_lock:
                if self._shadow_runtime is not None:
                    return self._shadow_runtime
                service = self._ensure_governed_service()
                if service is None:
                    return None
                from agent.skills.retrieval import ActiveSkillShadowRuntime

                system_dir = Path(self.custom_dir) / ".system"
                runtime = ActiveSkillShadowRuntime(
                    governance_repository=service.repository,
                    index_path=system_dir / "skill-retrieval.db",
                    telemetry_path=system_dir / "skill-shadow.db",
                    tenant_id=service.tenant_id,
                )
                self._shadow_runtime = runtime
                return runtime

    def production_injection_lock(self):
        """返回生产注入复核使用的租户级技能根锁。"""

        from agent.skills.locks import skill_root_lock

        return skill_root_lock(self.custom_dir)

    def non_governed_skill_names(self) -> List[str]:
        """返回当前已加载的内置技能和非治理自定义技能名称。"""

        names = []
        for name, entry in self.skills.items():
            if self._is_governed_entry(name, entry):
                continue
            names.append(name)
        return names

    def set_identity_context(self, identity_context) -> None:
        """绑定可信调用身份，供所有提示词技能过滤路径复用。"""

        with self.production_injection_lock():
            self.identity_context = identity_context

    def _is_governed_entry(self, name: str, entry: SkillEntry) -> bool:
        """识别事实库、投影标记或持久化配置声明的治理技能。"""

        config = self.skills_config.get(name, {})
        return (
            name in self._governed_active_names
            or self._frontmatter_marks_governed(entry)
            or config.get("managed_by") == "governance"
        )

    def _governed_prompt_read_allowed(self) -> bool:
        """仅允许已绑定且租户、读取角色均匹配的可信身份。"""

        identity = self.identity_context
        if identity is None:
            return False
        from agent.skills.governance import can_read_governed_skills

        tenant_id = self.tenant_id or getattr(
            self._governed_service, "tenant_id", None
        )
        if tenant_id and getattr(identity, "tenant_id", None) != tenant_id:
            return False
        return can_read_governed_skills(identity)

    def _filter_governed_prompt_entries(
        self, entries: Sequence[SkillEntry]
    ) -> List[SkillEntry]:
        """无治理读取权时从提示词候选中移除全部治理技能。"""

        if self._governed_prompt_read_allowed():
            return list(entries)
        return [
            entry
            for entry in entries
            if not self._is_governed_entry(entry.skill.name, entry)
        ]

    def explicit_non_governed_skill_filter(
        self, requested_names: Sequence[str]
    ) -> List[str]:
        """按调用者顺序保留其明确请求的非治理技能。"""

        requested = self._normalize_skill_filter(list(requested_names)) or []
        non_governed = set(self.non_governed_skill_names())
        allowed = []
        for name in requested:
            if name in non_governed and name not in allowed:
                allowed.append(name)
        return allowed

    def verify_explicit_skill_filter(
        self,
        identity,
        requested_names: Sequence[str],
        model_id: str,
    ) -> Tuple[List[str], Tuple[ShadowCandidate, ...]]:
        """复核显式集合中的治理技能，且不扩张非治理技能集合。"""

        with self.production_injection_lock():
            return self._verify_explicit_skill_filter_locked(
                identity, requested_names, model_id
            )

    def _verify_explicit_skill_filter_locked(
        self,
        identity,
        requested_names: Sequence[str],
        model_id: str,
    ) -> Tuple[List[str], Tuple[ShadowCandidate, ...]]:
        """在技能根锁内执行显式治理技能复核。"""

        requested = self._normalize_skill_filter(list(requested_names)) or []
        requested = list(dict.fromkeys(requested))
        allowed_non_governed = set(
            self.explicit_non_governed_skill_filter(requested)
        )
        service = self._ensure_governed_service()
        active_by_name = {}
        if service is not None:
            try:
                active_by_name = {
                    record.name: record
                    for record in service.repository.list_active_versions(
                        service.tenant_id
                    )
                }
            except Exception as error:
                logger.warning(
                    "[SkillManager] Explicit governed skill lookup failed: %s",
                    type(error).__name__,
                )

        candidates = []
        normalized_model = str(model_id or "").strip()
        for name in requested:
            record = active_by_name.get(name)
            if record is None:
                continue
            candidates.append(
                ShadowCandidate(
                    rank=len(candidates) + 1,
                    skill_id=record.skill_id,
                    version=record.version,
                    content_hash=record.content_hash,
                    score=0.0,
                    bm25_score=0.0,
                    query_coverage=0.0,
                    model_compatible=(
                        normalized_model in record.model_compatibility
                    ),
                )
            )

        try:
            verified, verified_names = self.verify_production_candidates(
                identity, candidates, normalized_model
            )
        except Exception as error:
            logger.warning(
                "[SkillManager] Explicit governed skill verification failed: %s",
                type(error).__name__,
            )
            verified, verified_names = (), ()
        verified_name_set = set(verified_names)
        allowed = [
            name
            for name in requested
            if name in allowed_non_governed or name in verified_name_set
        ]
        return allowed, verified

    def verify_production_candidates(
        self,
        identity,
        candidates: Sequence[ShadowCandidate],
        model_id: str,
    ) -> Tuple[Tuple[ShadowCandidate, ...], Tuple[str, ...]]:
        """复核候选事实、模型、投影字节和运行时可用性。"""

        with self.production_injection_lock():
            return self._verify_production_candidates_locked(
                identity, candidates, model_id
            )

    def _verify_production_candidates_locked(
        self,
        identity,
        candidates: Sequence[ShadowCandidate],
        model_id: str,
    ) -> Tuple[Tuple[ShadowCandidate, ...], Tuple[str, ...]]:
        """在技能根锁内执行生产候选复核。"""

        service = self._ensure_governed_service()
        if service is None:
            if candidates:
                raise RuntimeError("治理技能事实库不可用")
            return (), ()
        normalized_model = str(model_id or "").strip()
        if not normalized_model:
            raise ValueError("model_id 不能为空")
        if getattr(identity, "tenant_id", None) != service.tenant_id:
            raise ValueError("生产技能注入身份租户不匹配")

        verified_candidates = []
        verified_names = []
        for candidate in candidates:
            try:
                if not bool(getattr(candidate, "model_compatible", False)):
                    continue
                skill_id = str(getattr(candidate, "skill_id"))
                version = int(getattr(candidate, "version"))
                content_hash = str(getattr(candidate, "content_hash"))
                fact = service.repository.read_version(
                    service.tenant_id, skill_id, version
                )
                from agent.skills.governance import SkillStatus

                if fact.status is not SkillStatus.ACTIVE:
                    continue
                if fact.content_hash != content_hash:
                    continue
                if normalized_model not in fact.model_compatibility:
                    continue
                active = service.verify_projection(identity, fact.name)
                if (
                    active.skill_id != skill_id
                    or active.version != version
                    or active.content_hash != content_hash
                ):
                    continue
                entry = self._get_production_eligible_skill(fact.name)
                if entry is None:
                    continue
                expected = service.render_projection(active)
                projection_path = service._projection_path(active.name)
                if entry.skill.content != expected:
                    continue
                if self._entry_path(entry) != self._normalized_path(
                    projection_path
                ):
                    continue
                if projection_path.read_bytes() != expected.encode("utf-8"):
                    continue
            except Exception as error:
                logger.warning(
                    "[SkillManager] Production candidate verification failed: %s",
                    type(error).__name__,
                )
                continue
            verified_candidates.append(candidate)
            verified_names.append(fact.name)
        return tuple(verified_candidates), tuple(verified_names)

    @staticmethod
    def _normalized_path(path: Path) -> str:
        """生成投影路径的 Windows 大小写不敏感规范形式。"""

        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    def production_skill_filter(
        self, selected_governed_names: Sequence[str]
    ) -> List[str]:
        """只替换治理技能子集，保留内置技能和非治理技能。"""

        allowed = self.non_governed_skill_names()
        for name in selected_governed_names:
            if name in self._governed_active_names and name not in allowed:
                allowed.append(name)
        return allowed

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # skills_config.json management
    # ------------------------------------------------------------------
    def _load_skills_config(self) -> Dict[str, dict]:
        """Load skills_config.json from custom_dir. Returns empty dict if not found."""
        if not os.path.exists(self._skills_config_path):
            return {}
        try:
            with open(self._skills_config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning(f"[SkillManager] Failed to load {SKILLS_CONFIG_FILE}: {e}")
        return {}

    def _save_skills_config(self):
        """Persist skills_config to custom_dir/skills_config.json."""
        os.makedirs(self.custom_dir, exist_ok=True)
        try:
            with open(self._skills_config_path, "w", encoding="utf-8") as f:
                json.dump(self.skills_config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[SkillManager] Failed to save {SKILLS_CONFIG_FILE}: {e}")

    def _sync_skills_config(self):
        """
        Merge directory-scanned skills with the persisted config file.

        - New skills: use metadata.default_enabled as initial enabled state.
        - Existing skills: preserve their persisted enabled state.
        - Skills that no longer exist on disk are removed.
        - name/description/source are always refreshed from the latest scan.
        """
        saved = self._load_skills_config()
        merged: Dict[str, dict] = {}

        for name, entry in self.skills.items():
            skill = entry.skill
            prev = saved.get(name, {})
            category = prev.get("category", "skill")

            if name in self._governed_active_names:
                enabled = True
            elif name in saved:
                enabled = prev.get("enabled", True)
            else:
                enabled = entry.metadata.default_enabled if entry.metadata else True

            entry_dict = {
                "name": name,
                "description": skill.description,
                "source": prev.get("source") or skill.source,
                "enabled": enabled,
                "category": category,
            }
            if name in self._governed_active_names:
                entry_dict["managed_by"] = "governance"
            display_name = prev.get("display_name")
            if display_name:
                entry_dict["display_name"] = display_name
            merged[name] = entry_dict

        self.skills_config = merged
        self._save_skills_config()

    def is_skill_enabled(self, name: str) -> bool:
        """
        Check if a skill is enabled according to skills_config.

        :param name: skill name
        :return: True if enabled (default True if not in config)
        """
        entry = self.skills_config.get(name)
        if entry is None:
            return True
        return entry.get("enabled", True)

    def set_skill_enabled(self, name: str, enabled: bool):
        """
        Set a skill's enabled state and persist.

        :param name: skill name
        :param enabled: True to enable, False to disable
        """
        if name not in self.skills_config:
            raise ValueError(f"skill '{name}' not found in config")
        self.skills_config[name]["enabled"] = enabled
        self._save_skills_config()

    def get_skills_config(self) -> Dict[str, dict]:
        """
        Return the full skills_config dict (for query API).

        :return: copy of skills_config
        """
        return dict(self.skills_config)
    
    def get_skill(self, name: str) -> Optional[SkillEntry]:
        """
        Get a skill by name.
        
        :param name: Skill name
        :return: SkillEntry or None if not found
        """
        return self.skills.get(name)

    def get_eligible_skill(self, name: str) -> Optional[SkillEntry]:
        """只返回当前可进入模型上下文的已启用技能。"""

        entries = self.filter_skills(skill_filter=[name], include_disabled=False)
        for entry in entries:
            if (
                entry.skill.name == name
                and not entry.skill.disable_model_invocation
            ):
                return entry
        return None

    def _get_production_eligible_skill(
        self, name: str
    ) -> Optional[SkillEntry]:
        """在调用身份已单独鉴权后复核运行时可用性。"""

        from agent.skills.config import should_include_skill
        from config import conf

        entry = self.skills.get(name)
        if entry is None:
            return None
        if not should_include_skill(entry, self.config):
            return None
        if not self.is_skill_enabled(name):
            return None
        if not conf().get("knowledge", True) and name == "knowledge-wiki":
            return None
        if entry.skill.disable_model_invocation:
            return None
        return entry
    
    def list_skills(self) -> List[SkillEntry]:
        """
        Get all loaded skills.
        
        :return: List of all skill entries
        """
        return list(self.skills.values())
    
    @staticmethod
    def _normalize_skill_filter(skill_filter: Optional[List[str]]) -> Optional[List[str]]:
        """Normalize a skill_filter list into a flat list of stripped names."""
        if skill_filter is None:
            return None
        normalized = []
        for item in skill_filter:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    normalized.append(name)
            elif isinstance(item, list):
                for subitem in item:
                    if isinstance(subitem, str):
                        name = subitem.strip()
                        if name:
                            normalized.append(name)
        return normalized

    def filter_skills(
        self,
        skill_filter: Optional[List[str]] = None,
        include_disabled: bool = False,
    ) -> List[SkillEntry]:
        """
        Filter skills that are eligible (enabled + requirements met).

        :param skill_filter: List of skill names to include (None = all)
        :param include_disabled: Whether to include disabled skills
        :return: Filtered list of eligible skill entries
        """
        from agent.skills.config import should_include_skill

        entries = list(self.skills.values())

        entries = [e for e in entries if should_include_skill(e, self.config)]

        entries = self._filter_governed_prompt_entries(entries)

        normalized = self._normalize_skill_filter(skill_filter)
        if normalized is not None:
            entries = [e for e in entries if e.skill.name in normalized]

        if not include_disabled:
            entries = [e for e in entries if self.is_skill_enabled(e.skill.name)]

        from config import conf
        if not conf().get("knowledge", True):
            entries = [e for e in entries if e.skill.name != "knowledge-wiki"]

        return entries

    def filter_unavailable_skills(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> tuple:
        """
        Find skills that are enabled but have unmet requirements.

        :param skill_filter: Optional list of skill names to include
        :return: Tuple of (entries, missing_map) where missing_map maps
                 skill name to its missing requirements dict
        """
        from agent.skills.config import should_include_skill, get_missing_requirements

        entries = list(self.skills.values())

        # Only enabled skills
        entries = [e for e in entries if self.is_skill_enabled(e.skill.name)]

        entries = self._filter_governed_prompt_entries(entries)

        normalized = self._normalize_skill_filter(skill_filter)
        if normalized is not None:
            entries = [e for e in entries if e.skill.name in normalized]

        # Keep only those that fail should_include_skill (requirements not met)
        unavailable = []
        missing_map: Dict[str, dict] = {}
        for e in entries:
            if not should_include_skill(e, self.config):
                missing = get_missing_requirements(e)
                if missing:
                    unavailable.append(e)
                    missing_map[e.skill.name] = missing

        return unavailable, missing_map

    def build_skills_prompt(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> str:
        """在身份不可变的治理锁内构建技能提示词。"""

        with self.production_injection_lock():
            return self._build_skills_prompt_locked(skill_filter)

    def _build_skills_prompt_locked(
        self,
        skill_filter: Optional[List[str]] = None,
    ) -> str:
        """
        Build a formatted prompt containing available skills
        and brief hints for unavailable ones.

        :param skill_filter: Optional list of skill names to include
        :return: Formatted skills prompt
        """
        from common.log import logger
        from agent.skills.formatter import format_unavailable_skills_for_prompt

        eligible = self.filter_skills(skill_filter=skill_filter, include_disabled=False)
        logger.debug(f"[SkillManager] Eligible: {len(eligible)} skills (total: {len(self.skills)})")
        if eligible:
            skill_names = [e.skill.name for e in eligible]
            logger.debug(f"[SkillManager] Eligible skills: {skill_names}")

        result = format_skill_entries_for_prompt(eligible)

        unavailable, missing_map = self.filter_unavailable_skills(skill_filter=skill_filter)
        if unavailable:
            unavailable_names = [e.skill.name for e in unavailable]
            logger.debug(f"[SkillManager] Unavailable skills (setup needed): {unavailable_names}")
            result += format_unavailable_skills_for_prompt(unavailable, missing_map)

        logger.debug(f"[SkillManager] Generated prompt length: {len(result)}")
        return result
    
    def build_skill_snapshot(
        self,
        skill_filter: Optional[List[str]] = None,
        version: Optional[int] = None,
    ) -> SkillSnapshot:
        """在身份不可变的治理锁内构建技能运行快照。"""

        with self.production_injection_lock():
            return self._build_skill_snapshot_locked(
                skill_filter=skill_filter, version=version
            )

    def _build_skill_snapshot_locked(
        self,
        skill_filter: Optional[List[str]] = None,
        version: Optional[int] = None,
    ) -> SkillSnapshot:
        """
        Build a snapshot of skills for a specific run.
        
        :param skill_filter: Optional list of skill names to include
        :param version: Optional version number for the snapshot
        :return: SkillSnapshot
        """
        entries = self.filter_skills(skill_filter=skill_filter, include_disabled=False)
        prompt = format_skill_entries_for_prompt(entries)
        
        skills_info = []
        resolved_skills = []
        
        for entry in entries:
            skills_info.append({
                'name': entry.skill.name,
                'primary_env': entry.metadata.primary_env if entry.metadata else None,
            })
            resolved_skills.append(entry.skill)
        
        return SkillSnapshot(
            prompt=prompt,
            skills=skills_info,
            resolved_skills=resolved_skills,
            version=version,
        )
    
    def sync_skills_to_workspace(self, target_workspace_dir: str):
        """
        Sync all loaded skills to a target workspace directory.
        
        This is useful for sandbox environments where skills need to be copied.
        
        :param target_workspace_dir: Target workspace directory
        """
        import shutil
        
        target_skills_dir = os.path.join(target_workspace_dir, 'skills')
        
        # Remove existing skills directory
        if os.path.exists(target_skills_dir):
            shutil.rmtree(target_skills_dir)
        
        # Create new skills directory
        os.makedirs(target_skills_dir, exist_ok=True)
        
        # Copy each skill
        for entry in self.skills.values():
            skill_name = entry.skill.name
            source_dir = entry.skill.base_dir
            target_dir = os.path.join(target_skills_dir, skill_name)
            
            try:
                shutil.copytree(source_dir, target_dir)
                logger.debug(f"Synced skill '{skill_name}' to {target_dir}")
            except Exception as e:
                logger.warning(f"Failed to sync skill '{skill_name}': {e}")
        
        logger.info(f"Synced {len(self.skills)} skills to {target_skills_dir}")
    
    def get_skill_by_key(self, skill_key: str) -> Optional[SkillEntry]:
        """
        Get a skill by its skill key (which may differ from name).
        
        :param skill_key: Skill key to look up
        :return: SkillEntry or None
        """
        for entry in self.skills.values():
            if entry.metadata and entry.metadata.skill_key == skill_key:
                return entry
            if entry.skill.name == skill_key:
                return entry
        return None
