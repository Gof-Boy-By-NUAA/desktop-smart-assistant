"""SSRF 公网过滤必须拒绝共享地址段；只解析数字地址，不建立外部连接。"""

import pytest

from agent.tools.utils.url_safety import validate_url_safe


@pytest.mark.parametrize("address", ["100.64.0.1", "100.127.255.254"])
def test_shared_address_space_is_not_public(monkeypatch, address):
    monkeypatch.setenv("WEB_SECURITY_SSRF_PROTECTION", "true")
    with pytest.raises(ValueError, match="non-public"):
        validate_url_safe("http://" + address)


def test_explicitly_disabled_policy_still_allows_local_addresses(monkeypatch):
    monkeypatch.setenv("WEB_SECURITY_SSRF_PROTECTION", "false")
    validate_url_safe("http://100.64.0.1")


def test_public_literal_remains_allowed(monkeypatch):
    monkeypatch.setenv("WEB_SECURITY_SSRF_PROTECTION", "true")
    validate_url_safe("https://8.8.8.8")
