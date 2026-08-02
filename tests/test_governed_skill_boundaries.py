"""技能治理私有数据和有效投影的旁路边界测试。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.evolution import executor
from agent.skills.service import SkillService
from agent.tools.edit import Edit
from agent.tools.ls import Ls
from agent.tools.read import Read
from agent.tools.search_files import SearchFiles
from agent.tools.send import Send
from agent.tools.write import Write


def _create_active_skill_fact(workspace: Path, name: str = "data-check") -> Path:
    """创建通用边界测试所需的最小只读治理事实。"""

    system_dir = workspace / "skills" / ".system"
    system_dir.mkdir(parents=True)
    database = system_dir / "governed-skills.db"
    with sqlite3.connect(str(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE governed_skill_versions (
                tenant_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                name TEXT NOT NULL,
                version INTEGER NOT NULL
            );
            CREATE TABLE governed_skill_state_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                skill_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO governed_skill_versions VALUES (?, ?, ?, ?)",
            ("tenant-one", "skill-one", name, 1),
        )
        connection.execute(
            """
            INSERT INTO governed_skill_state_events
            (tenant_id, skill_id, version, status) VALUES (?, ?, ?, ?)
            """,
            ("tenant-one", "skill-one", 1, "active"),
        )
    return database


def _assert_denied(result) -> None:
    assert result.status == "error"
    assert "governed" in str(result.result).lower()


def _create_windows_junction_or_skip(link: Path, target: Path) -> None:
    """创建 Windows 目录联接点；权限不足时跳过对应边界测试。"""

    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("当前环境不能创建 Windows 目录联接点")


def test_generic_tools_cannot_read_or_enumerate_skill_private_state(tmp_path):
    database = _create_active_skill_fact(tmp_path)
    config = {"cwd": str(tmp_path)}

    _assert_denied(Read(config).execute({"path": str(database)}))
    _assert_denied(Ls(config).execute({"path": "skills/.system"}))
    _assert_denied(
        SearchFiles(config).execute(
            {"path": "skills/.system", "pattern": "active", "no_ignore": True}
        )
    )
    _assert_denied(Send(config).execute({"path": str(database)}))


def test_generic_write_and_edit_cannot_change_active_governed_skill(tmp_path):
    _create_active_skill_fact(tmp_path)
    skill_dir = tmp_path / "skills" / "data-check"
    skill_dir.mkdir(parents=True)
    existing = skill_dir / "notes.txt"
    existing.write_text("原始内容", encoding="utf-8")
    config = {"cwd": str(tmp_path)}

    _assert_denied(
        Write(config).execute(
            {"path": "skills/data-check/new.txt", "content": "绕过治理"}
        )
    )
    _assert_denied(
        Edit(config).execute(
            {
                "path": "skills/data-check/notes.txt",
                "oldText": "原始内容",
                "newText": "绕过治理",
            }
        )
    )
    assert existing.read_text(encoding="utf-8") == "原始内容"
    assert not (skill_dir / "new.txt").exists()


def test_generic_write_cannot_change_machine_managed_skill_config(tmp_path):
    _create_active_skill_fact(tmp_path)
    config_path = tmp_path / "skills" / "skills_config.json"
    config_path.write_text('{"data-check": {"enabled": true}}', encoding="utf-8")

    result = Write({"cwd": str(tmp_path)}).execute(
        {
            "path": "skills/skills_config.json",
            "content": '{"data-check": {"enabled": false}}',
        }
    )

    assert result.status == "error"
    assert "machine-managed" in str(result.result).lower()
    assert config_path.read_text(encoding="utf-8") == (
        '{"data-check": {"enabled": true}}'
    )


def test_frontmatter_marker_blocks_writes_when_database_is_unavailable(tmp_path):
    skill_dir = tmp_path / "skills" / "marked-skill"
    skill_dir.mkdir(parents=True)
    projection = skill_dir / "SKILL.md"
    projection.write_text(
        "---\nname: marked-skill\ndescription: test\ngoverned: true\n---\n",
        encoding="utf-8",
    )

    result = Write({"cwd": str(tmp_path)}).execute(
        {"path": "skills/marked-skill/script.py", "content": "print('x')"}
    )
    _assert_denied(result)
    assert not (skill_dir / "script.py").exists()


def test_control_plane_cannot_mutate_governed_skill_or_system_directory(tmp_path):
    _create_active_skill_fact(tmp_path)
    skill_dir = tmp_path / "skills" / "data-check"
    skill_dir.mkdir(parents=True)
    sentinel = skill_dir / "sentinel.txt"
    sentinel.write_text("保留", encoding="utf-8")
    manager = MagicMock()
    manager.custom_dir = str(tmp_path / "skills")
    service = SkillService(manager)

    for operation in (
        lambda: service.add(
            {
                "name": "data-check",
                "type": "url",
                "files": [{"url": "https://invalid.example/test", "path": "SKILL.md"}],
            }
        ),
        lambda: service.open({"name": "data-check"}),
        lambda: service.close({"name": "data-check"}),
        lambda: service.delete({"name": "data-check"}),
        lambda: service.delete({"name": ".system"}),
    ):
        try:
            operation()
        except ValueError as error:
            assert "governed" in str(error).lower()
        else:
            raise AssertionError("治理技能旁路操作未被拒绝")
    assert sentinel.read_text(encoding="utf-8") == "保留"
    assert (tmp_path / "skills" / ".system" / "governed-skills.db").exists()


@pytest.mark.parametrize("name", ["data-check/nested", "DATA-CHECK"])
def test_control_plane_rejects_governed_skill_aliases(tmp_path, name):
    _create_active_skill_fact(tmp_path)
    manager = MagicMock()
    manager.custom_dir = str(tmp_path / "skills")
    service = SkillService(manager)

    with pytest.raises(ValueError, match="governed"):
        service.delete({"name": name})


def test_url_install_rejects_file_path_escape_before_download(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    manager = MagicMock()
    manager.custom_dir = str(skills_dir)
    service = SkillService(manager)
    service._download_file = MagicMock()

    with pytest.raises(ValueError, match="file path"):
        service._add_url(
            "manual-skill",
            {
                "files": [
                    {
                        "url": "https://invalid.example/payload",
                        "path": "../skills_config.json",
                    }
                ]
            },
        )

    service._download_file.assert_not_called()
    assert not (skills_dir / "skills_config.json").exists()


def test_url_install_rechecks_governance_after_download(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "data-check"
    skill_dir.mkdir(parents=True)
    sentinel = skill_dir / "sentinel.txt"
    sentinel.write_text("保留", encoding="utf-8")
    manager = MagicMock()
    manager.custom_dir = str(skills_dir)
    service = SkillService(manager)

    def download_and_publish(_url: str, destination: str) -> None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text("候选内容", encoding="utf-8")
        _create_active_skill_fact(tmp_path)

    service._download_file = download_and_publish

    with pytest.raises(ValueError, match="governed"):
        service._add_url(
            "data-check",
            {
                "files": [
                    {
                        "url": "https://invalid.example/SKILL.md",
                        "path": "SKILL.md",
                    }
                ]
            },
        )

    assert sentinel.read_text(encoding="utf-8") == "保留"


def test_package_install_rechecks_governance_after_download(tmp_path):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "data-check"
    skill_dir.mkdir(parents=True)
    sentinel = skill_dir / "sentinel.txt"
    sentinel.write_text("保留", encoding="utf-8")
    manager = MagicMock()
    manager.custom_dir = str(skills_dir)
    service = SkillService(manager)

    def download_package_and_publish(_url: str, destination: str) -> None:
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(
                "SKILL.md", "---\nname: data-check\ndescription: 候选\n---\n"
            )
        _create_active_skill_fact(tmp_path)

    service._download_file = download_package_and_publish

    with pytest.raises(ValueError, match="governed"):
        service._add_package(
            "data-check",
            {
                "files": [{"url": "https://invalid.example/skill.zip"}],
            },
        )

    assert sentinel.read_text(encoding="utf-8") == "保留"


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持目录联接点")
def test_evolution_rejects_system_junction_before_database_creation(tmp_path):
    skills_dir = tmp_path / "skills"
    target = skills_dir / "junction-target"
    target.mkdir(parents=True)
    junction = skills_dir / ".system"
    _create_windows_junction_or_skip(junction, target)
    try:
        with pytest.raises(ValueError, match="重解析点"):
            executor._create_skill_propose_tool(
                workspace_dir=tmp_path,
                tenant_id="tenant-one",
                user_id="user-one",
                source_type="skill-shadow-evidence",
                source_ref="skill-shadow://evidence/sha256/%s" % ("a" * 64),
                source_payload=b'{"schema_version":1}',
                model_id="test-model",
                protected_skills=set(),
            )
        assert not (target / "governed-skills.db").exists()
    finally:
        os.rmdir(junction)


def test_ungoverned_skill_files_remain_writable(tmp_path):
    _create_active_skill_fact(tmp_path)
    result = Write({"cwd": str(tmp_path)}).execute(
        {"path": "skills/manual-skill/notes.txt", "content": "用户技能"}
    )
    assert result.status == "success"
    assert (
        tmp_path / "skills" / "manual-skill" / "notes.txt"
    ).read_text(encoding="utf-8") == "用户技能"
