"""识别只能由治理记忆运行时维护的路径。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from common.path_safety import is_link_or_reparse_point


def _normalized(path: str) -> str:
    """解析符号链接并统一 Windows 大小写。"""

    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _lexical_normalized(path: str) -> str:
    """保留路径命名空间，避免符号链接解析后丢失受管目录边界。"""

    return os.path.normcase(os.path.abspath(path))


def _is_same_or_child(path: str, directory: str) -> bool:
    """判断路径是否等于目录或位于目录内。"""

    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def _has_governed_frontmatter(skill_dir: Path) -> bool:
    """读取技能投影头部，识别事实库投影标记。"""

    projection = skill_dir / "SKILL.md"
    if not projection.is_file() or is_link_or_reparse_point(projection):
        return False
    try:
        with projection.open("r", encoding="utf-8") as handle:
            prefix = handle.read(65536)
        from agent.skills.frontmatter import parse_frontmatter

        value = parse_frontmatter(prefix).get("governed", False)
    except (OSError, UnicodeError, ValueError):
        return False
    return value is True or str(value).strip().lower() == "true"


def _database_marks_skill_active(skills_dir: Path, name: str) -> bool:
    """从任一技能治理事实库读取同名有效状态，读取异常时安全拒绝。"""

    system_dir = skills_dir / ".system"
    if not system_dir.exists():
        return False
    if is_link_or_reparse_point(system_dir):
        return True
    try:
        databases = tuple(system_dir.glob("*.db"))
    except OSError:
        return True
    for database in databases:
        if is_link_or_reparse_point(database):
            return True
        connection = None
        try:
            connection = sqlite3.connect(str(database), timeout=0.25)
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                """
                SELECT 1
                FROM governed_skill_versions AS v
                WHERE v.name = ? COLLATE NOCASE
                  AND (SELECT s.status
                       FROM governed_skill_state_events AS s
                       WHERE s.tenant_id = v.tenant_id
                         AND s.skill_id = v.skill_id
                         AND s.version = v.version
                       ORDER BY s.sequence DESC LIMIT 1) = 'active'
                LIMIT 1
                """,
                (name,),
            ).fetchone()
            if row is not None:
                return True
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error).lower():
                return True
        except sqlite3.DatabaseError:
            return True
        finally:
            if connection is not None:
                connection.close()
    return False


def is_governed_skill_directory(skills_dir: str, name: str) -> bool:
    """判断自定义技能目录是否由治理事实库拥有。"""

    root = Path(os.path.abspath(skills_dir))
    normalized = str(name or "").replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts or parts[0].casefold() == ".system":
        return True
    current = root
    logical_parts = []
    for part in parts:
        current = current / part
        logical_parts.append(part)
        if is_link_or_reparse_point(current):
            return True
        logical_name = "/".join(logical_parts)
        if _has_governed_frontmatter(current) or _database_marks_skill_active(
            root, logical_name
        ):
            return True
    return False


def is_governed_skill_private_path(path: str, workspace: str) -> bool:
    """识别技能治理数据库、事务日志及其 SQLite 辅助文件。"""

    lexical_path = _lexical_normalized(path)
    lexical_system_dir = _lexical_normalized(
        os.path.join(workspace, "skills", ".system")
    )
    real_path = _normalized(path)
    real_system_dir = _normalized(os.path.join(workspace, "skills", ".system"))
    return _is_same_or_child(
        lexical_path, lexical_system_dir
    ) or _is_same_or_child(real_path, real_system_dir)


def is_machine_managed_skill_path(path: str, workspace: str) -> bool:
    """识别通用写入工具不得改写的技能治理私有区和有效技能。"""

    if is_governed_skill_private_path(path, workspace):
        return True
    config_pairs = (
        (
            _lexical_normalized(path),
            _lexical_normalized(
                os.path.join(workspace, "skills", "skills_config.json")
            ),
        ),
        (
            _normalized(path),
            _normalized(os.path.join(workspace, "skills", "skills_config.json")),
        ),
    )
    if any(candidate == config_path for candidate, config_path in config_pairs):
        return True
    candidate_pairs = (
        (
            _lexical_normalized(path),
            _lexical_normalized(os.path.join(workspace, "skills")),
        ),
        (
            _normalized(path),
            _normalized(os.path.join(workspace, "skills")),
        ),
    )
    for candidate, skills_root in candidate_pairs:
        if not _is_same_or_child(candidate, skills_root) or candidate == skills_root:
            continue
        try:
            relative = os.path.relpath(candidate, skills_root)
        except ValueError:
            continue
        parts = Path(relative).parts
        if not parts:
            continue
        if parts[0] == ".system":
            return True
        if is_governed_skill_directory(skills_root, parts[0]):
            return True
    return False


def is_governed_private_path(path: str, workspace: str) -> bool:
    """识别包含记忆或知识事实数据库的非公开路径。"""

    real_path = _normalized(path)
    real_workspace = _normalized(workspace)
    projection_dir = _normalized(
        os.path.join(real_workspace, "memory", ".governed")
    )
    # The whole long-term directory is machine-owned: index.db,
    # governance.db, retrieval-v2.db and their -wal/-shm sidecars all hold
    # governed plaintext and must never be readable through generic tools.
    # (A WAL checkpoint race leaked retrieval-v2.db-wal content into generic
    # search on CI; guard the directory, not individual filenames.)
    long_term_dir = _normalized(
        os.path.join(real_workspace, "memory", "long-term")
    )
    knowledge_system_dir = _normalized(
        os.path.join(real_workspace, "knowledge", ".system")
    )
    return (
        _is_same_or_child(real_path, projection_dir)
        or _is_same_or_child(real_path, long_term_dir)
        or _is_same_or_child(real_path, knowledge_system_dir)
        or is_governed_skill_private_path(path, workspace)
    )


def is_machine_managed_memory_path(path: str, workspace: str) -> bool:
    """识别禁止通用文件工具改写的全部记忆路径。"""

    real_path = _normalized(path)
    legacy_index = _normalized(os.path.join(workspace, "MEMORY.md"))
    return real_path == legacy_index or is_governed_private_path(path, workspace)


def is_machine_managed_knowledge_path(path: str, workspace: str) -> bool:
    """识别只能通过知识生命周期工具访问的知识目录。"""

    lexical_path = _lexical_normalized(path)
    lexical_knowledge_dir = _lexical_normalized(
        os.path.join(workspace, "knowledge")
    )
    real_path = _normalized(path)
    knowledge_dir = _normalized(os.path.join(workspace, "knowledge"))
    return _is_same_or_child(
        lexical_path, lexical_knowledge_dir
    ) or _is_same_or_child(real_path, knowledge_dir)


def is_machine_managed_path(path: str, workspace: str) -> bool:
    """统一识别通用文件工具不可改写的机器管理路径。"""

    return is_machine_managed_memory_path(
        path, workspace
    ) or is_machine_managed_knowledge_path(
        path, workspace
    ) or is_machine_managed_skill_path(path, workspace)
