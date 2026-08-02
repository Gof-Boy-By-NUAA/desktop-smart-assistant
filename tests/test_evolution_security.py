from pathlib import Path

from agent.evolution.executor import _guard_tools, _select_tools
from agent.tools.base_tool import ToolResult


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = name
        self.params = {}


class _PathTool(_Tool):
    def __init__(self, name, workspace):
        super().__init__(name)
        self.workspace = Path(workspace)
        self.calls = 0

    def _resolve_path(self, path):
        candidate = Path(path)
        return str(candidate if candidate.is_absolute() else self.workspace / candidate)

    def execute(self, args):
        self.calls += 1
        return ToolResult.success(args.get("path"))


def test_unattended_evolution_never_receives_bash(tmp_path):
    selected = _select_tools(
        [_Tool("read"), _Tool("write"), _Tool("bash"), _Tool("web_fetch")]
    )
    guarded = _guard_tools(selected, str(tmp_path))

    assert [tool.name for tool in guarded] == ["read", "write"]


def test_evolution_write_guard_blocks_all_skill_paths(tmp_path):
    (tmp_path / "skills").mkdir()
    inner = _PathTool("write", tmp_path)
    guarded = _guard_tools([inner], str(tmp_path))[0]

    blocked = (
        "skills/new-skill/SKILL.md",
        "skills/../skills/new-skill/SKILL.md",
        str(tmp_path / "skills" / "new-skill" / "SKILL.md"),
    )
    for path in blocked:
        result = guarded.execute({"path": path})
        assert result.status == "error"
        assert "skill_propose" in result.result
    assert inner.calls == 0

    allowed = guarded.execute({"path": "output/result.md"})
    assert allowed.status == "success"
    assert inner.calls == 1
