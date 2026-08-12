from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from unittest.mock import patch

from agent.memory.conversation_store import ConversationStore
from common import log as log_module


def test_file_logging_uses_bounded_rotation(monkeypatch, tmp_path):
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COW_LOG_MAX_BYTES", "4096")
    monkeypatch.setenv("COW_LOG_BACKUP_COUNT", "3")
    logger = logging.getLogger("operational-readiness-test")
    log_module._reset_logger(logger)
    try:
        rotating = [
            handler for handler in logger.handlers
            if isinstance(handler, RotatingFileHandler)
        ]
        assert len(rotating) == 1
        assert rotating[0].maxBytes == 4096
        assert rotating[0].backupCount == 3
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_conversation_store_healthcheck_validates_security_schema(tmp_path):
    store = ConversationStore(tmp_path / "index.db")
    assert store.healthcheck() is True


def test_readiness_reports_dependency_failure_with_503(monkeypatch, tmp_path):
    from channel.web import web_channel

    store = patch("agent.memory.get_conversation_store").start()
    store.return_value.healthcheck.return_value = False
    monkeypatch.setattr(web_channel, "_get_workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(web_channel, "conf", lambda: {})
    monkeypatch.setattr(
        web_channel.web, "ctx", SimpleNamespace(status="200 OK"), raising=False
    )
    monkeypatch.setattr(web_channel.web, "header", lambda *args, **kwargs: None)
    try:
        response = json.loads(web_channel.ReadinessHandler().GET())
    finally:
        patch.stopall()
    assert response["status"] == "not_ready"
    assert response["checks"]["conversation_store"] is False
    assert web_channel.web.ctx.status == "503 Service Unavailable"



def test_environment_secret_overrides_never_reach_startup_logs(monkeypatch, tmp_path):
    """No config startup logger may disclose an env-provided credential."""
    import config as config_module

    api_sentinel = "API_SENTINEL_7f5e0d9c"
    password_sentinel = "PASSWORD_SENTINEL_7f5e0d9c"
    token_sentinel = "TOKEN_SENTINEL_7f5e0d9c"
    (tmp_path / "config.json").write_text(
        json.dumps({"cow_lang": "en", "agent": False, "debug": False}),
        encoding="utf-8",
    )
    records: list[str] = []
    original_config = config_module.config

    def capture(message, *args, **_kwargs):
        records.append(message % args if args else str(message))

    monkeypatch.setattr(config_module, "get_data_root", lambda: str(tmp_path))
    monkeypatch.setattr(config_module, "get_resource_root", lambda: str(tmp_path))
    monkeypatch.setattr(config_module.os, "environ", {
        "OPEN_AI_API_KEY": api_sentinel,
        "WEB_PASSWORD": password_sentinel,
        "WEIXIN_TOKEN": token_sentinel,
    })
    monkeypatch.setattr(config_module.logger, "info", capture)
    monkeypatch.setattr(config_module.logger, "debug", capture)
    monkeypatch.setattr(config_module.Config, "load_user_datas", lambda _self: None)
    monkeypatch.setattr(config_module, "config", original_config)

    try:
        config_module.load_config()
        joined = "\n".join(records)
        assert api_sentinel not in joined
        assert password_sentinel not in joined
        assert token_sentinel not in joined
        assert "open_ai_api_key=<redacted>" in joined
        assert "web_password=<redacted>" in joined
        assert "weixin_token=<redacted>" in joined
    finally:
        config_module.config = original_config
