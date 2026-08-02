"""受控配对评测套件运行器。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from .contracts import (
    EvaluationRunner,
    EvaluationRunResult,
    PairedSampleResult,
    SkillTamperError,
    SkillValidationError,
    SkillVersion,
)


_MAX_SUITE_BYTES = 2 * 1024 * 1024
_MAX_CASE_COUNT = 1000


class PairedCaseExecutor(ABC):
    """由应用可信配置注入的基线与候选执行器。"""

    @property
    @abstractmethod
    def executor_id(self) -> str:
        """返回稳定的执行器标识。"""

        raise NotImplementedError

    @property
    @abstractmethod
    def executor_version(self) -> str:
        """返回可审计的执行器版本。"""

        raise NotImplementedError

    @abstractmethod
    def execute_baseline(self, *, model_id: str, case_input: Any) -> Any:
        """在不加载候选技能时执行一个样本。"""

        raise NotImplementedError

    @abstractmethod
    def execute_candidate(
        self,
        *,
        model_id: str,
        candidate: SkillVersion,
        case_input: Any,
    ) -> Any:
        """在加载候选技能时执行同一个样本。"""

        raise NotImplementedError


class ControlledPairedSuiteRunner(EvaluationRunner):
    """从固定目录读取输入与期望，并实际执行同模型配对评测。"""

    _RUNNER_VERSION = "1.1.0"

    def __init__(
        self,
        suite_root: Path,
        executor: Optional[PairedCaseExecutor],
        clock_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        if executor is None:
            raise SkillValidationError("未配置可信配对执行器")
        if not isinstance(executor, PairedCaseExecutor):
            raise SkillValidationError("executor 必须实现 PairedCaseExecutor")
        self._executor = executor
        if clock_ns is not None and not callable(clock_ns):
            raise SkillValidationError("clock_ns ?????")
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._suite_root = self._validate_suite_root(Path(suite_root))
        self._runner_id = "controlled-paired-suite:%s" % self._clean_identity(
            executor.executor_id, "executor_id"
        )
        self._runner_version = "%s+%s" % (
            self._RUNNER_VERSION,
            self._clean_identity(executor.executor_version, "executor_version"),
        )

    @property
    def runner_id(self) -> str:
        """返回包含可信执行器标识的运行器名称。"""

        return self._runner_id

    @property
    def runner_version(self) -> str:
        """返回运行器与可信执行器的组合版本。"""

        return self._runner_version

    def run(
        self,
        *,
        suite_path: str,
        suite_sha256: str,
        model_id: str,
        candidate: SkillVersion,
    ) -> EvaluationRunResult:
        """执行每个样本的基线和候选路径，并由运行器生成成绩。"""

        path = self._resolve_suite_path(suite_path)
        payload = self._read_suite(path)
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if actual_sha256 != suite_sha256:
            raise SkillTamperError("评测套件 SHA-256 与运行请求不一致")
        if not isinstance(model_id, str) or not model_id.strip():
            raise SkillValidationError("model_id 不能为空")
        if not isinstance(candidate, SkillVersion):
            raise SkillValidationError("candidate 必须是 SkillVersion")

        cases = self._parse_cases(payload)
        samples = tuple(
            self._run_case(
                sample_id="case-%d" % index,
                model_id=model_id.strip(),
                candidate=candidate,
                case_input=case_input,
                expected=expected,
            )
            for index, (case_input, expected) in enumerate(cases, start=1)
        )
        if hashlib.sha256(self._read_suite(path)).hexdigest() != actual_sha256:
            raise SkillTamperError("评测套件在执行期间发生变化")
        return EvaluationRunResult(
            suite_sha256=actual_sha256,
            baseline_model_id=model_id.strip(),
            candidate_model_id=model_id.strip(),
            samples=samples,
        )

    def _run_case(
        self,
        *,
        sample_id: str,
        model_id: str,
        candidate: SkillVersion,
        case_input: Any,
        expected: Any,
    ) -> PairedSampleResult:
        """对一个样本分别执行基线与候选，并独立计时和判定。"""

        baseline_started = self._clock_ns()
        baseline_output = self._executor.execute_baseline(
            model_id=model_id,
            case_input=case_input,
        )
        baseline_finished = self._clock_ns()
        if baseline_finished < baseline_started:
            raise SkillValidationError("??????????")
        baseline_latency_ms = (baseline_finished - baseline_started) / 1_000_000

        candidate_started = self._clock_ns()
        candidate_output = self._executor.execute_candidate(
            model_id=model_id,
            candidate=candidate,
            case_input=case_input,
        )
        candidate_finished = self._clock_ns()
        if candidate_finished < candidate_started:
            raise SkillValidationError("??????????")
        candidate_latency_ms = (candidate_finished - candidate_started) / 1_000_000

        expected_json = self._canonical_json(expected, "expected")
        baseline_json = self._canonical_json(baseline_output, "baseline_output")
        candidate_json = self._canonical_json(candidate_output, "candidate_output")
        return PairedSampleResult(
            sample_id=sample_id,
            baseline_success=baseline_json == expected_json,
            candidate_success=candidate_json == expected_json,
            baseline_latency_ms=baseline_latency_ms,
            candidate_latency_ms=candidate_latency_ms,
        )

    def _parse_cases(self, payload: bytes) -> Tuple[Tuple[Any, Any], ...]:
        """只接受由 cases、input 和 expected 组成的严格套件结构。"""

        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=self._reject_duplicate_keys,
                parse_constant=self._reject_non_standard_number,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillValidationError("评测套件必须是 UTF-8 JSON") from exc
        if not isinstance(document, dict) or set(document) != {"cases"}:
            raise SkillValidationError("评测套件顶层只允许 cases 字段")
        raw_cases = document["cases"]
        if not isinstance(raw_cases, list) or not raw_cases:
            raise SkillValidationError("cases 必须是非空数组")
        if len(raw_cases) > _MAX_CASE_COUNT:
            raise SkillValidationError("评测套件样本数超过上限")

        cases = []
        for index, raw_case in enumerate(raw_cases, start=1):
            if not isinstance(raw_case, dict):
                raise SkillValidationError("第 %d 个 case 必须是对象" % index)
            if set(raw_case) != {"input", "expected"}:
                raise SkillValidationError(
                    "第 %d 个 case 只允许 input 和 expected 字段" % index
                )
            cases.append((raw_case["input"], raw_case["expected"]))
        return tuple(cases)

    def _resolve_suite_path(self, suite_path: str) -> Path:
        """拒绝固定根目录之外及任意符号链接路径。"""

        if not isinstance(suite_path, str) or not suite_path.strip():
            raise SkillValidationError("suite_path 不能为空")
        raw_path = Path(suite_path.strip())
        if not raw_path.is_absolute():
            raw_path = self._suite_root / raw_path
        lexical_path = Path(os.path.abspath(str(raw_path)))
        try:
            relative = lexical_path.relative_to(self._suite_root)
        except ValueError as exc:
            raise SkillValidationError("评测套件必须位于固定套件根目录内") from exc
        self._reject_symlink_components(self._suite_root, relative.parts)
        try:
            resolved = lexical_path.resolve(strict=True)
        except OSError as exc:
            raise SkillValidationError("评测套件必须是已存在的普通文件") from exc
        try:
            resolved.relative_to(self._suite_root)
        except ValueError as exc:
            raise SkillValidationError("评测套件解析后越出固定根目录") from exc
        if not resolved.is_file():
            raise SkillValidationError("评测套件必须是已存在的普通文件")
        return resolved

    @classmethod
    def _validate_suite_root(cls, suite_root: Path) -> Path:
        """固定套件根目录本身也不能经过符号链接。"""

        lexical_root = Path(os.path.abspath(str(suite_root)))
        cls._reject_symlink_components(Path(lexical_root.anchor), lexical_root.parts[1:])
        try:
            resolved = lexical_root.resolve(strict=True)
        except OSError as exc:
            raise SkillValidationError("套件根目录必须是已存在的目录") from exc
        if not resolved.is_dir():
            raise SkillValidationError("套件根目录必须是已存在的目录")
        return resolved

    @staticmethod
    def _reject_symlink_components(base: Path, parts: Iterable[str]) -> None:
        """逐段检查路径，避免父目录符号链接绕过最终文件检查。"""

        current = base
        for part in parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                continue
            attributes = getattr(metadata, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
                raise SkillValidationError(
                    "评测套件路径不能包含符号链接或重解析点"
                )

    @staticmethod
    def _read_suite(path: Path) -> bytes:
        """在解析前限制套件大小，防止无界内存占用。"""

        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SkillValidationError("无法读取评测套件") from exc
        if size > _MAX_SUITE_BYTES:
            raise SkillValidationError("评测套件文件超过大小上限")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise SkillValidationError("无法读取评测套件") from exc

    @staticmethod
    def _canonical_json(value: Any, field_name: str) -> bytes:
        """使用严格 JSON 比较输出，避免 Python 宽松相等规则误判。"""

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SkillValidationError("%s 必须是标准 JSON 值" % field_name) from exc

    @staticmethod
    def _reject_duplicate_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
        """拒绝重复键，防止解析器覆盖字段造成审计歧义。"""

        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SkillValidationError("评测套件包含重复 JSON 字段: %s" % key)
            result[key] = value
        return result

    @staticmethod
    def _reject_non_standard_number(value: str) -> None:
        """拒绝 JSON 标准之外的 NaN 和无穷数。"""

        raise SkillValidationError("评测套件包含非标准数值: %s" % value)

    @staticmethod
    def _clean_identity(value: str, field_name: str) -> str:
        """约束写入审计记录的执行器身份字段。"""

        if not isinstance(value, str) or not value.strip():
            raise SkillValidationError("%s 不能为空" % field_name)
        normalized = value.strip()
        if len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
            raise SkillValidationError("%s 格式无效" % field_name)
        return normalized
