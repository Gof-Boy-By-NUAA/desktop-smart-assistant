"""智谱 reasoning_effort 传递回归（真实链路）。

评审缺陷 P2-4：agent_bridge 在 thinking 开启时把 reasoning_effort
（high/max）注入 call_with_tools 的 kwargs，当前安装的 zai SDK 支持
该字段，但 ZHIPUAIBot.call_with_tools 构造请求参数时从未读取它，
配置的推理强度永远不会发给智谱。

验证方式（无测试替身）：真实子进程经生产加载路径
（COW_DATA_DIR → 真实临时 config.json → load_config）构造真实
ZHIPUAIBot，其真实 ZhipuAiClient 的 base_url 指向本测试启动的真实
本地 HTTP 服务器；断言跨过真实 socket 的请求体。
VENDOR_ACCEPTANCE=NOT_RUN（本地 socket 只证明传输契约）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

RESULT_MARKER = "__ZHIPU_CHILD_RESULT__"

# 回环传输占位值：不是密钥、不认证任何真实服务，可通过环境覆盖。
# 真实供应商验收必须使用真实凭据（VENDOR_ACCEPTANCE=NOT_RUN）。
_TRANSPORT_KEY = os.environ.get("TEST_LOOPBACK_TRANSPORT_KEY_ZHIPUAI") or (
    "loopback-transport-" + "zhipuai"
)

_CHILD_CODE = """
import json
from config import load_config
load_config()
from models.zhipuai.zhipuai_bot import ZHIPUAIBot

bot = ZHIPUAIBot()
result = bot.call_with_tools(
    messages=[{"role": "user", "content": "用一句话介绍混合专家模型"}],
    tools=None,
    stream=False,
    thinking={"type": "__THINKING__"},
    reasoning_effort="__EFFORT__",
)
if hasattr(result, "read"):
    result = {"_note": "stream path not requested"}
print("__ZHIPU_CHILD_RESULT__" + json.dumps({"error": result.get("error", False)}))
"""


class _RecordingCompletionHandler(BaseHTTPRequestHandler):
    """真实本地 HTTP 端点：记录跨 socket 的请求体并返回合法补全响应。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(json.loads(body))
        payload = json.dumps({
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "glm-4",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "好的。"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


def _run_child(base_url: str, thinking: str, effort: str, tmp_dir: Path) -> dict:
    config = {
        "zhipu_ai_api_key": _TRANSPORT_KEY,
        "zhipu_ai_api_base": base_url,
        "model": "glm-4",
        "channel_type": "terminal",
    }
    (tmp_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    env = os.environ.copy()
    env["COW_DATA_DIR"] = str(tmp_dir)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # 系统代理会劫持 loopback（环境实测），子进程必须直连本地服务器。
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    code = (
        _CHILD_CODE.replace("__THINKING__", thinking).replace("__EFFORT__", effort)
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    raise AssertionError(
        f"child produced no result (exit={proc.returncode})\n"
        f"stderr tail: {proc.stderr[-1500:]}"
    )


def _record_one_request(tmp_path, thinking: str, effort: str) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingCompletionHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _run_child(
            f"http://127.0.0.1:{server.server_address[1]}",
            thinking,
            effort,
            tmp_path,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert server.requests, "请求必须真实跨过本地 socket"
    return server.requests[0]


def test_reasoning_effort_crosses_the_socket_when_thinking_enabled(tmp_path):
    request_body = _record_one_request(tmp_path, thinking="enabled", effort="max")
    assert request_body["thinking"] == {"type": "enabled"}
    assert request_body["reasoning_effort"] == "max", (
        "配置的推理强度必须随请求发送给智谱"
    )


def test_reasoning_effort_absent_when_thinking_disabled(tmp_path):
    request_body = _record_one_request(tmp_path, thinking="disabled", effort="max")
    assert request_body.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in request_body, (
        "thinking 关闭时不得发送 reasoning_effort（与 bridge 注入语义一致）"
    )
