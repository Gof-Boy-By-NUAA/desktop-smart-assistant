"""真实 HTTP 请求验证 Unicode 密码及畸形凭证，不连接外部服务。"""

import json
import threading
from http.client import HTTPConnection
from wsgiref.simple_server import make_server

import pytest
import web

import config
from channel.web import web_channel


@pytest.fixture
def auth_server():
    # 只替换测试配置数据；处理器、签名、认证和 HTTP socket 均使用真实实现。
    original = config.config
    config.config = config.Config({"web_password": "密码-é", "web_session_expire_days": 1})
    application = web.application(
        ("/auth/login", "AuthLoginHandler", "/auth/check", "AuthCheckHandler"),
        vars(web_channel), autoreload=False,
    )
    server = make_server("127.0.0.1", 0, application.wsgifunc())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, payload=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            connection.request("POST" if payload is not None else "GET", path, body,
                               headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        config.config = original


def test_unicode_password_login_and_wrong_password(auth_server):
    status, body = auth_server("/auth/login", {"password": "密码-é"})
    assert status == 200
    token = json.loads(body)["token"]
    status, body = auth_server("/auth/check", headers={"Authorization": f"Bearer {token}"})
    assert status == 200 and json.loads(body)["authenticated"] is True
    status, body = auth_server("/auth/login", {"password": "错误密码"})
    assert status == 200 and json.loads(body)["status"] == "error"


def test_non_ascii_bearer_signature_is_rejected_without_server_error(auth_server):
    token = web_channel._create_auth_token()
    malformed = token.rsplit(".", 1)[0] + ".é"
    status, body = auth_server("/auth/check", headers={"Authorization": f"Bearer {malformed}"})
    assert status == 200 and json.loads(body)["authenticated"] is False


def test_non_ascii_subject_signature_is_rejected():
    assert web_channel._verify_auth_subject_token("s1." + "a" * 32 + ".é") is None
