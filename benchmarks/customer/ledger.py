"""Durable fail-closed ledger for customer-acceptance side effects."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from common.path_safety import has_link_or_reparse_component

from .contracts import CustomerPackageError
from .json_utils import (
    canonical_json_bytes,
    clean_sha256,
    clean_text,
    sha256_json,
    strict_json_loads,
)


class CustomerExecutionLedger:
    """Reserve a customer package and each case arm before external execution.

    The ledger has no lease expiry or automatic takeover. A predecessor may have
    sent a request after its durable intent but before its receipt was recorded,
    so a later process must surface in_doubt instead of replaying it.
    """

    def __init__(self, path: Path) -> None:
        try:
            lexical = Path(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError) as error:
            raise CustomerPackageError("customer ledger path is invalid") from error
        if not lexical.name or has_link_or_reparse_component(lexical):
            raise CustomerPackageError("customer ledger path is unsafe")
        try:
            lexical.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CustomerPackageError(
                "customer ledger directory cannot be created"
            ) from error
        if has_link_or_reparse_component(lexical):
            raise CustomerPackageError("customer ledger path is unsafe")
        self.path = lexical.resolve(strict=False)
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        """Enable WAL with bounded backoff during concurrent process startup."""

        for attempt in range(8):
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            connection = self._connect()
            try:
                self._enable_wal(connection)
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS customer_acceptance_runs (
                        package_manifest_sha256 TEXT NOT NULL,
                        cases_sha256 TEXT NOT NULL,
                        run_id TEXT NOT NULL UNIQUE,
                        run_binding_sha256 TEXT NOT NULL,
                        implementation_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(
                            state IN ('running', 'completed', 'in_doubt')
                        ),
                        detail TEXT,
                        report_json TEXT,
                        report_sha256 TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(package_manifest_sha256, cases_sha256)
                    );
                    CREATE TABLE IF NOT EXISTS customer_acceptance_operations (
                        package_manifest_sha256 TEXT NOT NULL,
                        cases_sha256 TEXT NOT NULL,
                        case_id TEXT NOT NULL,
                        arm TEXT NOT NULL CHECK(arm IN ('baseline', 'candidate')),
                        run_id TEXT NOT NULL,
                        request_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(
                            state IN (
                                'planned', 'intent', 'execution_receipt',
                                'judgment_intent', 'completed', 'in_doubt'
                            )
                        ),
                        execution_receipt_sha256 TEXT,
                        execution_receipt_json TEXT,
                        judgment_receipt_sha256 TEXT,
                        judgment_receipt_json TEXT,
                        detail TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(
                            package_manifest_sha256, cases_sha256, case_id, arm
                        ),
                        FOREIGN KEY(package_manifest_sha256, cases_sha256)
                            REFERENCES customer_acceptance_runs(
                                package_manifest_sha256, cases_sha256
                            )
                            ON DELETE RESTRICT
                    );
                    CREATE INDEX IF NOT EXISTS
                    idx_customer_acceptance_operations_run
                    ON customer_acceptance_operations(
                        package_manifest_sha256, cases_sha256, state
                    );
                    CREATE TABLE IF NOT EXISTS customer_execution_ledger_schema (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        schema_version INTEGER NOT NULL
                    );
                    """
                )
                # Version 1 stored receipt digests only.  Such rows remain
                # readable, but cannot be safely replayed because their output
                # and judgment evidence are unavailable.  Version 2 stores a
                # canonical, hash-bound receipt so recovery can reuse it.
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(customer_acceptance_operations)"
                    )
                }
                for name in (
                    "execution_receipt_json",
                    "judgment_receipt_json",
                ):
                    if name not in columns:
                        connection.execute(
                            "ALTER TABLE customer_acceptance_operations "
                            "ADD COLUMN %s TEXT" % name
                        )
                connection.execute(
                    "INSERT INTO customer_execution_ledger_schema("
                    "singleton, schema_version) VALUES (1, 2) "
                    "ON CONFLICT(singleton) DO UPDATE SET schema_version = "
                    "MAX(schema_version, excluded.schema_version)"
                )
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                self._initialized = True
            except sqlite3.Error as error:
                raise CustomerPackageError(
                    "customer execution ledger schema is unavailable"
                ) from error
            finally:
                connection.close()

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass

    def _commit(self, connection: sqlite3.Connection) -> None:
        """Commit using SQLite's FULL-sync transaction boundary.

        SQLite owns database/WAL locking and durability.  Reopening either file
        while a connection is live bypasses that VFS contract on Windows and can
        corrupt the journal, so callers must rely on ``synchronous=FULL`` set by
        :meth:`_connect` instead of attempting a second raw-file flush.
        """

        connection.execute("COMMIT")

    @staticmethod
    def _detail(value: object | None) -> str | None:
        if value is None:
            return None
        return (
            str(value)
            .replace("\x00", " ")
            .replace("\r", " ")
            .replace("\n", " ")[:512]
        )

    @staticmethod
    def _package_keys(
        package_manifest_sha256: str, cases_sha256: str
    ) -> tuple[str, str]:
        return (
            clean_sha256(package_manifest_sha256, "package_manifest_sha256"),
            clean_sha256(cases_sha256, "cases_sha256"),
        )

    @staticmethod
    def _operation_values(
        run_id: str, case_id: str, arm: str, request_sha256: str
    ) -> tuple[str, str, str, str]:
        run_id = clean_text(run_id, "run_id", 128)
        case_id = clean_text(case_id, "case_id", 256)
        if arm not in {"baseline", "candidate"}:
            raise CustomerPackageError("customer execution arm is invalid")
        return (
            run_id,
            case_id,
            arm,
            clean_sha256(request_sha256, "request_sha256"),
        )

    @staticmethod
    def _plan_values(
        operation_plan: Sequence[Mapping[str, Any]],
    ) -> Tuple[Tuple[str, str, str], ...]:
        if (
            not isinstance(operation_plan, Sequence)
            or isinstance(operation_plan, (str, bytes))
            or not operation_plan
        ):
            raise CustomerPackageError("customer execution plan is invalid")
        normalized = []
        seen = set()
        for entry in operation_plan:
            if not isinstance(entry, Mapping) or set(entry) != {
                "case_id",
                "arm",
                "request_sha256",
            }:
                raise CustomerPackageError("customer execution plan entry is invalid")
            case_id = clean_text(entry["case_id"], "case_id", 256)
            arm = entry["arm"]
            if arm not in {"baseline", "candidate"}:
                raise CustomerPackageError("customer execution plan arm is invalid")
            request_sha256 = clean_sha256(
                entry["request_sha256"],
                "request_sha256",
            )
            key = (case_id, arm)
            if key in seen:
                raise CustomerPackageError("customer execution plan has duplicates")
            seen.add(key)
            normalized.append((case_id, arm, request_sha256))
        return tuple(normalized)

    @staticmethod
    def _run_summary(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "state": str(row["state"]),
            "run_id": str(row["run_id"]),
            "run_binding_sha256": str(row["run_binding_sha256"]),
            "implementation_sha256": str(row["implementation_sha256"]),
            "detail": row["detail"],
        }

    def claim_run(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        run_binding_sha256: str,
        implementation_sha256: str,
        operation_plan: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Atomically reserve a package before any executor can be called."""

        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id = clean_text(run_id, "run_id", 128)
        binding = clean_sha256(run_binding_sha256, "run_binding_sha256")
        implementation = clean_sha256(
            implementation_sha256, "implementation_sha256"
        )
        plan = self._plan_values(operation_plan)
        self._ensure_schema()
        connection = self._connect()
        now = time.time()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM customer_acceptance_runs
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                """,
                (manifest, cases),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO customer_acceptance_runs(
                        package_manifest_sha256, cases_sha256, run_id,
                        run_binding_sha256, implementation_sha256, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (manifest, cases, run_id, binding, implementation, now, now),
                )
                connection.executemany(
                    """
                    INSERT INTO customer_acceptance_operations(
                        package_manifest_sha256, cases_sha256, case_id, arm,
                        run_id, request_sha256, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?, ?)
                    """,
                    [
                        (
                            manifest,
                            cases,
                            case_id,
                            arm,
                            run_id,
                            request_sha256,
                            now,
                            now,
                        )
                        for case_id, arm, request_sha256 in plan
                    ],
                )
                self._commit(connection)
                return {"claim_status": "claimed", "state": "running"}

            summary = self._run_summary(row)
            if (
                summary["state"] == "completed"
                and summary["run_id"] == run_id
                and summary["run_binding_sha256"] == binding
                and summary["implementation_sha256"] == implementation
            ):
                try:
                    report = strict_json_loads(
                        str(row["report_json"]).encode("utf-8"),
                        "completed customer report",
                    )
                    report_sha256 = sha256_json(report)
                except CustomerPackageError as error:
                    raise CustomerPackageError(
                        "completed customer report is unreadable"
                    ) from error
                if (
                    not isinstance(report, dict)
                    or report.get("status") != "completed"
                    or not isinstance(report.get("passed"), bool)
                    or report.get("run_id") != run_id
                ):
                    raise CustomerPackageError(
                        "completed customer report has an invalid identity"
                    )
                if (
                    not isinstance(row["report_sha256"], str)
                    or report_sha256 != row["report_sha256"]
                ):
                    raise CustomerPackageError(
                        "completed customer report hash is invalid"
                    )
                self._commit(connection)
                return {
                    "claim_status": "completed",
                    "state": "completed",
                    "report": report,
                }

            # A prior process may have crashed after a durable receipt but
            # before it rebuilt the in-memory event chain or final report.  It
            # is safe to resume only when no external effect is outstanding:
            # ``intent`` and ``judgment_intent`` are deliberately fenced as
            # in_doubt and never automatically replayed.
            if (
                summary["state"] == "running"
                and summary["run_id"] == run_id
                and summary["run_binding_sha256"] == binding
                and summary["implementation_sha256"] == implementation
            ):
                states = {
                    str(item["state"])
                    for item in connection.execute(
                        "SELECT state FROM customer_acceptance_operations "
                        "WHERE package_manifest_sha256 = ? "
                        "AND cases_sha256 = ?",
                        (manifest, cases),
                    ).fetchall()
                }
                if (
                    states.intersection({"intent", "judgment_intent", "in_doubt"})
                    or not states.intersection({"execution_receipt", "completed"})
                ):
                    self._commit(connection)
                    return {"claim_status": "in_doubt", **summary}
                self._commit(connection)
                return {"claim_status": "resumable", **summary}

            self._commit(connection)
            return {"claim_status": "in_doubt", **summary}
        except CustomerPackageError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise CustomerPackageError(
                "customer execution ledger cannot claim a run"
            ) from error
        finally:
            connection.close()

    def claim_case_operation(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        case_id: str,
        arm: str,
        request_sha256: str,
    ) -> Dict[str, Any]:
        """Persist an intent before calling the executor."""

        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id, case_id, arm, request_sha256 = self._operation_values(
            run_id, case_id, arm, request_sha256
        )
        self._ensure_schema()
        connection = self._connect()
        now = time.time()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT state, run_id FROM customer_acceptance_runs
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                """,
                (manifest, cases),
            ).fetchone()
            if (
                run is None
                or str(run["state"]) != "running"
                or str(run["run_id"]) != run_id
            ):
                raise CustomerPackageError(
                    "customer execution run is not actively owned"
                )
            row = connection.execute(
                """
                SELECT state, run_id, request_sha256, detail
                FROM customer_acceptance_operations
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND case_id = ? AND arm = ?
                """,
                (manifest, cases, case_id, arm),
            ).fetchone()
            if row is None:
                raise CustomerPackageError(
                    "customer execution operation is not in the reserved plan"
                )
            if (
                str(row["run_id"]) != run_id
                or str(row["request_sha256"]) != request_sha256
            ):
                raise CustomerPackageError(
                    "customer execution operation does not match the reserved plan"
                )
            if str(row["state"]) != "planned":
                self._commit(connection)
                return {
                    "claim_status": "in_doubt",
                    "state": str(row["state"]),
                    "run_matches": True,
                    "request_matches": True,
                    "detail": row["detail"],
                }
            cursor = connection.execute(
                """
                UPDATE customer_acceptance_operations
                SET state = 'intent', updated_at = ?
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND case_id = ? AND arm = ? AND run_id = ?
                  AND request_sha256 = ? AND state = 'planned'
                """,
                (
                    now,
                    manifest,
                    cases,
                    case_id,
                    arm,
                    run_id,
                    request_sha256,
                ),
            )
            if cursor.rowcount != 1:
                raise CustomerPackageError(
                    "customer execution operation intent was rejected"
                )
            self._commit(connection)
            return {"claim_status": "claimed", "state": "intent"}
        except CustomerPackageError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise CustomerPackageError(
                "customer execution ledger cannot persist an intent"
            ) from error
        finally:
            connection.close()

    def _transition(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        case_id: str,
        arm: str,
        request_sha256: str,
        *,
        expected: str,
        next_state: str,
        receipt_field: str | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> None:
        valid_states = {
            "planned",
            "intent",
            "execution_receipt",
            "judgment_intent",
            "completed",
            "in_doubt",
        }
        if expected not in valid_states or next_state not in valid_states:
            raise CustomerPackageError("customer ledger transition is invalid")
        if receipt_field not in {
            None,
            "execution_receipt_sha256",
            "judgment_receipt_sha256",
        }:
            raise CustomerPackageError("customer ledger receipt field is invalid")
        if (receipt_field is None) != (receipt is None):
            raise CustomerPackageError("customer ledger receipt is invalid")
        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id, case_id, arm, request_sha256 = self._operation_values(
            run_id, case_id, arm, request_sha256
        )
        receipt_value = dict(receipt) if receipt is not None else None
        receipt_sha256 = (
            sha256_json(receipt_value) if receipt_value is not None else None
        )
        receipt_json = (
            canonical_json_bytes(receipt_value).decode("utf-8")
            if receipt_value is not None
            else None
        )
        self._ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if receipt_field is None:
                cursor = connection.execute(
                    """
                    UPDATE customer_acceptance_operations
                    SET state = ?, updated_at = ?
                    WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                      AND run_id = ? AND case_id = ? AND arm = ?
                      AND request_sha256 = ? AND state = ?
                    """,
                    (
                        next_state,
                        time.time(),
                        manifest,
                        cases,
                        run_id,
                        case_id,
                        arm,
                        request_sha256,
                        expected,
                    ),
                )
            else:
                receipt_json_field = receipt_field.replace("_sha256", "_json")
                cursor = connection.execute(
                    f"""
                    UPDATE customer_acceptance_operations
                    SET state = ?, {receipt_field} = ?, {receipt_json_field} = ?,
                        updated_at = ?
                    WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                      AND run_id = ? AND case_id = ? AND arm = ?
                      AND request_sha256 = ? AND state = ?
                    """,
                    (
                        next_state,
                        receipt_sha256,
                        receipt_json,
                        time.time(),
                        manifest,
                        cases,
                        run_id,
                        case_id,
                        arm,
                        request_sha256,
                        expected,
                    ),
                )
            if cursor.rowcount != 1:
                raise CustomerPackageError(
                    "customer ledger transition was rejected"
                )
            self._commit(connection)
        except CustomerPackageError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise CustomerPackageError(
                "customer ledger cannot persist a receipt"
            ) from error
        finally:
            connection.close()

    def record_execution_receipt(self, *args, receipt: Mapping[str, Any]) -> None:
        self._transition(
            *args,
            expected="intent",
            next_state="execution_receipt",
            receipt_field="execution_receipt_sha256",
            receipt=receipt,
        )

    def begin_judgment(self, *args) -> None:
        self._transition(
            *args,
            expected="execution_receipt",
            next_state="judgment_intent",
        )

    def record_completed_operation(
        self, *args, judgment_receipt: Mapping[str, Any]
    ) -> None:
        self._transition(
            *args,
            expected="judgment_intent",
            next_state="completed",
            receipt_field="judgment_receipt_sha256",
            receipt=judgment_receipt,
        )

    def mark_case_in_doubt(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        case_id: str,
        arm: str,
        request_sha256: str,
        detail: object,
    ) -> None:
        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id, case_id, arm, request_sha256 = self._operation_values(
            run_id, case_id, arm, request_sha256
        )
        self._ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE customer_acceptance_operations
                SET state = 'in_doubt', detail = ?, updated_at = ?
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND run_id = ? AND case_id = ? AND arm = ?
                  AND request_sha256 = ?
                  AND state IN ('intent', 'judgment_intent')
                """,
                (
                    self._detail(detail),
                    time.time(),
                    manifest,
                    cases,
                    run_id,
                    case_id,
                    arm,
                    request_sha256,
                ),
            )
            self._commit(connection)
        except sqlite3.Error as error:
            self._rollback(connection)
            raise CustomerPackageError(
                "customer ledger cannot mark an uncertain operation"
            ) from error
        finally:
            connection.close()

    def load_operation_receipts(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        case_id: str,
        arm: str,
        request_sha256: str,
    ) -> Dict[str, Any]:
        """Load hash-verified receipts without permitting an executor replay."""

        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id, case_id, arm, request_sha256 = self._operation_values(
            run_id, case_id, arm, request_sha256
        )
        self._ensure_schema()
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT state, execution_receipt_sha256, execution_receipt_json,
                       judgment_receipt_sha256, judgment_receipt_json, detail
                FROM customer_acceptance_operations
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND run_id = ? AND case_id = ? AND arm = ?
                  AND request_sha256 = ?
                """,
                (manifest, cases, run_id, case_id, arm, request_sha256),
            ).fetchone()
            if row is None:
                raise CustomerPackageError("customer execution receipt is absent")

            def receipt(name: str) -> Dict[str, Any] | None:
                encoded = row[name + "_json"]
                digest = row[name + "_sha256"]
                if encoded is None and digest is None:
                    return None
                if not isinstance(encoded, str) or not isinstance(digest, str):
                    raise CustomerPackageError(
                        "customer execution receipt is not recoverable"
                    )
                value = strict_json_loads(
                    encoded.encode("utf-8"), "customer execution receipt"
                )
                if not isinstance(value, dict) or sha256_json(value) != digest:
                    raise CustomerPackageError(
                        "customer execution receipt integrity is invalid"
                    )
                return value

            return {
                "state": str(row["state"]),
                "detail": row["detail"],
                "execution_receipt": receipt("execution_receipt"),
                "judgment_receipt": receipt("judgment_receipt"),
            }
        except sqlite3.Error as error:
            raise CustomerPackageError(
                "customer ledger cannot read an execution receipt"
            ) from error
        finally:
            connection.close()

    def is_recoverable_run(
        self, package_manifest_sha256: str, cases_sha256: str, run_id: str
    ) -> bool:
        """Whether a crash left only durable, non-executing recovery work."""

        record = self.describe_run(package_manifest_sha256, cases_sha256)
        if record is None or record["state"] != "running" or record["run_id"] != run_id:
            return False
        states = set(record["operation_states"])
        return bool(states.intersection({"execution_receipt", "completed"})) and not bool(
            states.intersection({"intent", "judgment_intent", "in_doubt"})
        )

    def mark_run_in_doubt(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        detail: object,
    ) -> None:
        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id = clean_text(run_id, "run_id", 128)
        self._ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE customer_acceptance_runs
                SET state = 'in_doubt', detail = ?, updated_at = ?
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND run_id = ? AND state = 'running'
                """,
                (
                    self._detail(detail),
                    time.time(),
                    manifest,
                    cases,
                    run_id,
                ),
            )
            self._commit(connection)
        except sqlite3.Error as error:
            self._rollback(connection)
            raise CustomerPackageError(
                "customer ledger cannot mark an uncertain run"
            ) from error
        finally:
            connection.close()

    def _completed_report_operation_keys(
        self,
        report: Mapping[str, Any],
        manifest: str,
        cases: str,
        run_id: str,
    ) -> Tuple[Tuple[str, str, str], ...]:
        try:
            report_value = dict(report)
        except (TypeError, ValueError) as error:
            raise CustomerPackageError("customer final report is invalid") from error
        if (
            report_value.get("status") != "completed"
            or not isinstance(report_value.get("passed"), bool)
            or report_value.get("run_id") != run_id
        ):
            raise CustomerPackageError("customer final report identity is invalid")
        package = report_value.get("package")
        if not isinstance(package, Mapping) or (
            package.get("manifest_sha256") != manifest
            or package.get("cases_sha256") != cases
        ):
            raise CustomerPackageError("customer final report package is invalid")
        events = report_value.get("events")
        if not isinstance(events, list):
            raise CustomerPackageError("customer final report events are invalid")
        operation_keys = []
        for event in events:
            if not isinstance(event, Mapping) or event.get("event_type") != (
                "case.executed"
            ):
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise CustomerPackageError(
                    "customer final report operation is invalid"
                )
            case_id = clean_text(payload.get("case_id"), "case_id", 256)
            arm = payload.get("arm")
            if arm not in {"baseline", "candidate"}:
                raise CustomerPackageError(
                    "customer final report operation arm is invalid"
                )
            operation_keys.append(
                (
                    case_id,
                    arm,
                    clean_sha256(
                        payload.get("request_sha256"),
                        "request_sha256",
                    ),
                )
            )
        if not operation_keys or len(operation_keys) != len(set(operation_keys)):
            raise CustomerPackageError(
                "customer final report operations are incomplete"
            )
        return tuple(sorted(operation_keys))

    def complete_run(
        self,
        package_manifest_sha256: str,
        cases_sha256: str,
        run_id: str,
        report: Mapping[str, Any],
    ) -> None:
        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        run_id = clean_text(run_id, "run_id", 128)
        try:
            report_value = dict(report)
        except (TypeError, ValueError) as error:
            raise CustomerPackageError("customer final report is invalid") from error
        operation_keys = self._completed_report_operation_keys(
            report_value,
            manifest,
            cases,
            run_id,
        )
        report_json = canonical_json_bytes(report_value).decode("utf-8")
        report_sha256 = sha256_json(report_value)
        self._ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT case_id, arm, request_sha256, state
                FROM customer_acceptance_operations
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND run_id = ?
                ORDER BY case_id, arm
                """,
                (manifest, cases, run_id),
            ).fetchall()
            planned_keys = tuple(
                (str(row["case_id"]), str(row["arm"]), str(row["request_sha256"]))
                for row in rows
            )
            if (
                not planned_keys
                or any(str(row["state"]) != "completed" for row in rows)
                or operation_keys != planned_keys
            ):
                raise CustomerPackageError(
                    "customer execution plan is not completely settled"
                )
            cursor = connection.execute(
                """
                UPDATE customer_acceptance_runs
                SET state = 'completed', detail = NULL, report_json = ?,
                    report_sha256 = ?, updated_at = ?
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                  AND run_id = ? AND state = 'running'
                """,
                (
                    report_json,
                    report_sha256,
                    time.time(),
                    manifest,
                    cases,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise CustomerPackageError("customer ledger completion was rejected")
            self._commit(connection)
        except CustomerPackageError:
            self._rollback(connection)
            raise
        except sqlite3.Error as error:
            self._rollback(connection)
            raise CustomerPackageError(
                "customer ledger cannot persist the final report"
            ) from error
        finally:
            connection.close()

    def describe_run(
        self, package_manifest_sha256: str, cases_sha256: str
    ) -> Dict[str, Any] | None:
        manifest, cases = self._package_keys(package_manifest_sha256, cases_sha256)
        self._ensure_schema()
        connection = self._connect()
        try:
            run = connection.execute(
                """
                SELECT * FROM customer_acceptance_runs
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                """,
                (manifest, cases),
            ).fetchone()
            if run is None:
                return None
            states = connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM customer_acceptance_operations
                WHERE package_manifest_sha256 = ? AND cases_sha256 = ?
                GROUP BY state ORDER BY state
                """,
                (manifest, cases),
            ).fetchall()
            return {
                **self._run_summary(run),
                "operation_count": sum(int(row["count"]) for row in states),
                "operation_states": {
                    str(row["state"]): int(row["count"]) for row in states
                },
            }
        except sqlite3.Error as error:
            raise CustomerPackageError(
                "customer ledger cannot read run state"
            ) from error
        finally:
            connection.close()
