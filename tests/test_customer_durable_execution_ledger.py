"""Crash-window proof for the customer acceptance durable execution ledger."""

from __future__ import annotations

import sqlite3

import pytest

import benchmarks.customer.ledger as ledger_module
from benchmarks.customer import ControlledCustomerAcceptanceRunner, load_customer_package
from benchmarks.customer.verify import verify_customer_report

# Reuse the signed, independently verifiable customer-package fixture.  Keeping
# these tests against the real runner prevents a ledger-only mock from claiming
# an at-most-once guarantee that the executor call path does not actually keep.
from test_customer_acceptance import (
    FixtureCustomerExecutor,
    _TENANT_ID,
    _candidate,
    _package,
)


@pytest.mark.parametrize(
    ("point", "expected_status", "expected_passed", "expected_recovery_effects"),
    [
        ("before_effect", "in_doubt", False, 0),
        ("after_effect_before_receipt", "in_doubt", False, 0),
        ("after_receipt_before_chain", "completed", True, 59),
        ("before_final_report_commit", "completed", True, 0),
    ],
)
def test_durable_ledger_fault_windows_preserve_at_most_once_execution(
    tmp_path,
    point,
    expected_status,
    expected_passed,
    expected_recovery_effects,
):
    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "durable-customer-ledger.sqlite3"
        first_executor = FixtureCustomerExecutor()
        fired = {"value": False}

        def crash_at(boundary: str) -> None:
            if boundary == point and not fired["value"]:
                fired["value"] = True
                raise SystemExit("deterministic crash at " + boundary)

        runner = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            first_executor,
            execution_ledger_path=ledger_path,
            fault_injector=crash_at,
        )
        with pytest.raises(SystemExit, match=point):
            runner.run(
                package,
                skill_id=record.skill_id,
                skill_version=record.version,
                expected_skill_content_sha256=record.content_hash,
            )
        assert fired["value"] is True

        recovery_executor = FixtureCustomerExecutor()
        recovered = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            recovery_executor,
            execution_ledger_path=ledger_path,
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert recovered["status"] == expected_status
        assert recovered["passed"] is expected_passed
        assert len(recovery_executor.requests) == expected_recovery_effects
        all_effects = [
            (request.case_id, request.arm)
            for request in first_executor.requests + recovery_executor.requests
        ]
        assert len(all_effects) == len(set(all_effects))
        assert verify_customer_report(recovered, package) == ()
    finally:
        repository.close()

def test_durable_ledger_does_not_bypass_sqlite_wal_ownership(monkeypatch, tmp_path):
    """The SQLite VFS, not raw os.open calls, owns the WAL commit boundary."""

    repository, _, record = _candidate(tmp_path)
    try:
        root, manifest_sha256 = _package(tmp_path, record)
        package = load_customer_package(root, manifest_sha256)
        ledger_path = tmp_path / "durable-customer-ledger.sqlite3"

        def raw_open_is_forbidden(*_args, **_kwargs):
            raise AssertionError("ledger must not reopen SQLite or WAL files")

        monkeypatch.setattr(ledger_module.os, "open", raw_open_is_forbidden)
        report = ControlledCustomerAcceptanceRunner(
            repository,
            _TENANT_ID,
            FixtureCustomerExecutor(),
            execution_ledger_path=ledger_path,
        ).run(
            package,
            skill_id=record.skill_id,
            skill_version=record.version,
            expected_skill_content_sha256=record.content_hash,
        )

        assert report["status"] == "completed"
        connection = sqlite3.connect(ledger_path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()
    finally:
        repository.close()
