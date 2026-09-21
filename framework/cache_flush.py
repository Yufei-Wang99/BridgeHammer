"""Flush CPU cache lines covering a host buffer before integrity hashing."""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any, Optional


CACHELINE = 64
_FR_LIB: Any | None | bool = None


def _load_fr() -> Any | None:
    global _FR_LIB
    if _FR_LIB is False:
        return None
    if _FR_LIB is not None:
        return _FR_LIB
    lib_path = Path(__file__).resolve().parent / "libfr.so"
    if not lib_path.exists():
        _FR_LIB = False
        return None
    lib = ctypes.CDLL(str(lib_path))
    lib.fr_flush.argtypes = [ctypes.c_void_p]
    lib.fr_flush.restype = None
    _FR_LIB = lib
    return lib


def flush_buffer_cachelines(buffer: Any) -> dict[str, int]:
    """
    clflush every cache line covering `buffer`, then return flush stats.

    Must be called in the process that owns the virtual addresses.
    """
    lib = _load_fr()
    if lib is None:
        raise RuntimeError("framework/libfr.so unavailable for cache-line flush")

    ptr = int(buffer.__array_interface__["data"][0])
    nbytes = int(buffer.nbytes)
    if nbytes <= 0:
        return {"nbytes": 0, "lines": 0, "base": ptr}

    # Align down to cache line so the first partial line is covered.
    start = ptr & ~(CACHELINE - 1)
    end = ptr + nbytes
    lines = 0
    addr = start
    while addr < end:
        lib.fr_flush(ctypes.c_void_p(addr))
        addr += CACHELINE
        lines += 1
    # fr_flush already includes mfence per call in this codebase's libfr.
    return {"nbytes": nbytes, "lines": lines, "base": ptr}


def flush_buffers_cachelines(buffers: list[Any]) -> dict[str, int]:
    total_nbytes = 0
    total_lines = 0
    for buf in buffers:
        stats = flush_buffer_cachelines(buf)
        total_nbytes += int(stats["nbytes"])
        total_lines += int(stats["lines"])
    return {"nbytes": total_nbytes, "lines": total_lines}
