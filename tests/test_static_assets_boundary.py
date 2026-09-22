"""通过真实 HTTP 和临时文件验证静态资源目录边界。"""

import threading
from http.client import HTTPConnection
from wsgiref.simple_server import make_server

import pytest
import web

from channel.web import web_channel


@pytest.fixture
def assets_server(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "normal.txt").write_text("public asset", encoding="utf-8")
    sibling = tmp_path / "static_private"
    sibling.mkdir()
    (sibling / "private.txt").write_text("AUDIT_PRIVATE_FILE", encoding="utf-8")
    # 只改变资源目录定位，真实执行生产路径解析、读取与 HTTP 处理。
    monkeypatch.setattr(web_channel, "__file__", str(tmp_path / "web_channel.py"))
    application = web.application(("/assets/(.*)", "AssetsHandler"), vars(web_channel), autoreload=False)
    server = make_server("127.0.0.1", 0, application.wsgifunc())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        yield request, static, sibling
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_static_sibling_prefix_is_not_authorized(assets_server):
    request, _, _ = assets_server
    assert request("/assets/normal.txt") == (200, b"public asset")
    status, body = request("/assets/../static_private/private.txt")
    assert status == 404
    assert b"AUDIT_PRIVATE_FILE" not in body


def test_static_symlink_cannot_escape_root(assets_server):
    request, static, sibling = assets_server
    try:
        (static / "linked").symlink_to(sibling, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 进程不能创建符号链接")
    status, body = request("/assets/linked/private.txt")
    assert status == 404
    assert b"AUDIT_PRIVATE_FILE" not in body
