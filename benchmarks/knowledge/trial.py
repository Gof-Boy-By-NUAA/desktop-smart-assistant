"""在独立进程中执行一次 Knowledge 质量或建库试验。"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Optional


_WINDOWS_ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
_POWER_PLAN_GUID_PATTERN = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class _MemoryStatusEx(ctypes.Structure):
    """映射 Windows `MEMORYSTATUSEX`。"""

    _fields_ = (
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


def configure_measurement_process() -> Dict[str, object]:
    """固定可控的调度参数并采集计时前系统快照。"""

    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle_type = ctypes.c_void_p
        mask_type = ctypes.c_size_t
        kernel32.GetCurrentProcess.restype = handle_type
        kernel32.GetProcessAffinityMask.argtypes = (
            handle_type,
            ctypes.POINTER(mask_type),
            ctypes.POINTER(mask_type),
        )
        kernel32.GetProcessAffinityMask.restype = ctypes.c_int
        kernel32.SetProcessAffinityMask.argtypes = (handle_type, mask_type)
        kernel32.SetProcessAffinityMask.restype = ctypes.c_int
        kernel32.SetPriorityClass.argtypes = (handle_type, ctypes.c_ulong)
        kernel32.SetPriorityClass.restype = ctypes.c_int
        kernel32.GetPriorityClass.argtypes = (handle_type,)
        kernel32.GetPriorityClass.restype = ctypes.c_ulong
        process = kernel32.GetCurrentProcess()
        process_mask = mask_type()
        system_mask = mask_type()
        if not kernel32.GetProcessAffinityMask(
            process, ctypes.byref(process_mask), ctypes.byref(system_mask)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        allowed_mask = int(process_mask.value)
        if allowed_mask <= 0:
            raise RuntimeError("性能试验没有可用 CPU 亲和性")
        selected_mask = allowed_mask & -allowed_mask
        if not kernel32.SetProcessAffinityMask(process, mask_type(selected_mask)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.SetPriorityClass(
            process, _WINDOWS_ABOVE_NORMAL_PRIORITY_CLASS
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        verified_process_mask = mask_type()
        verified_system_mask = mask_type()
        if not kernel32.GetProcessAffinityMask(
            process,
            ctypes.byref(verified_process_mask),
            ctypes.byref(verified_system_mask),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if int(verified_process_mask.value) != selected_mask:
            raise RuntimeError("性能试验 CPU 亲和性回读不一致")
        if kernel32.GetPriorityClass(process) != _WINDOWS_ABOVE_NORMAL_PRIORITY_CLASS:
            raise RuntimeError("性能试验进程优先级回读不一致")
        affinity = [selected_mask.bit_length() - 1]
        priority = "above_normal"
    else:
        affinity = None
        if hasattr(os, "sched_setaffinity"):
            available = sorted(os.sched_getaffinity(0))
            if not available:
                raise RuntimeError("性能试验没有可用 CPU")
            os.sched_setaffinity(0, {available[0]})
            affinity = [available[0]]
        priority = "platform_default"

    return {
        "fresh_process": True,
        "process_instance_id": os.environ.get(
            "SMART_ASSISTANT_KNOWLEDGE_TRIAL_ID", uuid.uuid4().hex
        ),
        "pid": os.getpid(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "cpu_affinity": affinity,
        "priority": priority,
        "power_plan": _power_plan(),
        "background_load": _background_load_snapshot(),
    }


def _power_plan() -> Optional[str]:
    """读取 Windows 当前电源计划；其他平台明确返回空值。"""

    if os.name != "nt":
        return None
    result = subprocess.run(
        ["powercfg", "/getactivescheme"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("无法读取当前 Windows 电源计划")
    match = _POWER_PLAN_GUID_PATTERN.search(result.stdout)
    if match is None:
        raise RuntimeError("无法解析当前 Windows 电源计划 GUID")
    return match.group(0).decode("ascii").lower()


def _background_load_snapshot() -> Dict[str, object]:
    """记录计时前 CPU 负载和可用内存，不把缺失值伪装成零。"""

    if os.name == "nt":
        return {
            "cpu_busy_ratio": _windows_cpu_busy_ratio(),
            "available_memory_bytes": _windows_available_memory(),
        }
    load_average = None
    if hasattr(os, "getloadavg"):
        load_average = list(os.getloadavg())
    return {
        "cpu_busy_ratio": None,
        "available_memory_bytes": None,
        "load_average": load_average,
    }


def _windows_cpu_busy_ratio() -> float:
    """用两次 `GetSystemTimes` 采样计算全机短时 CPU 忙碌比例。"""

    def sample() -> tuple[int, int, int]:
        idle = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise RuntimeError("无法读取 Windows CPU 时间")
        return idle.value, kernel.value, user.value

    before = sample()
    time.sleep(0.05)
    after = sample()
    idle_delta = after[0] - before[0]
    total_delta = (after[1] - before[1]) + (after[2] - before[2])
    if total_delta <= 0:
        raise RuntimeError("Windows CPU 负载采样无效")
    return max(0.0, min(1.0, 1.0 - idle_delta / float(total_delta)))


def _windows_available_memory() -> int:
    """读取 Windows 当前可用物理内存。"""

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise RuntimeError("无法读取 Windows 内存状态")
    return int(status.ullAvailPhys)


def run_trial(
    dataset_path: Path,
    engine_name: str,
    mode: str,
    max_queries: Optional[int] = None,
) -> Dict[str, object]:
    """在已经配置好的独立进程中执行一次预注册试验。"""

    environment = configure_measurement_process()
    # 产品日志统一转到标准错误，标准输出只保留最终 JSON 协议。
    with contextlib.redirect_stdout(sys.stderr):
        from .evaluate import (
            GovernedKnowledgeEngine,
            LegacyKnowledgeEngine,
            run_knowledge_engine,
            run_knowledge_index_trial,
        )

        factory = (
            LegacyKnowledgeEngine
            if engine_name == "legacy"
            else GovernedKnowledgeEngine
        )
        if mode == "full":
            report = run_knowledge_engine(
                Path(dataset_path), factory(), max_queries=max_queries
            )
        elif mode == "index":
            report = run_knowledge_index_trial(Path(dataset_path), factory())
        else:
            raise ValueError("mode 必须是 full 或 index")
    report["measurement_environment"] = environment
    return report


def main() -> int:
    """命令行只向标准输出写一份严格 JSON 结果。"""

    parser = argparse.ArgumentParser(description="运行独立 Knowledge 性能试验")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--engine", choices=("legacy", "governed"), required=True)
    parser.add_argument("--mode", choices=("full", "index"), required=True)
    parser.add_argument("--max-queries", type=int)
    args = parser.parse_args()
    report = run_trial(
        args.dataset,
        args.engine,
        args.mode,
        max_queries=args.max_queries,
    )
    sys.stdout.write(
        json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
