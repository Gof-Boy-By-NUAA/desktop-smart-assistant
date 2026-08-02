"""
Skill service for handling skill CRUD operations.

This service provides a unified interface for managing skills, which can be
called from the cloud control client (LinkAI), the local web console, or any
other management entry point.
"""

import os
import re
import shutil
import stat
import zipfile
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from common.log import logger
from agent.skills.types import Skill, SkillEntry
from agent.skills.locks import skill_root_lock
from agent.skills.manager import SkillManager
from common.path_safety import is_link_or_reparse_point
from agent.tools.utils.governed_memory import (
    is_governed_skill_directory,
)

try:
    import requests
except ImportError:
    requests = None


class SkillService:
    """
    High-level service for skill lifecycle management.
    Wraps SkillManager and provides network-aware operations such as
    downloading skill files from remote URLs.
    """

    def __init__(self, skill_manager: SkillManager):
        """
        :param skill_manager: The SkillManager instance to operate on
        """
        self.manager = skill_manager
        self._mutation_lock = skill_root_lock(self.manager.custom_dir)

    @staticmethod
    def _relative_parts(value: str, label: str) -> tuple[str, ...]:
        """把可移植相对路径解析为不含穿越成分的路径片段。"""

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} is required")
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith("/")
            or normalized.startswith("//")
            or re.match(r"^[A-Za-z]:", normalized)
        ):
            raise ValueError(f"invalid {label} (path traversal detected): {value!r}")
        parts = tuple(normalized.split("/"))
        if any(
            not part
            or part in (".", "..")
            or part.endswith((" ", "."))
            for part in parts
        ):
            raise ValueError(f"invalid {label} (path traversal detected): {value!r}")
        return parts

    @staticmethod
    def _assert_no_reparse_components(root: Path, target: Path, label: str) -> None:
        """拒绝根目录至目标之间已有的符号链接或 Windows 重解析点。"""

        root = Path(os.path.abspath(os.fspath(root)))
        target = Path(os.path.abspath(os.fspath(target)))
        try:
            relative = target.relative_to(root)
        except ValueError as error:
            raise ValueError(f"{label} resolves outside the skills directory") from error
        current = root
        if is_link_or_reparse_point(current):
            raise ValueError(f"{label} contains a symbolic link or reparse point")
        for part in relative.parts:
            current = current / part
            if is_link_or_reparse_point(current):
                raise ValueError(f"{label} contains a symbolic link or reparse point")

    def _safe_skill_dir(self, name: str) -> str:
        """Derive and validate the skill directory path.

        Ensures the resolved path stays within the custom_dir root,
        preventing path traversal via names like ``../escaped``.

        :raises ValueError: if the name would escape the skills root.
        """
        normalized_parts = self._relative_parts(name, "skill name")
        first_part = normalized_parts[0].casefold()
        if first_part == ".system":
            raise ValueError("skills/.system is reserved for governed skill state")
        if len(normalized_parts) == 1 and first_part == "skills_config.json":
            raise ValueError("skills_config.json is reserved for machine-managed state")
        lexical_root = Path(os.path.abspath(self.manager.custom_dir))
        lexical_target = lexical_root.joinpath(*normalized_parts)
        self._assert_no_reparse_components(
            lexical_root, lexical_target, "skill path"
        )
        skill_dir = os.path.realpath(str(lexical_target))
        root = os.path.realpath(str(lexical_root))
        if not skill_dir.startswith(root + os.sep) and skill_dir != root:
            raise ValueError(
                f"skill name {name!r} resolves outside the skills directory"
            )
        return skill_dir

    def _safe_file_destination(
        self, root: Path, relative_path: str, label: str = "skill file path"
    ) -> str:
        """解析下载或解压目标，并保证目标始终位于临时目录中。"""

        parts = self._relative_parts(relative_path, label)
        root = Path(os.path.abspath(os.fspath(root)))
        target = root.joinpath(*parts)
        self._assert_no_reparse_components(root, target, label)
        resolved_root = os.path.realpath(str(root))
        resolved_target = os.path.realpath(str(target))
        if os.path.commonpath([resolved_root, resolved_target]) != resolved_root:
            raise ValueError(f"invalid {label} (path traversal detected)")
        return str(target)

    def _assert_skill_mutable(self, name: str) -> None:
        """拒绝控制台安装或删除治理事实库拥有的技能目录。"""

        self._safe_skill_dir(name)
        if is_governed_skill_directory(self.manager.custom_dir, name):
            raise ValueError(
                "governed skill must be changed through the governance lifecycle"
            )

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------
    def query(self) -> List[dict]:
        """
        Query all skills and return a serialisable list.
        Reads from skills_config.json (refreshes from disk if needed).

        :return: list of skill info dicts
        """
        with self._mutation_lock:
            self.manager.refresh_skills()
            config = self.manager.get_skills_config()
            result = list(config.values())
        logger.info(f"[SkillService] query: {len(result)} skills found")
        return result

    # ------------------------------------------------------------------
    # add / install
    # ------------------------------------------------------------------
    def add(self, payload: dict) -> None:
        """
        Add (install) a skill from a remote payload.

        Supported payload types:

        1. ``type: "url"`` – download individual files::

            {
                "name": "web_search",
                "type": "url",
                "enabled": true,
                "files": [
                    {"url": "https://...", "path": "README.md"},
                    {"url": "https://...", "path": "scripts/main.py"}
                ]
            }

        2. ``type: "package"`` – download a zip archive and extract::

            {
                "name": "plugin-custom-tool",
                "type": "package",
                "category": "skills",
                "enabled": true,
                "files": [{"url": "https://cdn.example.com/skills/custom-tool.zip"}]
            }

        :param payload: skill add payload from server
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._assert_skill_mutable(name)

        payload_type = payload.get("type", "url")

        if payload_type == "package":
            self._add_package(name, payload)
        else:
            self._add_url(name, payload)

    def _add_url(self, name: str, payload: dict) -> None:
        """Install a skill by downloading individual files."""
        self._assert_skill_mutable(name)
        files = payload.get("files", [])
        if not files:
            raise ValueError("skill files list is empty")

        skills_root = os.path.realpath(self.manager.custom_dir)
        os.makedirs(skills_root, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".skill-url-install-", dir=skills_root
        ) as tmp_dir:
            staged_dir = Path(tmp_dir) / "payload"
            staged_dir.mkdir()
            for file_info in files:
                url = file_info.get("url")
                rel_path = file_info.get("path")
                if not url or not rel_path:
                    logger.warning(f"[SkillService] add: skip invalid file entry {file_info}")
                    continue
                dest = self._safe_file_destination(staged_dir, rel_path)
                self._download_file(url, dest)
            self._finalize_install(name, staged_dir, payload.get("category"))

        logger.info(f"[SkillService] add: skill '{name}' installed via url ({len(files)} files)")

    def _add_package(self, name: str, payload: dict) -> None:
        """
        Install a skill by downloading a zip archive and extracting it.

        If the archive contains a single top-level directory, that directory
        is used as the skill folder directly; otherwise a new directory named
        after the skill is created to hold the extracted contents.
        """
        self._assert_skill_mutable(name)
        files = payload.get("files", [])
        if not files or not files[0].get("url"):
            raise ValueError("package url is required")

        url = files[0]["url"]
        skills_root = os.path.realpath(self.manager.custom_dir)
        os.makedirs(skills_root, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".skill-package-install-", dir=skills_root
        ) as tmp_dir:
            zip_path = os.path.join(tmp_dir, "package.zip")
            self._download_file(url, zip_path)

            if not zipfile.is_zipfile(zip_path):
                raise ValueError(f"downloaded file is not a valid zip archive: {url}")

            extract_dir = os.path.join(tmp_dir, "extracted")
            with zipfile.ZipFile(zip_path, "r") as zf:
                self._extract_package(zf, Path(extract_dir))

            # 单一顶层目录时直接安装其内容，避免技能目录多嵌套一层。
            top_items = [
                item for item in os.listdir(extract_dir)
                if not item.startswith(".")
            ]
            if len(top_items) == 1:
                single = os.path.join(extract_dir, top_items[0])
                if os.path.isdir(single):
                    extract_dir = single
            self._finalize_install(
                name, Path(extract_dir), payload.get("category")
            )

        logger.info(f"[SkillService] add: skill '{name}' installed via package ({url})")

    def _extract_package(self, archive: zipfile.ZipFile, destination: Path) -> None:
        """逐项校验并解压技能包，拒绝穿越路径和特殊文件。"""

        destination.mkdir(parents=True, exist_ok=True)
        for member in archive.infolist():
            raw_name = member.filename.rstrip("/\\")
            if not raw_name:
                continue
            target = self._safe_file_destination(
                destination, raw_name, "archive member path"
            )
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(unix_mode)
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("archive member must be a regular file or directory")
            if member.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(member, "r") as source, open(target, "xb") as output:
                shutil.copyfileobj(source, output)

    def _finalize_install(
        self, name: str, staged_dir: Path, category: Optional[str]
    ) -> None:
        """在共享锁内复核治理状态，并以可回退目录替换完成安装。"""

        if not staged_dir.is_dir() or is_link_or_reparse_point(staged_dir):
            raise ValueError("staged skill payload must be a regular directory")
        self._assert_skill_mutable(name)
        with self._mutation_lock:
            self._assert_skill_mutable(name)
            skill_dir = Path(self._safe_skill_dir(name))
            skill_dir.parent.mkdir(parents=True, exist_ok=True)
            self._assert_no_reparse_components(
                Path(self.manager.custom_dir), skill_dir, "skill path"
            )
            backup_dir = skill_dir.with_name(
                ".%s.backup-%s" % (skill_dir.name, uuid.uuid4().hex)
            )
            previous_existed = skill_dir.exists()
            if previous_existed:
                if is_link_or_reparse_point(skill_dir) or not skill_dir.is_dir():
                    raise ValueError("existing skill path must be a regular directory")
                os.rename(str(skill_dir), str(backup_dir))
            installed = False
            try:
                os.rename(str(staged_dir), str(skill_dir))
                installed = True
                self.manager.refresh_skills()
                if category and name in self.manager.skills_config:
                    self.manager.skills_config[name]["category"] = category
                    self.manager._save_skills_config()
            except Exception:
                if installed and skill_dir.exists():
                    shutil.rmtree(skill_dir)
                if previous_existed and backup_dir.exists():
                    os.rename(str(backup_dir), str(skill_dir))
                raise
            if previous_existed and backup_dir.exists():
                shutil.rmtree(backup_dir)

    # ------------------------------------------------------------------
    # open / close (enable / disable)
    # ------------------------------------------------------------------
    def open(self, payload: dict) -> None:
        """
        Enable a skill by name.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._assert_skill_mutable(name)
        with self._mutation_lock:
            self._assert_skill_mutable(name)
            self.manager.set_skill_enabled(name, enabled=True)
        logger.info(f"[SkillService] open: skill '{name}' enabled")

    def close(self, payload: dict) -> None:
        """
        Disable a skill by name.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._assert_skill_mutable(name)
        with self._mutation_lock:
            self._assert_skill_mutable(name)
            self.manager.set_skill_enabled(name, enabled=False)
        logger.info(f"[SkillService] close: skill '{name}' disabled")

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------
    def delete(self, payload: dict) -> None:
        """
        Delete a skill by removing its directory entirely.

        :param payload: {"name": "skill_name"}
        """
        name = payload.get("name")
        if not name:
            raise ValueError("skill name is required")
        self._assert_skill_mutable(name)
        removed_dir = None
        with self._mutation_lock:
            self._assert_skill_mutable(name)
            skill_dir = Path(self._safe_skill_dir(name))
            if skill_dir.exists():
                if is_link_or_reparse_point(skill_dir) or not skill_dir.is_dir():
                    raise ValueError("skill path must be a regular directory")
                removed_dir = skill_dir.with_name(
                    ".%s.deleted-%s" % (skill_dir.name, uuid.uuid4().hex)
                )
                os.rename(str(skill_dir), str(removed_dir))
                logger.info(f"[SkillService] delete: removed directory {skill_dir}")
            else:
                logger.warning(
                    f"[SkillService] delete: skill directory not found: {skill_dir}"
                )
            try:
                # 刷新失败时恢复原目录，避免磁盘内容与配置形成半完成状态。
                self.manager.refresh_skills()
            except Exception:
                if removed_dir is not None and removed_dir.exists():
                    os.rename(str(removed_dir), str(skill_dir))
                raise
        if removed_dir is not None and removed_dir.exists():
            shutil.rmtree(removed_dir)
        logger.info(f"[SkillService] delete: skill '{name}' deleted")

    # ------------------------------------------------------------------
    # dispatch - single entry point for protocol messages
    # ------------------------------------------------------------------
    def dispatch(self, action: str, payload: Optional[dict] = None) -> dict:
        """
        Dispatch a skill management action and return a protocol-compatible
        response dict.

        :param action: one of query / add / open / close / delete
        :param payload: action-specific payload (may be None for query)
        :return: dict with action, code, message, payload
        """
        payload = payload or {}
        try:
            if action == "query":
                result_payload = self.query()
                return {"action": action, "code": 200, "message": "success", "payload": result_payload}
            elif action == "add":
                self.add(payload)
            elif action == "open":
                self.open(payload)
            elif action == "close":
                self.close(payload)
            elif action == "delete":
                self.delete(payload)
            else:
                return {"action": action, "code": 400, "message": f"unknown action: {action}", "payload": None}
            return {"action": action, "code": 200, "message": "success", "payload": None}
        except Exception as e:
            logger.error(f"[SkillService] dispatch error: action={action}, error={e}")
            return {"action": action, "code": 500, "message": str(e), "payload": None}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _download_file(url: str, dest: str):
        """
        Download a file from *url* and save to *dest*.

        :param url: remote file URL
        :param dest: local destination path
        """
        if requests is None:
            raise RuntimeError("requests library is required for downloading skill files")

        dest_dir = os.path.dirname(dest)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.write(resp.content)
        logger.debug(f"[SkillService] downloaded {url} -> {dest}")
