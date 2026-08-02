import os

import pytest

from agent.tools.send.send import Send


def _assert_denied(result) -> None:
    assert result.status == "error"
    assert "governed" in str(result.result)


def test_send_blocks_all_knowledge_paths_and_keeps_regular_files(tmp_path):
    knowledge_file = tmp_path / "knowledge" / "shared" / "page.md"
    knowledge_file.parent.mkdir(parents=True)
    knowledge_file.write_text("private knowledge", encoding="utf-8")
    regular_file = tmp_path / "exports" / "result.md"
    regular_file.parent.mkdir()
    regular_file.write_text("public result", encoding="utf-8")

    tool = Send({"cwd": str(tmp_path)})
    for path in (
        "knowledge/shared/page.md",
        "./knowledge/shared/../shared/page.md",
        str(knowledge_file),
    ):
        _assert_denied(tool.execute({"path": path}))

    allowed = tool.execute({"path": str(regular_file)})
    assert allowed.status == "success"
    assert allowed.result["path"] == str(regular_file)


@pytest.mark.skipif(
    os.path.normcase("KNOWLEDGE") != os.path.normcase("knowledge"),
    reason="大小写别名只存在于大小写不敏感的文件系统",
)
def test_send_blocks_uppercase_knowledge_alias(tmp_path):
    target = tmp_path / "knowledge" / "page.md"
    target.parent.mkdir()
    target.write_text("private knowledge", encoding="utf-8")

    _assert_denied(
        Send({"cwd": str(tmp_path)}).execute({"path": "KNOWLEDGE/PAGE.MD"})
    )


def test_send_blocks_symlink_alias_to_knowledge(tmp_path):
    target = tmp_path / "knowledge" / "page.md"
    target.parent.mkdir()
    target.write_text("private knowledge", encoding="utf-8")
    alias = tmp_path / "knowledge-alias"
    try:
        alias.symlink_to(target.parent, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows 当前进程没有创建符号链接的权限")
        raise

    _assert_denied(
        Send({"cwd": str(tmp_path)}).execute({"path": str(alias / "page.md")})
    )


def test_send_blocks_governed_memory_private_storage(tmp_path):
    private_file = tmp_path / "memory" / ".governed" / "private.md"
    private_file.parent.mkdir(parents=True)
    private_file.write_text("private memory", encoding="utf-8")

    _assert_denied(
        Send({"cwd": str(tmp_path)}).execute({"path": str(private_file)})
    )
