"""客户验收的独立确定性和盲评判定器。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Sequence

from common.path_safety import has_link_or_reparse_component

from .attestation import clean_ed25519_signature
from .contracts import (
    CustomerExecutionError,
    CustomerJudgment,
    CustomerJudgmentRequest,
)
from .json_utils import (
    canonical_json_bytes,
    clean_sha256,
    clean_text,
    strict_json_loads,
)


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CustomerCaseJudge(ABC):
    """在不知道技能正文的边界内判定单臂输出。"""

    @property
    @abstractmethod
    def judge_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def judge_version(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def judge(self, request: CustomerJudgmentRequest) -> CustomerJudgment:
        raise NotImplementedError


class DeterministicCustomerCaseJudge(CustomerCaseJudge):
    """用严格规范 JSON 等值判定确定性客户任务。"""

    @property
    def judge_id(self) -> str:
        return "deterministic-json-equality"

    @property
    def judge_version(self) -> str:
        return "1.0.0"

    def judge(self, request: CustomerJudgmentRequest) -> CustomerJudgment:
        expected = canonical_json_bytes(request.oracle)
        actual = canonical_json_bytes(request.output)
        return CustomerJudgment(
            success=actual == expected,
            evidence={
                "comparison": "canonical-json-equality",
                "expected_sha256": hashlib.sha256(expected).hexdigest(),
                "actual_sha256": hashlib.sha256(actual).hexdigest(),
            },
        )


class SubprocessCustomerCaseJudge(CustomerCaseJudge):
    """通过固定命令调用与 Agent 执行器分离的客户盲评环境。"""

    def __init__(
        self,
        command: Sequence[str],
        *,
        judge_id: str,
        judge_version: str,
        run_root: Path,
        timeout_seconds: float = 300.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command or any(
            not isinstance(part, str) or not part for part in command
        ):
            raise CustomerExecutionError("判定器命令不能为空")
        if timeout_seconds <= 0:
            raise CustomerExecutionError("判定器超时必须大于零")
        self._command = tuple(command)
        self._judge_id = clean_text(judge_id, "judge_id", 128)
        self._judge_version = clean_text(judge_version, "judge_version", 128)
        lexical_run_root = Path(os.path.abspath(os.fspath(run_root)))
        if has_link_or_reparse_component(lexical_run_root):
            raise CustomerExecutionError("判定器工作根目录包含链接或重解析点")
        try:
            lexical_run_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CustomerExecutionError("无法创建判定器工作根目录") from error
        if has_link_or_reparse_component(lexical_run_root):
            raise CustomerExecutionError("判定器工作根目录包含链接或重解析点")
        self._run_root = lexical_run_root.resolve(strict=True)
        self._timeout_seconds = timeout_seconds
        self._environment = dict(environment) if environment is not None else None

    @property
    def judge_id(self) -> str:
        return self._judge_id

    @property
    def judge_version(self) -> str:
        return self._judge_version

    def judge(self, request: CustomerJudgmentRequest) -> CustomerJudgment:
        payload = canonical_json_bytes(
            {
                "schema_version": 1,
                "run_id": request.run_id,
                "case_id": request.case_id,
                "arm_label": request.arm_label,
                "oracle_id": request.oracle_id,
                "oracle": request.oracle,
                "output": request.output,
            }
        )
        with tempfile.TemporaryDirectory(
            prefix="judge-%s-" % request.case_id[:32],
            dir=str(self._run_root),
        ) as workspace:
            environment = (
                os.environ.copy()
                if self._environment is None
                else dict(self._environment)
            )
            environment["SMART_ASSISTANT_CUSTOMER_JUDGE_WORKSPACE"] = workspace
            try:
                completed = subprocess.run(
                    self._command,
                    input=payload,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=workspace,
                    env=environment,
                    timeout=self._timeout_seconds,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CustomerExecutionError("客户独立判定器调用失败") from error
        if completed.returncode != 0:
            raise CustomerExecutionError(
                "客户独立判定器返回非零退出码: %d" % completed.returncode
            )
        if len(completed.stdout) > _MAX_RESPONSE_BYTES:
            raise CustomerExecutionError("客户独立判定器响应超过大小上限")
        response = strict_json_loads(completed.stdout, "独立判定器响应")
        return _parse_judgment(response, request)


def _parse_judgment(
    response: Any, request: CustomerJudgmentRequest
) -> CustomerJudgment:
    expected = {
        "schema_version", "run_id", "case_id", "arm_label",
        "oracle_id", "success", "evidence", "judge_artifact_sha256",
        "attestation_signature",
    }
    if not isinstance(response, dict) or set(response) != expected:
        raise CustomerExecutionError("独立判定器响应字段不符合协议")
    bindings = (
        ("schema_version", response["schema_version"], 1),
        ("run_id", response["run_id"], request.run_id),
        ("case_id", response["case_id"], request.case_id),
        ("arm_label", response["arm_label"], request.arm_label),
        ("oracle_id", response["oracle_id"], request.oracle_id),
    )
    for field_name, actual, expected_value in bindings:
        if actual != expected_value:
            raise CustomerExecutionError(
                "独立判定器响应 %s 与请求不一致" % field_name
            )
    if not isinstance(response["success"], bool):
        raise CustomerExecutionError("独立判定器 success 必须是布尔值")
    canonical_json_bytes(response["evidence"])
    return CustomerJudgment(
        success=response["success"],
        evidence=response["evidence"],
        judge_artifact_sha256=clean_sha256(
            response["judge_artifact_sha256"], "judge_artifact_sha256"
        ),
        attestation_signature=clean_ed25519_signature(
            response["attestation_signature"], "attestation_signature"
        ),
    )
