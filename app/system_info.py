"""Best-effort CPU and memory readings without adding a dependency.

Windows uses ``GetSystemTimes`` and ``GlobalMemoryStatusEx`` through ctypes;
Linux reads ``/proc/stat`` and ``/proc/meminfo``. Anything else returns
``None`` so the UI can show "unknown" instead of an invented number.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Optional

_lock = threading.Lock()
_previous: Optional[tuple[int, int, int]] = None
_process_previous: Optional[tuple[int, float]] = None
_psutil_process = None
_psutil_previous: Optional[tuple[float, float]] = None


def _filetime_value(low: int, high: int) -> int:
    return (high << 32) | low


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


def _windows_cpu() -> Optional[float]:
    global _previous
    idle = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return None
    idle_v = _filetime_value(idle.dwLowDateTime, idle.dwHighDateTime)
    kernel_v = _filetime_value(kernel.dwLowDateTime, kernel.dwHighDateTime)
    user_v = _filetime_value(user.dwLowDateTime, user.dwHighDateTime)

    if _previous is None:
        _previous = (idle_v, kernel_v, user_v)
        return None

    prev_idle, prev_kernel, prev_user = _previous
    _previous = (idle_v, kernel_v, user_v)
    idle_delta = idle_v - prev_idle
    total_delta = (kernel_v - prev_kernel) + (user_v - prev_user)
    if total_delta <= 0:
        return None
    busy = max(0.0, min(1.0, 1.0 - idle_delta / total_delta))
    return round(busy * 100.0, 1)


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _windows_memory() -> Optional[float]:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
        ctypes.byref(status)
    ):
        return None
    return float(status.dwMemoryLoad)


def _linux_cpu() -> Optional[float]:
    global _previous
    try:
        with open("/proc/stat", "r", encoding="ascii") as handle:
            fields = handle.readline().split()
    except OSError:
        return None
    if not fields or fields[0] != "cpu":
        return None
    values = [int(value) for value in fields[1:8]]
    idle_v = values[3] + (values[4] if len(values) > 4 else 0)
    total_v = sum(values)
    if _previous is None:
        _previous = (idle_v, total_v, 0)
        return None
    prev_idle, prev_total, _unused = _previous
    _previous = (idle_v, total_v, 0)
    total_delta = total_v - prev_total
    if total_delta <= 0:
        return None
    busy = max(0.0, min(1.0, 1.0 - (idle_v - prev_idle) / total_delta))
    return round(busy * 100.0, 1)


def _linux_memory() -> Optional[float]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    values[name] = int(rest.strip().split()[0])
    except (OSError, ValueError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return round(100.0 * (1.0 - available / total), 1)


def system_usage() -> tuple[Optional[float], Optional[float]]:
    """Return ``(cpu_percent, memory_percent)`` or ``(None, None)``."""
    with _lock:
        try:
            if sys.platform.startswith("win"):
                return _windows_cpu(), _windows_memory()
            if sys.platform.startswith("linux"):
                return _linux_cpu(), _linux_memory()
        except Exception:  # noqa: BLE001 - metrics must never break a snapshot
            return None, None
        return None, None


def total_memory_gb() -> Optional[float]:
    """Best-effort installed RAM in GB, used for the auto performance profile."""
    try:
        if sys.platform.startswith("win"):
            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(status)
            ):
                return round(status.ullTotalPhys / 1e9, 1)
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", "r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001 - auto detection must never be fatal
        return None
    return None


def _windows_process() -> tuple[Optional[float], Optional[float]]:
    global _process_previous
    handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
    creation = _FileTime()
    exit_time = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    if not ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None, None
    ticks = _filetime_value(kernel.dwLowDateTime, kernel.dwHighDateTime) + _filetime_value(
        user.dwLowDateTime, user.dwHighDateTime
    )
    now = time.monotonic()
    cpu: Optional[float] = None
    if _process_previous is not None:
        prev_ticks, prev_now = _process_previous
        wall = now - prev_now
        if wall > 0:
            cores = max(1, os.cpu_count() or 1)
            cpu = round(
                100.0 * (ticks - prev_ticks) / 10_000_000.0 / wall / cores, 1
            )
    _process_previous = (ticks, now)

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
    rss = None
    try:
        if ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            handle, ctypes.byref(counters), counters.cb
        ):
            rss = round(counters.WorkingSetSize / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001 - RSS is optional
        rss = None
    return cpu, rss


def _linux_process() -> tuple[Optional[float], Optional[float]]:
    global _process_previous
    cpu: Optional[float] = None
    try:
        values = open("/proc/self/stat", "r", encoding="ascii").read().split()
        ticks = int(values[13]) + int(values[14])
        now = time.monotonic()
        if _process_previous is not None:
            prev_ticks, prev_now = _process_previous
            wall = now - prev_now
            if wall > 0:
                cores = max(1, os.cpu_count() or 1)
                cpu = round(
                    100.0 * (ticks - prev_ticks) / os.sysconf("SC_CLK_TCK") / wall / cores,
                    1,
                )
        _process_previous = (ticks, now)
    except (OSError, ValueError, IndexError, AttributeError):
        cpu = None
    rss = None
    try:
        pages = int(open("/proc/self/statm", "r", encoding="ascii").read().split()[1])
        rss = round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError, AttributeError):
        rss = None
    return cpu, rss


def _psutil_process_usage() -> tuple[Optional[float], Optional[float]]:
    """Optional path used when psutil is available (it is declared for tools)."""
    global _psutil_process, _psutil_previous
    try:
        import psutil
    except ImportError:
        return None, None
    try:
        if _psutil_process is None:
            _psutil_process = psutil.Process()
        times = _psutil_process.cpu_times()
        total = float(times.user + times.system)
        now = time.monotonic()
        cpu: Optional[float] = None
        if _psutil_previous is not None:
            previous_total, previous_now = _psutil_previous
            wall = now - previous_now
            if wall > 0:
                cores = max(1, os.cpu_count() or 1)
                cpu = round(100.0 * (total - previous_total) / wall / cores, 1)
        _psutil_previous = (total, now)
        rss = round(_psutil_process.memory_info().rss / (1024 * 1024), 1)
        return cpu, rss
    except Exception:  # noqa: BLE001 - metrics must never break a snapshot
        return None, None


def process_usage() -> tuple[Optional[float], Optional[float]]:
    """Return ``(app_cpu_percent_of_machine, rss_mb)`` for this process."""
    cpu, rss = _psutil_process_usage()
    if cpu is not None and rss is not None:
        return cpu, rss
    try:
        if sys.platform.startswith("win"):
            return _windows_process()
        if sys.platform.startswith("linux"):
            return _linux_process()
    except Exception:  # noqa: BLE001 - metrics must never break a snapshot
        return None, None
    return None, None
