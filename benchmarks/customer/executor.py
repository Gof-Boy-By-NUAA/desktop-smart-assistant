"""客户验收的可信外部执行适配器。"""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from common.path_safety import has_link_or_reparse_component

from .attestation import clean_ed25519_signature
from .contracts import (
    CustomerExecutionError,
    CustomerExecutionRequest,
    CustomerExecutionResult,
)
from .json_utils import (
    canonical_json_bytes,
    clean_sha256,
    clean_text,
    sha256_json,
    strict_json_loads,
)


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CustomerCaseExecutor(ABC):
    """在隔离工作区执行一个客户样本的可信边界。"""

    @property
    @abstractmethod
    def executor_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def executor_version(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def execute(
        self, request: CustomerExecutionRequest
    ) -> CustomerExecutionResult:
        """执行一个固定模型、固定工具环境的单臂请求。"""

        raise NotImplementedError


class SubprocessCustomerCaseExecutor(CustomerCaseExecutor):
    """通过固定命令和严格 JSON 协议调用客户执行环境。"""

    def __init__(
        self,
        command: Sequence[str],
        *,
        executor_id: str,
        executor_version: str,
        run_root: Path,
        timeout_seconds: float = 300.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not command or any(
            not isinstance(part, str) or not part for part in command
        ):
            raise CustomerExecutionError("执行器命令不能为空")
        if timeout_seconds <= 0:
            raise CustomerExecutionError("执行器超时必须大于零")
        self._command = tuple(command)
        self._executor_id = clean_text(executor_id, "executor_id", 128)
        self._executor_version = clean_text(
            executor_version, "executor_version", 128
        )
        lexical_run_root = Path(os.path.abspath(os.fspath(run_root)))
        if has_link_or_reparse_component(lexical_run_root):
            raise CustomerExecutionError("执行器工作根目录包含链接或重解析点")
        try:
            lexical_run_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CustomerExecutionError("无法创建执行器工作根目录") from error
        if has_link_or_reparse_component(lexical_run_root):
            raise CustomerExecutionError("执行器工作根目录包含链接或重解析点")
        self._run_root = lexical_run_root.resolve(strict=True)
        self._timeout_seconds = timeout_seconds
        self._environment = dict(environment) if environment is not None else None

    @property
    def executor_id(self) -> str:
        return self._executor_id

    @property
    def executor_version(self) -> str:
        return self._executor_version

    def execute(
        self, request: CustomerExecutionRequest
    ) -> CustomerExecutionResult:
        if not isinstance(request, CustomerExecutionRequest):
            raise CustomerExecutionError("request 类型无效")
        payload = canonical_json_bytes(_request_payload(request))
        prefix = "%s-%s-" % (request.case_id[:32], request.arm)
        with tempfile.TemporaryDirectory(
            prefix=prefix, dir=str(self._run_root)
        ) as workspace:
            environment = (
                os.environ.copy()
                if self._environment is None
                else dict(self._environment)
            )
            environment["SMART_ASSISTANT_CUSTOMER_RUN_WORKSPACE"] = workspace
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
                raise CustomerExecutionError("客户执行适配器调用失败") from error
        if completed.returncode != 0:
            raise CustomerExecutionError(
                "客户执行适配器返回非零退出码: %d" % completed.returncode
            )
        if len(completed.stdout) > _MAX_RESPONSE_BYTES:
            raise CustomerExecutionError("客户执行适配器响应超过大小上限")
        response = strict_json_loads(completed.stdout, "执行适配器响应")
        return _parse_response(response, request)


def _request_payload(request: CustomerExecutionRequest) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": request.run_id,
        "case_id": request.case_id,
        "arm": request.arm,
        "tenant_id": request.tenant_id,
        "model": {
            "id": request.model_id,
            "parameters": request.model_parameters,
            "endpoint_sha256": request.endpoint_sha256,
            "prompt_sha256": request.prompt_sha256,
            "tools_sha256": request.tools_sha256,
        },
        "input": request.case_input,
        "skill": request.skill,
    }


def _parse_response(
    response: Any, request: CustomerExecutionRequest
) -> CustomerExecutionResult:
    expected = {
        "schema_version",
        "run_id",
        "case_id",
        "arm",
        "model_id",
        "execution_snapshot_sha256",
        "request_sha256",
        "executor_artifact_sha256",
        "attestation_signature",
        "latency_ms",
        "output",
        "usage",
    }
    if not isinstance(response, dict) or set(response) != expected:
        raise CustomerExecutionError("执行适配器响应字段不符合协议")
    bindings = (
        ("schema_version", response["schema_version"], 1),
        ("run_id", response["run_id"], request.run_id),
        ("case_id", response["case_id"], request.case_id),
        ("arm", response["arm"], request.arm),
        ("model_id", response["model_id"], request.model_id),
    )
    for field_name, actual, expected_value in bindings:
        if actual != expected_value:
            raise CustomerExecutionError(
                "执行适配器响应 %s 与请求不一致" % field_name
            )
    expected_snapshot = execution_snapshot_sha256(request)
    if response["execution_snapshot_sha256"] != expected_snapshot:
        raise CustomerExecutionError("执行适配器实际环境快照与客户包不一致")
    expected_request_sha256 = execution_request_sha256(request)
    if response["request_sha256"] != expected_request_sha256:
        raise CustomerExecutionError("执行适配器请求回执与实际请求不一致")
    executor_artifact_sha256 = clean_sha256(
        response["executor_artifact_sha256"], "executor_artifact_sha256"
    )
    attestation_signature = clean_ed25519_signature(
        response["attestation_signature"], "attestation_signature"
    )
    latency_ms = response["latency_ms"]
    if (
        not isinstance(latency_ms, (int, float))
        or isinstance(latency_ms, bool)
        or not math.isfinite(float(latency_ms))
        or latency_ms < 0
    ):
        raise CustomerExecutionError("执行适配器延迟字段无效")
    usage = response["usage"]
    if not isinstance(usage, dict) or set(usage) != {
        "input_tokens", "output_tokens"
    }:
        raise CustomerExecutionError("执行适配器 usage 字段无效")
    for field_name in ("input_tokens", "output_tokens"):
        if (
            not isinstance(usage[field_name], int)
            or isinstance(usage[field_name], bool)
            or usage[field_name] < 0
        ):
            raise CustomerExecutionError("执行适配器令牌数量无效")
    canonical_json_bytes(response["output"])
    return CustomerExecutionResult(
        output=response["output"],
        latency_ms=float(latency_ms),
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        execution_snapshot_sha256=expected_snapshot,
        request_sha256=expected_request_sha256,
        executor_artifact_sha256=executor_artifact_sha256,
        attestation_signature=attestation_signature,
    )


def execution_snapshot_sha256(request: CustomerExecutionRequest) -> str:
    """绑定两臂必须相同的模型、端点、参数、基础提示词和工具。"""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "tenant_id": request.tenant_id,
                "model_id": request.model_id,
                "model_parameters": request.model_parameters,
                "endpoint_sha256": request.endpoint_sha256,
                "prompt_sha256": request.prompt_sha256,
                "tools_sha256": request.tools_sha256,
            }
        )
    ).hexdigest()


def execution_request_sha256(request: CustomerExecutionRequest) -> str:
    """绑定执行器实际接收的场景输入、臂和技能注入。"""

    return sha256_json(_request_payload(request))
