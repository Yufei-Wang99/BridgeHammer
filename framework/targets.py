"""Load online_targets.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


def _parse_int(value: object) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 0)


@dataclass
class TargetPage:
    weight_page: int | None
    weight_pages: list[int]
    dnn_page: int | None
    dram_page: int | None
    pfn: int
    pattern_id: str
    aggressor_to_addr: list
    bit_flips: list[dict]
    weight_flips: list[dict] = field(default_factory=list)


@dataclass
class OnlineTargets:
    path: Path
    model: str
    asr: float
    place_pages: list[int]
    pages: list[TargetPage]
    blacksmith_json: Path
    hammer_pfns: list[int]
    wanted_pfns: list[int]


def align_place_pfns(place_pages: list[int], mappings: list[TargetPage]) -> list[int]:
    """One listed vulnerable PFN per weight-file page, mapping order first."""
    used: set[int] = set()
    out: list[int] = []
    for page in place_pages:
        match = next(
            (m for m in mappings if m.pfn and m.pfn not in used and m.weight_page == page),
            None,
        )
        if match is None:
            match = next(
                (
                    m
                    for m in mappings
                    if m.pfn and m.pfn not in used and page in m.weight_pages
                ),
                None,
            )
        if match is None:
            match = next((m for m in mappings if m.pfn and m.pfn not in used), None)
        if match is None:
            continue
        out.append(match.pfn)
        used.add(match.pfn)
    return out


def load_online_targets(path: Path, repo_root: Path | None = None) -> OnlineTargets:
    data = json.loads(path.read_text(encoding="utf-8"))
    root = repo_root or path.resolve().parents[2]
    pages: list[TargetPage] = []
    for raw in data.get("pages") or []:
        extra = [int(x) for x in (raw.get("weight_pages") or [])]
        pages.append(
            TargetPage(
                weight_page=int(raw["weight_page"]) if raw.get("weight_page") is not None else None,
                weight_pages=extra,
                dnn_page=int(raw["dnn_page"]) if raw.get("dnn_page") is not None else None,
                dram_page=int(raw["dram_page"]) if raw.get("dram_page") is not None else None,
                pfn=_parse_int(raw.get("pfn") or 0),
                pattern_id=str(raw.get("pattern_id") or ""),
                aggressor_to_addr=list(raw.get("aggressor_to_addr") or []),
                bit_flips=list(raw.get("bit_flips") or []),
                weight_flips=list(raw.get("weight_flips") or []),
            )
        )
    place = [int(p) for p in (data.get("place_pages") or [])]
    if not place:
        place = sorted({p.weight_page for p in pages if p.weight_page is not None})
    hammer = [_parse_int(x) for x in (data.get("hammer_pfns") or [p.pfn for p in pages if p.pfn])]
    wanted = align_place_pfns(place, pages)
    bs = data.get("blacksmith_json") or "artifacts/resnet20_int8/offline/blacksmith_fuzz_summary.json"
    bs_path = Path(bs)
    if not bs_path.is_absolute():
        bs_path = root / bs_path
    return OnlineTargets(
        path=path,
        model=str(data.get("model") or path.parent.name),
        asr=float(data.get("asr") or 0.0),
        place_pages=place,
        pages=pages,
        blacksmith_json=bs_path,
        hammer_pfns=hammer,
        wanted_pfns=wanted,
    )


def _agg_entry(item: object) -> tuple[int, int, int] | None:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    loc = item[1]
    if not isinstance(loc, dict):
        return None
    return int(item[0]), int(loc.get("bank") or 0), int(loc.get("row") or 0)


def pattern_geometry(targets: OnlineTargets) -> dict:
    """Recorded aggressor geometry and access order for neighbor hold/hammer."""
    aggressors: list[tuple[int, int, int]] = []
    orig_victim_row: int | None = None
    for page in targets.pages:
        for item in page.aggressor_to_addr:
            parsed = _agg_entry(item)
            if parsed is not None:
                aggressors.append(parsed)
        if aggressors:
            for flip in page.bit_flips:
                if not isinstance(flip, dict):
                    continue
                dram = flip.get("dram_addr") or {}
                if isinstance(dram, dict) and dram.get("row") is not None:
                    orig_victim_row = int(dram["row"])
                    break
            break
    access_ids: list[int] = []
    if targets.blacksmith_json.is_file():
        data = json.loads(targets.blacksmith_json.read_text(encoding="utf-8"))
        patterns = data.get("hammering_patterns") or []
        if patterns:
            access_ids = [int(x) for x in (patterns[0].get("access_ids") or [])]
            if orig_victim_row is None:
                for mapping in patterns[0].get("address_mappings") or []:
                    for run in mapping.get("bit_flips") or []:
                        for flip in run or []:
                            if not isinstance(flip, dict):
                                continue
                            dram = flip.get("dram_addr") or {}
                            if isinstance(dram, dict) and dram.get("row") is not None:
                                orig_victim_row = int(dram["row"])
                                break
                        if orig_victim_row is not None:
                            break
                    if orig_victim_row is not None:
                        break
    if orig_victim_row is None and aggressors:
        rows = [row for _i, _b, row in aggressors]
        orig_victim_row = (min(rows) + max(rows)) // 2
    return {
        "aggressors": aggressors,
        "orig_victim_row": orig_victim_row,
        "access_ids": access_ids,
    }
