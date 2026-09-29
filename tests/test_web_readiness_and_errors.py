"""Web 就绪检查与对话错误提示回归（真实链路）。

评审缺陷 P2-5：/api/readiness 只检查存储/目录/磁盘/队列，Agent 初始化
失败（如记忆初始化故障）时仍返回 ready，页面可访问与对话可用不可区分。
评审缺陷 P2-7：初始化失败等终态在 SSE 投递中统一显示英文
"Agent execution was rejected..."，用户无法判断原因。
P0 批次三：readiness 增加聊天模型配置检查（local_preflight，只查本地
配置，不在 readiness 请求里调用付费模型 API）。

验证方式（无测试替身）：真实子进程经生产加载路径
（COW_DATA_DIR → 真实临时 config.json → load_config）启动真实
web.py 应用与 stdlib wsgiref 真实 HTTP 服务器，对自身发起真实 HTTP GET；错误文案
经由生产映射函数断言。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULT_MARKER = "__WEB_CHILD_RESULT__"


def _test_deepseek_key() -> str:
    """readiness 用例的 DeepSeek Key：环境变量读取或本地拼装默认值。

    不是真实凭据，只用于让 local_preflight 的"非空且非占位 Key"检查
    通过；真实鉴权与对话能力验证属于 P3。
    """
    return os.environ.get("SA_READINESS_TEST_DEEPSEEK_KEY") or (
        "readiness-local-preflight-deepseek"
    )


def _test_key(name: str) -> str:
    """readiness 用例各 provider 的 Key：环境变量读取或本地拼装默认值。

    与 _test_deepseek_key 同一模式：不是真实凭据，不发起任何 API 调用。
    """
    return os.environ.get("SA_READINESS_TEST_KEY_" + name) or (
        "readiness-local-preflight-" + name
    )

_CHILD_CODE = """
import json
import threading
from urllib.error import HTTPError
import urllib.request
from wsgiref.simple_server import make_server

from config import load_config
load_config()

import web
from channel.web import web_channel

# 真实 web.py 应用（生产构建方式）；HTTP 承载用 stdlib wsgiref 真实服务器
# （web.py 自带 WSGIServer 在本机子进程中启动挂起，与请求处理无关）。
urls = (
    '/api/readiness', 'ReadinessHandler',
)
app = web.application(urls, web_channel.__dict__, autoreload=False)
server = make_server('127.0.0.1', 0, app.wsgifunc())
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
port = server.server_port
try:
    # 直连本地服务器：系统代理会劫持 loopback（本机实测）。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(
            'http://127.0.0.1:%d/api/readiness' % port, timeout=180
        )
        with response:
            body = json.loads(response.read().decode('utf-8'))
            status_code = response.status
    except HTTPError as exc:
        body = json.loads(exc.read().decode('utf-8'))
        status_code = exc.code
finally:
    server.shutdown()
    server.server_close()

# 错误文案映射（P2-7）
from channel.web.web_channel import _web_execution_error_brief

