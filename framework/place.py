"""Occupy recorded pages, evict the weight file, then plant after evict."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
EVICTION = ROOT / "eviction"


def _ensure_eviction_path() -> Path:
    if not EVICTION.is_dir():
        raise SystemExit(f"missing eviction tree {EVICTION}")
    text = str(EVICTION)
    if text not in sys.path:
        sys.path.insert(0, text)
    return EVICTION


def ensure_eviction_bins() -> Path:
    ev = _ensure_eviction_path()
    need = (
        ev / "build" / "eviction_controller",
        ev / "build" / "page_monitor",
        ev / "build" / "libpcp_adapter.so",
    )
    if all(p.is_file() for p in need):
        return ev
    print(f"building {ev}")
    subprocess.check_call(["make", "-C", str(ev)])
    missing = [p for p in need if not p.is_file()]
    if missing:
        raise SystemExit(f"eviction build incomplete: {missing}")
    return ev


def evict_file(
    weights: Path,
    *,
    timeout_s: int,
    workers: str,
    baseline_count: int,
    baseline_gib: Optional[float],
    list_shlibs: Optional[Path],
    until_pages: Optional[list[int]] = None,
) -> dict[str, str]:
    ensure_eviction_bins()
    _ensure_eviction_path()
    from evict import evict_all

    return evict_all(
        weights,
        timeout_s,
        workers,
        list_shlibs,
        baseline_count,
        baseline_gib,
        until_pages=until_pages,
    )


def occupy_listed(
    place_pages: list[int],
    wanted_pfns: list[int],
    *,
    pcp_cpu: int,
    hunt_pages: int,
    hunt_attempts: int = 16,
    targets=None,
):
    """Hold listed victim PFNs and their neighbor rows (pagemap at hold time)."""
    ensure_eviction_bins()
    _ensure_eviction_path()
    from pcp import occupy_listed as _occupy
    from framework.targets import pattern_geometry

    geo = pattern_geometry(targets) if targets is not None else {}
    return _occupy(
        place_pages,
        wanted_pfns,
        hunt_pages=hunt_pages,
        hunt_attempts=hunt_attempts,
        pcp_cpu=pcp_cpu,
        aggressors=geo.get("aggressors"),
        orig_victim_row=geo.get("orig_victim_row"),
        access_ids=geo.get("access_ids"),
    )


def place_pages(
    weights: Path,
    targets: list[int],
    occupancy,
    *,
    pcp_cpu: int,
    drain_pages: int,
    cs_batch: int = 1,
) -> tuple[list[int], list[int], float, str]:
    """Plant the already-occupied listed pages onto the weight file."""
    ensure_eviction_bins()
    _ensure_eviction_path()
    from pcp import place_occupied

    return place_occupied(
        occupancy,
        weights,
        targets,
        pcp_cpu=pcp_cpu,
        drain_pages=drain_pages,
        cs_batch=cs_batch,
    )


def monitor_range(weights: Path, start: int, count: int) -> dict[int, int]:
    ensure_eviction_bins()
    _ensure_eviction_path()
    from evict import BUILD, run_monitor_range

    return run_monitor_range(BUILD / "page_monitor", weights, start, count)


def monitor_pages(weights: Path, pages: list[int]) -> dict[int, int]:
    ensure_eviction_bins()
    _ensure_eviction_path()
    from evict import BUILD, run_monitor_pages

    return run_monitor_pages(BUILD / "page_monitor", weights, pages)


def cleanup_evictors() -> None:
    _ensure_eviction_path()
    from evict import kill_evictors, wait_evictors_dead

    kill_evictors()
    wait_evictors_dead(5.0)


def recover_after_trial(baseline_kb: int) -> None:
    _ensure_eviction_path()
    from evict import recover_memory

    recover_memory(baseline_kb, timeout_s=45.0)


def list_shlibs_script() -> Optional[Path]:
    _ensure_eviction_path()
    from evict import list_shlibs_script as _script

    return _script()
