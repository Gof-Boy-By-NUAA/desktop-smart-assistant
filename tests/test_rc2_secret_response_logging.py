"""R-003: injected malformed responses exercise the real error log branches."""

import copy
import logging
from types import SimpleNamespace

import pytest

from agent.tools.mcp.mcp_oauth import OAuthHandler
from common.log import logger


@pytest.fixture
def records():
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Capture()
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)
        handler.close()


def _malformed_response():
    return {
        "refresh_token": "rc2-test-refresh-value",
        "id_token": "rc2-test-id-value",
        "token": "rc2-test-token-value",
        "secret": "rc2-test-secret-value",
        "api_key": "rc2-test-key-value",
        "authorization": "rc2-test-auth-value",
        "credential": {"nested": "rc2-test-nested-value"},
        "error": "rc2-test-error-with-private-value",
    }


def _assert_no_values_logged(response, messages):
    output = "\n".join(messages)
    for value in response.values():
        assert str(value) not in output
    return output


def test_oauth_missing_access_token_logs_keys_without_mutation(records):
    handler = object.__new__(OAuthHandler)
    handler.server_name = "rc2-oauth-server"
    handler.access_token = "existing-runtime-value"
    response = _malformed_response()
    before = copy.deepcopy(response)
    assert handler._absorb_token_response(response) is False
    output = _assert_no_values_logged(response, records)
    assert "rc2-oauth-server" in output
    assert "missing access_token" in output
    assert "refresh_token" in output
    assert response == before
    assert handler.access_token == "existing-runtime-value"


def test_dingtalk_missing_access_token_logs_status_without_mutation(monkeypatch, tmp_path, records):
    from channel.dingtalk import dingtalk_message
    import config

    response = _malformed_response()
    before = copy.deepcopy(response)
    monkeypatch.setattr(config, "conf", lambda: {
        "dingtalk_client_id": "rc2-test-client", "dingtalk_client_secret": "rc2-test-client-secret"
    })
    monkeypatch.setattr(dingtalk_message.requests, "post", lambda *_args, **_kwargs: SimpleNamespace(
        status_code=200, json=lambda: response
    ))
    assert dingtalk_message.download_image_file("dingtalk://download/robot:download", str(tmp_path)) is None
    output = _assert_no_values_logged(response, records)
    assert "DingTalk" in output
    assert "200" in output
    assert "refresh_token" in output
    assert response == before
