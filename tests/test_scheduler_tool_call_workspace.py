from types import SimpleNamespace

import pytest

from agent.tools.base_tool import BaseTool
from agent.tools.scheduler.integration import _execute_tool_call
from agent.tools.tool_manager import ToolManager
from agent.tools.write.write import Write
from config import conf


@pytest.fixture(autouse=True)
def _isolate_tool_manager(monkeypatch):
    monkeypatch.setattr(ToolManager, "_instance", None)


def test_scheduled_write_uses_configured_agent_workspace(tmp_path, monkeypatch):
    process_dir = tmp_path / "process-cwd"
    workspace_dir = tmp_path / "agent-workspace"
    process_dir.mkdir()
    workspace_dir.mkdir()
    monkeypatch.chdir(process_dir)
    monkeypatch.setitem(conf(), "agent_workspace", str(workspace_dir))

    manager = ToolManager()
    manager.tool_classes = {"write": Write}
    manager.tool_configs = {"write": {"restrict_to_workspace": True}}

    delivered = []
    channel = SimpleNamespace(
        send=lambda reply, context: delivered.append((reply, context))
    )
    monkeypatch.setattr(
        "channel.channel_factory.create_channel",
        lambda _channel_type: channel,
    )

    task = {
        "id": "workspace-write",
        "action": {
            "type": "tool_call",
            "tool_name": "write",
            "tool_params": {
                "path": "scheduled.txt",
                "content": "scheduled output",
            },
            "receiver": "audit-user",
            "channel_type": "web",
        },
    }

    assert _execute_tool_call(task, object()) is True
    assert (workspace_dir / "scheduled.txt").read_text(encoding="utf-8") == (
        "scheduled output"
    )
    assert not (process_dir / "scheduled.txt").exists()
    assert len(delivered) == 1


def test_create_tool_passes_merged_runtime_config_to_constructor(tmp_path):
    manager = ToolManager()
    manager.tool_classes = {"write": Write}
    manager.tool_configs = {"write": {"restrict_to_workspace": True}}

    tool = manager.create_tool(
        "write",
        runtime_config={"cwd": str(tmp_path)},
    )

    assert tool.cwd == str(tmp_path)
    assert tool.config == {
        "restrict_to_workspace": True,
        "cwd": str(tmp_path),
    }


def test_create_tool_keeps_no_arg_constructor_compatible(tmp_path):
    class NoArgTool(BaseTool):
        name = "no_arg"

        def __init__(self):
            self.initialized = True

    manager = ToolManager()
    manager.tool_classes = {"no_arg": NoArgTool}
    manager.tool_configs = {"no_arg": {"enabled": True}}

    tool = manager.create_tool(
        "no_arg",
        runtime_config={"cwd": str(tmp_path)},
    )

    assert tool.initialized is True
    assert tool.config == {"enabled": True, "cwd": str(tmp_path)}


def test_create_tool_does_not_repurpose_unrelated_optional_argument(tmp_path):
    class OptionalArgumentTool(BaseTool):
        name = "optional_argument"

        def __init__(self, mode="default"):
            self.mode = mode

    manager = ToolManager()
    manager.tool_classes = {"optional_argument": OptionalArgumentTool}
    manager.tool_configs = {"optional_argument": {"enabled": True}}

    tool = manager.create_tool(
        "optional_argument",
        runtime_config={"cwd": str(tmp_path)},
    )

    assert tool.mode == "default"
    assert tool.config == {"enabled": True, "cwd": str(tmp_path)}


def test_create_tool_does_not_mutate_shared_mcp_instance(tmp_path):
    manager = ToolManager()
    manager.tool_classes = {}
    mcp_tool = SimpleNamespace(config={"server": "example"})
    manager._mcp_tool_instances = {"remote_tool": mcp_tool}

    created = manager.create_tool(
        "remote_tool",
        runtime_config={"cwd": str(tmp_path)},
    )

    assert created is mcp_tool
    assert created.config == {"server": "example"}
