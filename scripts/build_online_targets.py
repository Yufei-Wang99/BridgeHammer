#!/usr/bin/env python3
"""Build the online DNN-page → vulnerable-PFN list for ResNet-20 INT8.

DRAM page N is the N-th unique physical page in the offline profile after
sorting by flip count (ROBBIN ranking).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts" / "resnet20_int8"
PAGE_SIZE = 4096


def parse_hex(value: object) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 0)


def parse_attack_report(path: Path) -> tuple[dict, list[dict]]:
    text = path.read_text(encoding="utf-8")
    meta = {
        "asr": 90.17,
        "acc_clean": 90.73,
        "acc_final": 83.0,
        "target_class": 2,
    }
    m = re.search(r"Final ASR:\s*([0-9.]+)", text)
    if m:
        meta["asr"] = float(m.group(1))
    m = re.search(r"Initial Accuracy:\s*([0-9.]+)", text)
    if m:
        meta["acc_clean"] = float(m.group(1))
    m = re.search(r"Final Accuracy:\s*([0-9.]+)", text)
    if m:
        meta["acc_final"] = float(m.group(1))
    mappings = []
    for block in re.finditer(
        r"Mapping #(\d+):\s*"
        r"DNN Page:\s*(\d+)\s*"
        r"DRAM Page:\s*(\d+)\s*"
        r"Total Flips:\s*(\d+)\s*"
        r"Beneficial Flips:\s*(\d+)\s*"
        r"0.1 Flips:\s*(\d+)\s*"
        r"1.0 Flips:\s*(\d+)\s*"
        r"ASR:\s*([0-9.]+)%\s*"
        r"Accuracy:\s*([0-9.]+)%",
        text,
    ):
        mappings.append(
            {
                "index": int(block.group(1)),
                "dnn_page": int(block.group(2)),
                "dram_page": int(block.group(3)),
                "num_flips": int(block.group(4)),
                "beneficial_flips": int(block.group(5)),
                "flips_0to1": int(block.group(6)),
                "flips_1to0": int(block.group(7)),
                "asr": float(block.group(8)),
                "acc": float(block.group(9)),
            }
        )
    if not mappings:
        raise SystemExit(f"{path}: no DNN-DRAM mappings")
    return meta, mappings


def rank_profile_pages(profile_path: Path) -> list[tuple[int, list[dict]]]:
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    by_pfn: dict[int, list[dict]] = defaultdict(list)
    for rec in data:
        if not isinstance(rec, dict) or "phys_page_number" not in rec:
            continue
        pfn = parse_hex(rec["phys_page_number"])
        by_pfn[pfn].append(rec)
    ranked = sorted(by_pfn.items(), key=lambda kv: len(kv[1]), reverse=True)
    return ranked


def parse_pagemap(path: Path) -> dict[int, list[dict]]:
    pages: dict[int, list[dict]] = defaultdict(list)
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PARAM"):
            parts = line.split()
            current = {
                "name": parts[1],
                "dtype": parts[-1] if parts else "",
            }
        elif line.startswith("PAGE") and current is not None:
            parts = line.split()
            page_id = int(parts[1])
            pages[page_id].append(
                {
                    "param": current["name"],
                    "dtype": current.get("dtype", ""),
                    "start_byte": int(parts[2]),
                    "end_byte": int(parts[3]),
                    "num_bytes": int(parts[4]),
                }
            )
    return pages


def load_manifest(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for entry in data.get("weights") or []:
        out[str(entry.get("pytorch_name") or "")] = entry
        out[str(entry.get("tensor_name") or "")] = entry
    return out


def load_weight_flips(path: Path) -> list[dict]:
    flips = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "|" not in line or line.lstrip().startswith("#") or line.lstrip().startswith("`"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8 or parts[0] == "layer_name":
            continue
        try:
            flips.append(
                {
                    "layer": parts[0],
                    "tensor": parts[1],
                    "byte": int(parts[2]),
                    "bit": int(parts[3]),
                    "direction": parts[4],
                    "global_byte": int(parts[5]),
                    "weight_page": int(parts[6]),
                    "page_offset": int(parts[7]),
                }
            )
        except ValueError:
            continue
    return flips


def profile_flips_to_blacksmith(pfn: int, recs: list[dict]) -> list[dict]:
    out = []
    for rec in recs:
        bit = int(rec["bit_position"])
        flip_type = int(rec["flip_type"])
        off = int(rec["page_offset"])
        out.append(
            {
                "pfn": pfn,
                "page_offset": off,
                "bitmask": 1 << bit,
                "bit_position": bit,
                "dram_addr": {
                    "bank": int(rec.get("bank") or 0),
                    "row": None,
                    "col": off,
                },
                "direction": "0->1" if flip_type == 0 else "1->0",
            }
        )
    return out


def dnn_to_weight_pages(
    dnn_page: int,
    page_allocs: dict[int, list[dict]],
    manifest: dict[str, dict],
    max_page: int,
) -> list[int]:
    found: set[int] = set()
    for alloc in page_allocs.get(dnn_page, []):
        if "int8" not in str(alloc.get("dtype") or "").lower() and not str(alloc["param"]).endswith(".weight"):
            continue
        entry = manifest.get(alloc["param"])
        if not entry:
            continue
        base = int(entry["combined_offset"])
        length = int(entry.get("bytes_length") or 0)
        start = min(base + int(alloc["start_byte"]), base + max(length - 1, 0))
        end = min(base + int(alloc["end_byte"]), base + max(length - 1, 0))
        found.add(min(max_page, start // PAGE_SIZE))
        found.add(min(max_page, end // PAGE_SIZE))
    return sorted(p for p in found if 0 <= p <= max_page)


def first_pattern(fuzz: dict) -> tuple[str, list]:
    patterns = fuzz.get("hammering_patterns") or []
    if not patterns:
        return "", []
    patt = patterns[0]
    mappings = patt.get("address_mappings") or []
    aggs = mappings[0].get("aggressor_to_addr") if mappings else []
    return str(patt.get("id") or ""), aggs or []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=ART / "offline" / "dram_profile.json")
    parser.add_argument("--report", type=Path, default=ART / "attack" / "attack_report.txt")
    parser.add_argument("--pagemap", type=Path, default=ART / "offline" / "ResNet20_INT8_pagemap.txt")
    parser.add_argument("--manifest", type=Path, default=ART / "combined_manifest.json")
    parser.add_argument("--fault-map", type=Path, default=ART / "target_bits_locations.md")
    parser.add_argument("--blacksmith-json", type=Path, default=ART / "offline" / "blacksmith_fuzz_summary.json")
    parser.add_argument("--out", type=Path, default=ART / "online_targets.json")
    args = parser.parse_args()

    meta, mappings = parse_attack_report(args.report)
    ranked = rank_profile_pages(args.profile)
    page_allocs = parse_pagemap(args.pagemap)
    manifest = load_manifest(args.manifest)
    weight_flips = load_weight_flips(args.fault_map)
    flips_by_page: dict[int, list[dict]] = defaultdict(list)
    for flip in weight_flips:
        flips_by_page[int(flip["weight_page"])].append(flip)
    place_pages = sorted(flips_by_page)
    weights = ART / "combined_weights.bin"
    max_page = max(0, (weights.stat().st_size - 1) // PAGE_SIZE) if weights.is_file() else 65
    fuzz = json.loads(args.blacksmith_json.read_text(encoding="utf-8")) if args.blacksmith_json.is_file() else {}
    pattern_id, aggressors = first_pattern(fuzz)

    pages = []
    for mapping in mappings:
        dram_id = mapping["dram_page"]
        if dram_id >= len(ranked):
            raise SystemExit(f"dram_page {dram_id} out of ranked profile ({len(ranked)})")
        pfn, recs = ranked[dram_id]
        wpages = dnn_to_weight_pages(mapping["dnn_page"], page_allocs, manifest, max_page)
        wpage = wpages[0] if wpages else (place_pages[0] if place_pages else None)
        pages.append(
            {
                "weight_page": wpage,
                "weight_pages": wpages,
                "dnn_page": mapping["dnn_page"],
                "dram_page": dram_id,
                "pfn": f"0x{pfn:x}",
                "pattern_id": pattern_id,
                "aggressor_to_addr": aggressors,
                "bit_flips": profile_flips_to_blacksmith(pfn, recs),
                "weight_flips": [
                    flip for wp in wpages for flip in flips_by_page.get(wp, [])
                ],
                "mapping": mapping,
            }
        )

    doc = {
        "model": "resnet20_int8",
        "page_size": PAGE_SIZE,
        "asr": meta["asr"],
        "acc_clean": meta["acc_clean"],
        "acc_final": meta["acc_final"],
        "target_class": meta["target_class"],
        "place_pages": place_pages,
        "hammer_pfns": [page["pfn"] for page in pages],
        "blacksmith_json": str(args.blacksmith_json.relative_to(ROOT)),
        "sources": {
            "attack_report": str(args.report.relative_to(ROOT)),
            "profile": str(args.profile.relative_to(ROOT)),
            "pagemap": str(args.pagemap.relative_to(ROOT)),
            "fault_map": str(args.fault_map.relative_to(ROOT)),
        },
        "pages": pages,
    }
    args.out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {args.out} mappings={len(pages)} place_pages={place_pages} "
        f"profile_pages={len(ranked)} pattern={pattern_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
