"""Hammer neighbor/aggressor VAs the attacker still holds. No PFN lookup."""

from __future__ import annotations

import ctypes
from pathlib import Path

from framework.targets import OnlineTargets


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIB = ROOT / "blacksmith" / "build" / "libblacksmith.so"


class BlacksmithReplay:
    def __init__(self, lib_path: Path = DEFAULT_LIB, num_ranks: int = 1, mem_mb: int = 4) -> None:
        if not lib_path.is_file():
            raise FileNotFoundError(lib_path)
        self.lib = ctypes.CDLL(str(lib_path))
        self.lib.bs_init.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.lib.bs_init.restype = ctypes.c_int
        self.lib.bs_cleanup.argtypes = []
        self.lib.bs_cleanup.restype = None
        self.lib.bs_replay_access_vas.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.lib.bs_replay_access_vas.restype = ctypes.c_int
        if self.lib.bs_init(int(num_ranks), int(mem_mb), 0) != 0:
            raise RuntimeError("bs_init failed")

    def replay_access_vas(self, json_path: Path, addrs: list[int], num_reps: int = 500) -> int:
        arr = (ctypes.c_uint64 * len(addrs))(*[int(a) for a in addrs])
        return int(
            self.lib.bs_replay_access_vas(
                str(json_path).encode(),
                arr,
                len(addrs),
                int(num_reps),
            )
        )

    def close(self) -> None:
        self.lib.bs_cleanup()


def replay_held_neighbors(
    held,
    targets: OnlineTargets,
    *,
    num_reps: int = 500,
    lib_path: Path = DEFAULT_LIB,
    require: bool = False,
) -> dict[str, object]:
    groups = [
        g
        for g in (getattr(held, "neighbor_groups", []) if held is not None else [])
        if len(g.get("access_vas") or []) >= 2
    ]
    if not groups:
        msg = "no held neighbor VAs; occupy must keep aggressor rows"
        if require:
            raise SystemExit(msg)
        print(f"warning: {msg}")
        return {"status": "no_neighbors", "pages": 0, "bitflips": 0}
    if not lib_path.is_file():
        msg = f"missing {lib_path}; run: make hammer"
        if require:
            raise SystemExit(msg)
        print(f"warning: {msg}")
        return {"status": "lib_missing", "pages": 0, "bitflips": 0}
    if not targets.blacksmith_json.is_file():
        msg = f"missing Blacksmith JSON {targets.blacksmith_json}"
        if require:
            raise SystemExit(msg)
        print(f"warning: {msg}")
        return {"status": "json_missing", "pages": 0, "bitflips": 0}
    hammer = BlacksmithReplay(lib_path)
    total = 0
    try:
        for group in groups:
            vas = [int(v) for v in group["access_vas"]]
            total += hammer.replay_access_vas(targets.blacksmith_json, vas, num_reps=num_reps)
    finally:
        hammer.close()
    return {
        "status": "ok" if total >= 0 else "error",
        "pages": len(groups),
        "bitflips": total,
        "reps": num_reps,
    }
