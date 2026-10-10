"""Real HTTP/config/filesystem regression for rejected RC3 config updates.

The test server routes to the production handlers and real authentication.
Only configuration data and COW_DATA_DIR are isolated; no handler, credential
verification, transport or filesystem implementation is replaced.
"""

import copy
import http.cookiejar
import json
import secrets
import threading
import urllib.error
import urllib.request
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

import config


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


@pytest.fixture
def config_http(tmp_path, monkeypatch):
    from channel.web import web_channel

    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    current = config.conf()
    original = copy.deepcopy(dict(current))
    current.clear()
    current.update({"cow_lang": "en", "agent_max_steps": 5,
                    "web_password": "", "web_password_hash": ""})
    disk = tmp_path / "config.json"
    disk.write_text(json.dumps(dict(current)), encoding="utf-8")
    app = web_channel.web.application(
        ("/config", "ConfigHandler", "/auth/login", "AuthLoginHandler"),
        vars(web_channel), autoreload=False,
    )
    server = make_server("127.0.0.1", 0, app.wsgifunc(), handler_class=QuietHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )

    def request(route, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}{route}", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.loads(response.read().decode("utf-8"))

    try:
        yield current, disk, request, web_channel
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=10)
        current.clear()
        current.update(original)
        assert not worker.is_alive()


@pytest.mark.parametrize("with_existing_password", [False, True])
def test_rejected_weak_password_changes_neither_memory_nor_disk(config_http, with_existing_password):
    current, disk, request, channel = config_http
    if with_existing_password:
        old_password = secrets.token_urlsafe(24)
        current["web_password_hash"] = config.hash_web_password(old_password)
        disk.write_text(json.dumps(dict(current)), encoding="utf-8")
        status, result = request("/auth/login", {"password": old_password})
        assert status == 200 and result["status"] == "success"
    before_memory = copy.deepcopy(dict(current))
    before_disk = disk.read_bytes()
    status, result = request("/config", {"updates": {"cow_lang": "zh", "web_password": "short"}})
    assert status == 200 and result["message"] == "weak_password"
    assert dict(current) == before_memory
    assert disk.read_bytes() == before_disk
    assert channel._password_matches("short") is False
    status, result = request("/config")
    assert status == 200 and result["status"] == "success"


def test_invalid_numeric_update_does_not_apply_earlier_keys(config_http):
    current, disk, request, _ = config_http
    before = copy.deepcopy(dict(current)), disk.read_bytes()
    _, result = request("/config", {"updates": {"cow_lang": "zh", "agent_max_steps": "invalid"}})
    assert result["status"] == "error"
    assert dict(current) == before[0]
    assert disk.read_bytes() == before[1]


def test_unreadable_disk_config_does_not_apply_memory_updates(config_http):
    current, disk, request, _ = config_http
    before = copy.deepcopy(dict(current))
    disk.unlink()
    disk.mkdir()
    _, result = request("/config", {"updates": {"cow_lang": "zh"}})
    assert result["status"] == "error"
    assert dict(current) == before
    assert disk.is_dir()


def test_valid_password_is_hashed_on_disk_and_usable_for_real_login(config_http):
    current, disk, request, channel = config_http
    candidate = "fixture-new-password-9"
    status, result = request("/config", {"updates": {"web_password": candidate, "agent_max_steps": 7}})
    assert status == 200 and result["status"] == "success"
    stored = json.loads(disk.read_text(encoding="utf-8"))
    assert "web_password" not in current and "web_password" not in stored
    assert stored["web_password_hash"] == current["web_password_hash"]
    assert config.verify_web_password(candidate, stored["web_password_hash"])
    assert channel._password_matches(candidate)
    assert current["agent_max_steps"] == stored["agent_max_steps"] == 7
    assert request("/config")[0] == 401
    status, result = request("/auth/login", {"password": candidate})
    assert status == 200 and result["status"] == "success"
    status, result = request("/config")
    assert status == 200 and result["web_password_set"] is True


def test_explicit_password_clear_updates_both_memory_and_disk(config_http):
    current, disk, request, channel = config_http
    candidate = "fixture-existing-password-9"
    current["web_password_hash"] = config.hash_web_password(candidate)
    disk.write_text(json.dumps(dict(current)), encoding="utf-8")
    assert request("/auth/login", {"password": candidate})[1]["status"] == "success"
    _, result = request("/config", {"updates": {"web_password": ""}})
    assert result["status"] == "success" and result["warning"] == "password_cleared"
    assert current["web_password_hash"] == ""
    assert json.loads(disk.read_text(encoding="utf-8"))["web_password_hash"] == ""
    assert not channel._is_password_enabled()
