"""Spawn file-cache pressure workers and poll residency with preadv2(RWF_NOWAIT)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
LIST_SHLIBS = HERE / "primitives" / "scripts" / "list-all-shlibs.sh"
SAFE_WORKERS = "baseline,shlibs,randaccess,dynpressure"

EVICTOR_COMMS = {
    "evict-1-shlibs",
    "evict-2-randaccess",
    "evict-3-baseline",
    "evict-4-dynpressure",
}


def parse_kv_lines(lines: list[str]) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key] = value
    return info


def fstype_for(path: Path) -> str:
    path = path.resolve()
    best_mnt = ""
    best = ""
    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return ""
    for line in mounts.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt, fstype = parts[1], parts[2]
        prefix = mnt.rstrip("/") + "/"
        text = str(path)
        if text == mnt or text.startswith(prefix) or (mnt == "/" and text.startswith("/")):
            if len(mnt) >= len(best_mnt):
                best_mnt = mnt
                best = fstype
    return best


def memavailable_kb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    return -1


def swap_enabled() -> bool:
    swaps = Path("/proc/swaps")
    if not swaps.exists():
        return False
    return len(swaps.read_text().splitlines()) > 1


def evictor_pids() -> list[int]:
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm in EVICTOR_COMMS:
            pids.append(int(entry.name))
    return pids


def kill_evictors() -> None:
    for pid in evictor_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.2)
    for pid in evictor_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def wait_evictors_dead(timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not evictor_pids():
            return True
        time.sleep(0.1)
    kill_evictors()
    return not evictor_pids()


def recover_memory(baseline_kb: int, timeout_s: float = 30.0) -> None:
    if baseline_kb <= 0:
        time.sleep(1.0)
        return
    deadline = time.time() + timeout_s
    target = int(baseline_kb * 0.8)
    while time.time() < deadline:
        now = memavailable_kb()
        if now >= target:
            return
        time.sleep(0.5)


def run_monitor_range(monitor: Path, test_file: Path, start: int, count: int) -> dict[int, int]:
    out = subprocess.check_output(
        [str(monitor), str(test_file), "--range", str(start), str(count)],
        text=True,
        cwd=str(HERE),
    )
    result: dict[int, int] = {}
    for line in out.splitlines():
        if not line.startswith("page="):
            continue
        parts = dict(item.split("=", 1) for item in line.split(","))
        result[int(parts["page"])] = int(parts["resident"])
    return result


def run_monitor_pages(monitor: Path, test_file: Path, pages: list[int]) -> dict[int, int]:
    if not pages:
        return {}
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".pages",
        prefix="evict_mon_",
        delete=False,
        encoding="utf-8",
    )
    for page in pages:
        tmp.write(f"{int(page)}\n")
    tmp.close()
    try:
        out = subprocess.check_output(
            [str(monitor), str(test_file), "--list", tmp.name],
            text=True,
            cwd=str(HERE),
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    result: dict[int, int] = {}
    for line in out.splitlines():
        if not line.startswith("page="):
            continue
        parts = dict(item.split("=", 1) for item in line.split(","))
        result[int(parts["page"])] = int(parts["resident"])
    return result


def fadvise_dontneed(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def evict_all(
    test_file: Path,
    timeout_s: int,
    workers: str,
    list_shlibs: Path | None,
    baseline_count: int,
    baseline_gib: float | None,
    until_pages: list[int] | None = None,
) -> dict[str, str]:
    cmd = [
        str(BUILD / "eviction_controller"),
        str(test_file),
        "0",
        "--timeout-s",
        str(timeout_s),
        "--bin-dir",
        str(BUILD),
        "--page-monitor",
        str(BUILD / "page_monitor"),
        "--workers",
        workers,
        "--baseline-count",
        str(baseline_count),
    ]
    if baseline_gib is not None:
        cmd.extend(["--baseline-gib", f"{baseline_gib:.2f}"])
    if list_shlibs is not None and list_shlibs.exists():
        cmd.extend(["--list-shlibs", str(list_shlibs)])
    list_path = None
    if until_pages:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".pages",
            prefix="evict_until_",
            delete=False,
            encoding="utf-8",
        )
        for page in until_pages:
            tmp.write(f"{int(page)}\n")
        tmp.close()
        list_path = tmp.name
        cmd.extend(["--until-pages", list_path])
    else:
        cmd.append("--until-all")
    n_watch = len(until_pages) if until_pages else 0
    print(f"evict workers={workers} timeout={timeout_s}s targets={n_watch or 'all'}")
    try:
        completed = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(HERE),
        )
    finally:
        if list_path:
            try:
                os.unlink(list_path)
            except OSError:
                pass
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    if completed.stdout:
        print(completed.stdout, end="")
    info = parse_kv_lines(completed.stdout.splitlines())
    if completed.returncode != 0 and not info:
        info["eviction_success"] = "0"
    return info


def list_shlibs_script() -> Path | None:
    return LIST_SHLIBS if LIST_SHLIBS.is_file() else None
