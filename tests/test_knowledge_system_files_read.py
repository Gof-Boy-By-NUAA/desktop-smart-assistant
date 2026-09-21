"""知识库系统文件可读性回归（真实链路）。

评审缺陷 P2-3：列表会展示根目录的 index.md / log.md，但读取时
read_file 先走治理文档查询，find_by_logical_path 对这两个保护名
抛 KnowledgeValidationError("projection_path 无效")，导致后面的
PROTECTED_FILES 兜底分支永远不可达——服务列出的文件自己打不开。

本文件使用真实 KnowledgeService、真实临时工作区与真实磁盘文件，
不使用任何测试替身；路径安全检查（穿越/非 .md/越界）必须保持。
"""

from __future__ import annotations

import pytest

from agent.knowledge.service import KnowledgeService


@pytest.fixture()
def service_with_system_files(tmp_path):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "index.md").write_text(
        "# 知识库索引\n\n- [MoE](concepts/moe.md)\n", encoding="utf-8"
    )
    (knowledge / "log.md").write_text(
        "# 变更日志\n\n- 2026-09-18 初始化知识库\n", encoding="utf-8"
    )
    concepts = knowledge / "concepts"
    concepts.mkdir()
    (concepts / "moe.md").write_text("# MoE\n混合专家模型。\n", encoding="utf-8")
    return KnowledgeService(str(tmp_path))


def test_read_index_and_log_system_files(service_with_system_files):
    svc = service_with_system_files

    index_result = svc.read_file("index.md")
    assert index_result["path"] == "index.md"
    assert "知识库索引" in index_result["content"]

    log_result = svc.read_file("log.md")
    assert log_result["path"] == "log.md"
    assert "变更日志" in log_result["content"]


def test_listed_files_are_all_openable(service_with_system_files):
    """列表展示的每个文件都必须能被 read_file 打开（端到端一致性）。"""

    svc = service_with_system_files
    listing = svc.list_tree()

    listed = [
        entry["name"] for entry in listing["root_files"]
    ]

    def walk(nodes, prefix):
        for node in nodes:
            node_prefix = f"{prefix}{node['dir']}/"
            listed.extend(
                f"{node_prefix}{entry['name']}" for entry in node["files"]
            )
            walk(node["children"], node_prefix)

    walk(listing["tree"], prefix="")

    assert "index.md" in listed
    assert "log.md" in listed

    for rel_path in listed:
        result = svc.read_file(rel_path)
        assert result["path"] == rel_path
        assert isinstance(result["content"], str)


def test_path_safety_checks_are_preserved(service_with_system_files):
    """修复不得放宽路径检查：穿越、非 .md、越界仍要拒绝。"""

    svc = service_with_system_files
    with pytest.raises(ValueError):
        svc.read_file("../outside.md")
    with pytest.raises(ValueError):
        svc.read_file("concepts/../../escape.md")
    with pytest.raises(ValueError):
        svc.read_file("notes.txt")
    with pytest.raises(FileNotFoundError):
        svc.read_file("missing.md")
