"""运行客户技能配对验收；缺输入时明确保持待定。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from agent.skills.governance import GovernedSkillRepository

from .contracts import CustomerAcceptanceError, CustomerPackageError
from .executor import SubprocessCustomerCaseExecutor
from .judge import SubprocessCustomerCaseJudge
from .json_utils import strict_json_loads
from .package import load_customer_package
from .runner import (
    ControlledCustomerAcceptanceRunner,
    pending_customer_report,
)
from .verify import verify_customer_report


def main(arguments: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="运行客户技能配对验收")
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--skills-db", type=Path)
    parser.add_argument("--tenant-id")
    parser.add_argument("--skill-id")
    parser.add_argument("--skill-version", type=int)
    parser.add_argument("--skill-content-sha256")
    parser.add_argument("--executor-json")
    parser.add_argument("--executor-id")
    parser.add_argument("--executor-version")
    parser.add_argument("--judge-json")
    parser.add_argument("--judge-id")
    parser.add_argument("--judge-version")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)

    required = {
        "package_root": args.package_root,
        "manifest_sha256": args.manifest_sha256,
        "skills_db": args.skills_db,
        "tenant_id": args.tenant_id,
        "skill_id": args.skill_id,
        "skill_version": args.skill_version,
        "skill_content_sha256": args.skill_content_sha256,
        "executor_json": args.executor_json,
        "executor_id": args.executor_id,
        "executor_version": args.executor_version,
        "run_root": args.run_root,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        report = pending_customer_report(missing)
        _render_report(report, args.output)
        return 2

    try:
        package = load_customer_package(
            args.package_root, args.manifest_sha256
        )
        executor_command = _command(args.executor_json, "executor_json")
        executor = SubprocessCustomerCaseExecutor(
            executor_command,
            executor_id=args.executor_id,
            executor_version=args.executor_version,
            run_root=args.run_root / "executor",
        )
        judge = None
        if args.judge_json is not None:
            judge_required = (args.judge_id, args.judge_version)
            if any(value is None for value in judge_required):
                report = pending_customer_report(
                    ["judge_id", "judge_version"]
                )
                _render_report(report, args.output)
                return 2
            judge = SubprocessCustomerCaseJudge(
                _command(args.judge_json, "judge_json"),
                judge_id=args.judge_id,
                judge_version=args.judge_version,
                run_root=args.run_root / "judge",
            )
        repository = GovernedSkillRepository(args.skills_db)
        try:
            runner = ControlledCustomerAcceptanceRunner(
                repository,
                args.tenant_id,
                executor,
                judge,
                execution_ledger_path=(
                    args.run_root / "customer_execution_ledger.sqlite3"
                ),
            )
            report = runner.run(
                package,
                skill_id=args.skill_id,
                skill_version=args.skill_version,
                expected_skill_content_sha256=args.skill_content_sha256,
            )
        finally:
            repository.close()
    except CustomerAcceptanceError as error:
        report = {
            "schema_version": 1,
            "status": "invalid_evidence",
            "passed": False,
            "verification_failures": [str(error)],
        }
        _render_report(report, args.output)
        return 3
    verification_failures = verify_customer_report(report, package)
    if verification_failures:
        report = dict(report)
        report["status"] = "invalid_evidence"
        report["passed"] = False
        report["verification_failures"] = list(verification_failures)
    _render_report(report, args.output)
    if report["status"] == "invalid_evidence":
        return 3
    return 0 if report["passed"] else 1


def _command(value: str, field_name: str) -> Sequence[str]:
    parsed = strict_json_loads(value.encode("utf-8"), field_name)
    if not isinstance(parsed, list) or not parsed or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise CustomerPackageError(
            "%s 必须是非空字符串数组" % field_name
        )
    return tuple(parsed)


def _render_report(report, output: Optional[Path]) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
