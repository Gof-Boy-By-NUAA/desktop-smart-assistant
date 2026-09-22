"""验证真实工具初始化路径；仅隔离无关的自动发现及 MCP 加载。"""

import logging

from agent.tools import tool_manager
from agent.tools.vision.vision import Vision
from bridge.agent_initializer import AgentInitializer


def test_vision_receives_agent_workspace(monkeypatch, tmp_path):
    manager = tool_manager.ToolManager()
    monkeypatch.setattr(manager, "tool_classes", {"vision": Vision})
    monkeypatch.setattr(manager, "tool_configs", {}, raising=False)
    monkeypatch.setattr(manager, "_mcp_tool_instances", {})
    monkeypatch.setattr(manager, "load_tools", lambda: None)
    image = tmp_path / "image.png"
    image.write_bytes(b"test image path only")
    tools = AgentInitializer(None, None)._load_tools(str(tmp_path), None, [], "audit")
    assert len(tools) == 1
    assert tools[0]._resolve_path("image.png") == str(image)


def test_generic_loader_leaves_governed_tools_to_identity_initializer(monkeypatch, caplog):
    import agent.tools

    manager = tool_manager.ToolManager()
    monkeypatch.setattr(manager, "tool_classes", {})
    monkeypatch.setattr(agent.tools, "__all__", [
        "Read", "KnowledgeSearchTool", "KnowledgeGetTool", "KnowledgeWriteTool",
        "KnowledgeRevokeTool", "KnowledgeRollbackTool",
    ])
    with caplog.at_level(logging.ERROR, logger="log"):
        assert manager._load_tools_from_init()
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert set(manager.tool_classes) == {"read"}
