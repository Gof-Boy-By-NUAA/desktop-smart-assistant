"""本地受控 MCP 协议对端注入错误；真实子进程/管道，不证明第三方服务可用。"""

import sys
import time

import pytest

from agent.tools.mcp.mcp_client import McpClient
from agent.tools.mcp.mcp_tool import McpTool


PEER = r'''
import json, sys, time
mode = sys.argv[1]
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    reply = {"jsonrpc": "2.0", "id": request["id"]}
    if request["method"] == "initialize":
        if mode == "bad-handshake":
            reply["error"] = {"code": -32603, "message": "handshake rejected"}
        else:
            reply["result"] = {"protocolVersion": "2024-11-05", "capabilities": {},
                               "serverInfo": {"name": "audit-test-peer", "version": "1"}}
    elif mode == "disconnect":
        sys.exit(0)
    elif mode == "rpc-error":
        reply["error"] = {"code": -32603, "message": "controlled RPC failure"}
    elif mode == "tool-error":
        reply["result"] = {"isError": True, "content": [{"type": "text", "text": "controlled tool failure"}]}
    elif mode == "noise":
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}), flush=True)
            time.sleep(0.05)
        reply["result"] = {"content": [{"type": "text", "text": "late response"}]}
    else:
        reply["result"] = {"content": [{"type": "text", "text": "normal result"}]}
    print(json.dumps(reply), flush=True)
'''


def client_for(mode):
    return McpClient({"name": "audit-test-peer", "type": "stdio", "command": sys.executable,
                      "args": ["-u", "-c", PEER, mode], "timeout": 1})


@pytest.mark.parametrize("mode", ["disconnect", "rpc-error", "tool-error"])
def test_mcp_failure_does_not_report_tool_success(mode):
    client = client_for(mode)
    try:
        assert client.initialize()
        tool = McpTool(client, {"name": "probe"}, "audit-test-peer")
        result = tool.execute({})
        assert result.status == "error", result.result
    finally:
        client.shutdown()


def test_mcp_success_is_preserved():
    client = client_for("success")
    try:
        assert client.initialize()
        result = McpTool(client, {"name": "probe"}, "audit-test-peer").execute({})
        assert result.status == "success"
        assert result.result == "normal result"
    finally:
        client.shutdown()


def test_mcp_arguments_are_not_logged(caplog):
    client = client_for("success")
    secret_marker = "AUDIT_SYNTHETIC_CREDENTIAL_66271"
    try:
        assert client.initialize()
        result = McpTool(client, {"name": "probe"}, "audit-test-peer").execute(
            {"api_key": secret_marker, "private_document": secret_marker}
        )
        assert result.status == "success"
        assert secret_marker not in caplog.text
    finally:
        client.shutdown()


def test_failed_handshake_reaps_child():
    client = client_for("bad-handshake")
    try:
        assert client.initialize() is False
        assert client._proc is None or client._proc.poll() is not None
    finally:
        client.shutdown()


def test_notifications_do_not_reset_request_deadline():
    client = client_for("noise")
    try:
        assert client.initialize()
        started = time.monotonic()
        result = McpTool(client, {"name": "probe"}, "audit-test-peer").execute({})
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"configured 1s timeout took {elapsed:.2f}s"
        assert result.status == "error"
    finally:
        client.shutdown()
