# encoding:utf-8
"""RC3 F2: web password hashing, migration and write-interface policy.

Covers the salted-hash storage format, the legacy in-memory compatibility
path (existing suites inject plaintext into the runtime config), the
deterministic signing secret, the strength policy on the config write
interface and the startup migration that strips plaintext from disk.

All secret strings here are obviously synthetic unit fixtures; none is a
usable credential for any system.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod
from config import hash_web_password, verify_web_password

# Synthetic fixtures (never real credentials), kept as named constants so no
# dict literal below ever carries a raw secret-looking value inline.
PW_PRIMARY = "unit-fixture-pass-a"
PW_SECONDARY = "unit-fixture-pass-b"
PW_UNICODE = "密码-unit-fixture-é"
PW_LEGACY_RUNTIME = "legacy-runtime-fixture"
PW_LEGACY_STALE = "stale-legacy-fixture"
PW_HASH_SIDE = "hash-side-fixture"
PW_FIRST_ROTATE = "rotate-first-fixture"
PW_SECOND_ROTATE = "rotate-second-fixture"
PW_STRONG = "strong-unit-fixture-9"
PW_MIGRATE_A = "migrate-fixture-a-77"
PW_MIGRATE_B = "migrate-fixture-b-88"
PW_WRONG = "wrong-fixture"


# ---------------------------------------------------------------- hash format

def test_hash_format_is_salted_pbkdf2():
    encoded = hash_web_password(PW_PRIMARY)
    parts = encoded.split("$")
    assert len(parts) == 4
    assert parts[0] == "pbkdf2_sha256"
    assert int(parts[1]) >= 100_000
    # 16-byte salt -> 32 hex chars; sha256 digest -> 64 hex chars
    assert len(parts[2]) == 32
    assert len(parts[3]) == 64


def test_hash_is_salted_per_call():
    a = hash_web_password(PW_PRIMARY)
    b = hash_web_password(PW_PRIMARY)
    assert a != b, "each encoding must use a fresh random salt"


def test_verify_accepts_correct_rejects_wrong():
    encoded = hash_web_password(PW_PRIMARY)
    assert verify_web_password(PW_PRIMARY, encoded) is True
    assert verify_web_password(PW_SECONDARY, encoded) is False
    assert verify_web_password("", encoded) is False


def test_verify_rejects_malformed_encoded_values():
    for bad in ("", "plain", "md5$1$ab$cd", "pbkdf2_sha256$x$y$z",
                "pbkdf2_sha256$0$ab$cd", "pbkdf2_sha256$999999999$ab$cd"):
        assert verify_web_password(PW_PRIMARY, bad) is False


def test_verify_handles_unicode_candidate():
    encoded = hash_web_password(PW_UNICODE)
    assert verify_web_password(PW_UNICODE, encoded) is True
    assert verify_web_password(PW_SECONDARY, encoded) is False


# ------------------------------------------------- channel auth integration

@pytest.fixture()
def channel_module():
    from channel.web import web_channel
    return web_channel


def _set_conf(channel_module, payload: dict, monkeypatch):
    monkeypatch.setattr(channel_module, "conf", lambda: payload)


def test_password_matches_via_stored_hash(channel_module, monkeypatch):
    encoded = hash_web_password(PW_PRIMARY)
    _set_conf(channel_module, {"web_password_hash": encoded}, monkeypatch)
    assert channel_module._password_matches(PW_PRIMARY) is True
    assert channel_module._password_matches(PW_WRONG) is False


def test_password_matches_via_legacy_plaintext_runtime(channel_module, monkeypatch):
    runtime_conf = {"web_password": PW_LEGACY_RUNTIME}
    _set_conf(channel_module, runtime_conf, monkeypatch)
    assert channel_module._password_matches(PW_LEGACY_RUNTIME) is True
    assert channel_module._password_matches(PW_WRONG) is False


def test_password_matches_disabled_when_no_credentials(channel_module, monkeypatch):
    _set_conf(channel_module, {}, monkeypatch)
    assert channel_module._is_password_enabled() is False


def test_hash_takes_precedence_over_legacy(channel_module, monkeypatch):
    encoded = hash_web_password(PW_HASH_SIDE)
    _set_conf(
        channel_module,
        {"web_password_hash": encoded, "web_password": PW_LEGACY_STALE},
        monkeypatch,
    )
    assert channel_module._password_matches(PW_HASH_SIDE) is True
    assert channel_module._password_matches(PW_LEGACY_STALE) is False


def test_signing_secret_is_stable_for_legacy_runtime(channel_module, monkeypatch):
    runtime_conf = {"web_password": PW_LEGACY_RUNTIME}
    _set_conf(channel_module, runtime_conf, monkeypatch)
    a = channel_module._get_web_password()
    b = channel_module._get_web_password()
    assert a == b
    assert a.startswith("legacy$")


def test_signing_secret_changes_with_password_rotation(channel_module, monkeypatch):
    runtime_conf = {"web_password": PW_FIRST_ROTATE}
    _set_conf(channel_module, runtime_conf, monkeypatch)
    first = channel_module._get_web_password()
    runtime_conf["web_password"] = PW_SECOND_ROTATE
    second = channel_module._get_web_password()
    assert first != second


# ------------------------------------------------- write-interface policy

def test_strength_policy_rejects_short_and_common():
    from channel.web.web_channel import _check_password_strength
    for weak in ("12345678", "password", "PASSWORD1", "short1", "", "1234567890"):
        ok, reason = _check_password_strength(weak)
        assert ok is False, weak
        assert reason in ("password_too_short", "password_too_common")
    ok, reason = _check_password_strength(PW_STRONG)
    assert ok is True
    assert reason == ""


# ------------------------------------------------- startup migration (disk)

def _write_user_config(tmp_path: Path, payload: dict) -> Path:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    return cfg


def test_load_config_migrates_plaintext_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    _write_user_config(tmp_path, {"web_password": PW_MIGRATE_A, "web_port": 9899})
    config_mod.load_config()
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "web_password" not in on_disk, "plaintext must be physically removed"
    encoded = on_disk.get("web_password_hash", "")
    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_web_password(PW_MIGRATE_A, encoded) is True
    assert config_mod.conf().get("web_password_hash") == encoded
    assert "web_password" not in config_mod.conf()


def test_load_config_keeps_empty_password_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    _write_user_config(tmp_path, {"web_password": "", "web_port": 9899})
    config_mod.load_config()
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk.get("web_password") == ""
    assert on_disk.get("web_password_hash", "") == ""


def test_load_config_does_not_write_template_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    config_mod.load_config()
    assert not (tmp_path / "config.json").exists()


def test_load_config_preserves_other_keys_during_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("COW_DATA_DIR", str(tmp_path))
    _write_user_config(tmp_path, {"web_password": PW_MIGRATE_B, "web_port": 9999, "cow_lang": "en"})
    config_mod.load_config()
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk.get("web_port") == 9999
    assert on_disk.get("cow_lang") == "en"
    assert "web_password_hash" in on_disk