briefs = {
    "init": _web_execution_error_brief(
        'failed_safe', 'Agent initialization returned no agent', 'req-42'
    ),
    "uncertain": _web_execution_error_brief(
        'in_doubt', None, 'req-43'
    ),
}
print(
    "__WEB_CHILD_RESULT__"
    + json.dumps(
        {"status_code": status_code, "body": body, "briefs": briefs},
        ensure_ascii=False,
    )
)
"""


def _run_child(
    tmp_path: Path,
    config_extra: dict | None = None,
    scrub_env: tuple = (),
) -> dict:
    config = {
        "channel_type": "web",
        "agent_workspace": str(tmp_path / "workspace"),
        "use_agent": True,
        # 让 local_preflight 的聊天模型配置检查可判定：有效模型、选中的
        # provider、非空 Key。Key 值来自 helper（环境变量或本地拼装默认
        # 值），不是真实凭据。
        "model": "deepseek-chat",
        "bot_type": "deepseek",
        "deepseek_api_key": _test_deepseek_key(),
    }
    if config_extra:
        config.update(config_extra)
    (tmp_path / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    env = os.environ.copy()
    # 摘除指定环境变量，防止宿主机环境覆盖子进程 config 中的同名键
    # （load_config 会用同名环境变量覆盖配置值）。
    for victim in scrub_env:
        for name in [k for k in env if k.lower() == victim.lower()]:
            env.pop(name, None)
    env["COW_DATA_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", _CHILD_CODE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    raise AssertionError(
        f"child produced no result (exit={proc.returncode})\n"
        f"stderr tail: {proc.stderr[-2000:]}"
    )


def test_readiness_covers_agent_initialization(tmp_path):
    result = _run_child(tmp_path)

    assert result["status_code"] == 200
    body = result["body"]
    checks = body["checks"]

    # P2-5：就绪检查必须覆盖 Agent 初始化（真实初始化链在临时工作区执行）。
    assert "agent_initialization" in checks, (
        "就绪检查缺少 agent_initialization，记忆初始化故障时仍会返回 ready"
    )
    # 临时工作区内真实初始化应成功（P1-1 修复后），整体 ready。
    assert checks["agent_initialization"] is True
    assert body["status"] == "ready"
    # 页面可访问与对话可用两个状态必须显式区分。
    assert body["conversation_ready"] is True
    # 返回值必须明示只是本地预检：readiness 不调用任何付费模型 API。
    assert body["scope"] == "local_preflight"


def test_error_briefs_are_chinese_with_category_and_request_id(tmp_path):
    result = _run_child(tmp_path)
    briefs = result["briefs"]

    init_brief = briefs["init"]
    assert "初始化" in init_brief, "初始化失败必须给出中文类别，而非统一英文拒绝文案"
    assert "req-42" in init_brief, "错误提示必须包含请求编号"
    assert "Agent initialization returned no agent" in init_brief, (
        "保留 durable detail 作为诊断信息"
    )

    uncertain_brief = briefs["uncertain"]
    assert "不确定" in uncertain_brief
    assert "req-43" in uncertain_brief


def test_conversation_ready_is_false_when_any_readiness_check_fails(tmp_path):
    result = _run_child(
        tmp_path,
        {"web_readiness_min_free_bytes": 10**30},
    )

    body = result["body"]
    assert result["status_code"] == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["disk_space"] is False
    assert body["checks"]["agent_initialization"] is True
    assert body["conversation_ready"] is False


def test_readiness_503_when_chat_model_api_key_missing(tmp_path):
    # P0 批次三：选中的 chat provider 缺少可用 API Key（空值）时，
    # readiness 必须返回 503 且 conversation_ready=false——页面可访问
    # 不等于可对话。scrub 掉宿主机可能存在的 DEEPSEEK_API_KEY，确保
    # 子进程里的空 Key 不会被环境变量覆盖成"已配置"。
    result = _run_child(
        tmp_path,
        {"deepseek_api_key": ""},
        scrub_env=("DEEPSEEK_API_KEY",),
    )

    body = result["body"]
    assert result["status_code"] == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["chat_model_config"] is False
    assert body["checks"]["agent_initialization"] is True
    assert body["conversation_ready"] is False
    assert body["scope"] == "local_preflight"


def test_readiness_custom_provider_key_is_sufficient(tmp_path):
    # P1 正向用例：custom 凭据有效、open_ai 为空时 chat_model_config 必须为
    # True。生产路径凭据来自 custom_api_key（custom_provider.resolve_custom_credentials），
    # 映射到 open_ai_api_key 的旧实现在此场景产生假阴性。
    result = _run_child(
        tmp_path,
        {
            "bot_type": "custom",
            "model": "test-model",
            "custom_api_key": _test_key("custom"),
            "custom_api_base": "https://example.invalid/v1",
            "open_ai_api_key": "",
        },
        scrub_env=("CUSTOM_API_KEY", "OPENAI_API_KEY"),
    )

    body = result["body"]
    assert result["status_code"] == 200
    assert body["status"] == "ready"
    assert body["checks"]["chat_model_config"] is True
    assert body["conversation_ready"] is True


def test_readiness_custom_provider_rejects_openai_key_substitution(tmp_path):
    # P1 反向用例：custom 凭据为空、open_ai 有值时 chat_model_config 必须为
    # False——配置了其他 provider 的 Key 不代表当前选中的 custom 可对话。
    # 映射到 open_ai_api_key 的旧实现在此场景产生假阳性。
    result = _run_child(
        tmp_path,
        {
            "bot_type": "custom",
            "model": "test-model",
            "custom_api_key": "",
            "custom_api_base": "https://example.invalid/v1",
            "open_ai_api_key": _test_key("openai"),
        },
        scrub_env=("CUSTOM_API_KEY", "OPENAI_API_KEY"),
    )

    body = result["body"]
    assert result["status_code"] == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["chat_model_config"] is False
    assert body["checks"]["agent_initialization"] is True
    assert body["conversation_ready"] is False


def test_readiness_custom_provider_by_id_reads_selected_entry(tmp_path):
    # P1 多 provider 用例：bot_type="custom:<id>" 的凭据来自
    # custom_providers 中选中条目，与 open_ai_api_key 无关。
    result = _run_child(
        tmp_path,
        {
            "bot_type": "custom:p0prov",
            "model": "test-model",
            "custom_providers": [
                {
                    "id": "p0prov",
                    "name": "p0-provider",
                    "api_key": _test_key("custom"),
                    "api_base": "https://example.invalid/v1",
                }
            ],
            "custom_api_key": "",
            "open_ai_api_key": "",
        },
        scrub_env=("CUSTOM_API_KEY", "OPENAI_API_KEY", "CUSTOM_PROVIDERS"),
    )

    body = result["body"]
    assert result["status_code"] == 200
    assert body["status"] == "ready"
    assert body["checks"]["chat_model_config"] is True
    assert body["conversation_ready"] is True


def test_readiness_custom_provider_model_from_selected_entry(tmp_path):
    # P1 回归：生产 ChatGPTBot 的有效模型是 custom_model or 全局 model
    # （chat_gpt_bot.py，provider 条目自带 model 优先）。全局 model 为空、
    # provider model 有效时可正常对话，readiness 不得在解析前按全局 model
    # 判死（假阴性，实测 503）。
    result = _run_child(
        tmp_path,
        {
            "model": "",
            "bot_type": "custom:p0prov",
            "custom_providers": [
                {
                    "id": "p0prov",
                    "name": "p0-provider",
                    "api_key": _test_key("custom"),
                    "api_base": "https://example.invalid/v1",
                    "model": "provider-model",
                }
            ],
            "custom_api_key": "",
            "open_ai_api_key": "",
        },
        scrub_env=("CUSTOM_API_KEY", "OPENAI_API_KEY", "CUSTOM_PROVIDERS"),
    )

    body = result["body"]
    assert result["status_code"] == 200
    assert body["status"] == "ready"
    assert body["checks"]["chat_model_config"] is True
    assert body["conversation_ready"] is True


def test_readiness_custom_provider_requires_some_model(tmp_path):
    # 边界：全局与 provider model 均为空时视为未配置。生产路径虽会回退
    # 默认模型名，但那是用户从未选择过的模型，不能据此判 ready。
    result = _run_child(
        tmp_path,
        {
            "model": "",
            "bot_type": "custom",
            "custom_api_key": _test_key("custom"),
            "custom_api_base": "https://example.invalid/v1",
            "open_ai_api_key": "",
        },
        scrub_env=("CUSTOM_API_KEY", "OPENAI_API_KEY"),
    )

    body = result["body"]
    assert result["status_code"] == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["chat_model_config"] is False
