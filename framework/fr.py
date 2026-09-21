"""Flush+Reload and SIGSTOP."""

from __future__ import annotations

import ctypes
import mmap
import os
import time
from pathlib import Path


PAGE_SIZE = os.sysconf("SC_PAGESIZE")
LIBFR = Path(__file__).resolve().parent / "libfr.so"


class FileLineMapping:
    """Map one file-backed cache line so F+R shares the page-cache line."""

    def __init__(self, path: Path, file_offset: int) -> None:
        self.path = path
        self.file_offset = int(file_offset)
        self.page_offset = self.file_offset & ~(PAGE_SIZE - 1)
        self.delta = self.file_offset - self.page_offset
        self.fd = os.open(str(path), os.O_RDONLY)
        self.map = mmap.mmap(
            self.fd,
            PAGE_SIZE,
            flags=mmap.MAP_PRIVATE,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
            offset=self.page_offset,
        )
        self.ptr = ctypes.addressof(ctypes.c_char.from_buffer(self.map)) + self.delta

    def close(self) -> None:
        self.map.close()
        os.close(self.fd)


class Fr:
    def __init__(self, lib_path: Path = LIBFR) -> None:
        if not Path(lib_path).is_file():
            raise SystemExit(f"missing {lib_path}")
        self.lib = ctypes.CDLL(str(lib_path))
        self.lib.fr_flush.argtypes = [ctypes.c_void_p]
        self.lib.fr_flush.restype = None
        self.lib.fr_flush_reload_time.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.fr_flush_reload_time.restype = ctypes.c_uint32
        self.lib.fr_monitor_line_sigstop.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.fr_monitor_line_sigstop.restype = ctypes.c_uint64

    def probe_once(self, ptr: int, pause_iters: int) -> int:
        return int(self.lib.fr_flush_reload_time(ctypes.c_void_p(ptr), pause_iters))

    def monitor_sigstop(
        self,
        ptr: int,
        threshold: int,
        max_samples: int,
        pause_iters: int,
        min_consecutive: int,
        pid: int,
    ) -> dict[str, object]:
        best = ctypes.c_uint32(0)
        elapsed = ctypes.c_uint64(0)
        kill_rc = ctypes.c_int(-1)
        t0 = time.perf_counter_ns()
        idx = self.lib.fr_monitor_line_sigstop(
            ctypes.c_void_p(ptr),
            ctypes.c_uint32(threshold),
            ctypes.c_uint64(max_samples),
            ctypes.c_int(pause_iters),
            ctypes.c_int(min_consecutive),
            ctypes.c_int(pid),
            ctypes.byref(best),
            ctypes.byref(elapsed),
            ctypes.byref(kill_rc),
        )
        elapsed_ns = time.perf_counter_ns() - t0
        confirmed, confirm_ns = wait_stopped(pid, 2.0)
        return {
            "detected": bool(idx),
            "sample_index": int(idx),
            "best_cycles": int(best.value),
            "elapsed_cycles": int(elapsed.value),
            "elapsed_us": elapsed_ns / 1000.0,
            "kill_rc": int(kill_rc.value),
            "sigstop_confirmed": confirmed,
            "sigstop_confirm_us": confirm_ns / 1000.0,
        }


def wait_stopped(pid: int, timeout_s: float) -> tuple[bool, int]:
    status = Path(f"/proc/{pid}/status")
    t0 = time.perf_counter_ns()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("State:") and line.split()[1] in {"T", "t"}:
                    return True, time.perf_counter_ns() - t0
        except FileNotFoundError:
            return False, time.perf_counter_ns() - t0
        time.sleep(0.001)
    return False, time.perf_counter_ns() - t0


def wait_for_quiet_line(
    fr: Fr,
    ptr: int,
    threshold: int,
    pause_iters: int,
    required_misses: int,
    max_probes: int,
) -> bool:
    misses = 0
    for _ in range(max_probes):
        if fr.probe_once(ptr, pause_iters) >= threshold:
            misses += 1
            if misses >= required_misses:
                return True
        else:
            misses = 0
    return False
