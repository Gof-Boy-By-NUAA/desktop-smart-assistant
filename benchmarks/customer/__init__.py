"""客户任务上的受控技能配对验收框架。"""

from .contracts import (
    CustomerCase,
    CustomerExecutionRequest,
    CustomerExecutionResult,
    CustomerJudgment,
    CustomerJudgmentRequest,
    CustomerPackage,
    CustomerThresholds,
)
from .executor import CustomerCaseExecutor, SubprocessCustomerCaseExecutor
from .judge import (
    CustomerCaseJudge,
    DeterministicCustomerCaseJudge,
    SubprocessCustomerCaseJudge,
)
from .package import load_customer_package
from .runner import ControlledCustomerAcceptanceRunner
from .verify import verify_customer_report

__all__ = [
    "ControlledCustomerAcceptanceRunner",
    "CustomerCase",
    "CustomerCaseExecutor",
    "CustomerCaseJudge",
    "CustomerExecutionRequest",
    "CustomerExecutionResult",
    "CustomerJudgment",
    "CustomerJudgmentRequest",
    "CustomerPackage",
    "CustomerThresholds",
    "DeterministicCustomerCaseJudge",
    "SubprocessCustomerCaseExecutor",
    "SubprocessCustomerCaseJudge",
    "load_customer_package",
    "verify_customer_report",
]
