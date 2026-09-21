#!/usr/bin/env python3
"""OpenVINO iGPU victim: memmap weights, bind, infer, eval."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from framework.eval_inputs import apply_trigger, first_clean_input, load_eval_batch, load_trigger


PAGE_SIZE = 4096
DEFAULT_MANIFEST = "artifacts/resnet20_int8/combined_manifest.json"
DEFAULT_MODEL = "artifacts/resnet20_int8/model.onnx"
DEFAULT_CHECKPOINT = "artifacts/resnet20_int8/backdoored_model.pth"

try:
    import openvino as ov
    from openvino.runtime import Core

    HAS_OPENVINO = True
except ImportError:
    HAS_OPENVINO = False


def array_addr(arr: np.ndarray) -> int:
    return int(arr.__array_interface__["data"][0])


def np_dtype_from_entry(entry: dict) -> np.dtype:
    name = str(entry.get("dtype") or entry.get("ov_type") or "int8").lower()
    if name in {"float32", "f32"}:
        return np.dtype("<f4")
    if name in {"float16", "f16"}:
        return np.dtype("<f2")
    if name in {"uint8", "u8"}:
        return np.dtype("u1")
    return np.dtype("i1")


def fault_memmap(buf: np.ndarray) -> None:
    n = int(buf.nbytes)
    raw = buf.view(np.uint8).reshape(-1)
    for off in range(0, n, PAGE_SIZE):
        int(raw[off])
    if n:
        int(raw[n - 1])


def combined_weight_view(combined: np.ndarray, entry: dict) -> np.ndarray:
    off = int(entry["combined_offset"])
    size = int(entry["bytes_length"])
    shape = tuple(entry["shape"])
    view = combined[off : off + size].view(np_dtype_from_entry(entry)).reshape(shape)
    if not view.flags["C_CONTIGUOUS"]:
        raise RuntimeError(f"{entry.get('tensor_name')} weight view is not C-contiguous")
    return view


def evaluate_backdoor(req, cm, *, checkpoint_path, manifest_path, eval_samples):
    trigger, area, target_class = load_trigger(Path(manifest_path), Path(checkpoint_path) if checkpoint_path else None)
    images, labels = load_eval_batch(Path(manifest_path), eval_samples=int(eval_samples))
    clean_correct = 0
    attack_success = 0
    total = int(images.shape[0])
    bs = 64
    for start in range(0, total, bs):
        xb = images[start : start + bs]
        yb = labels[start : start + bs]
        req.set_tensor("input", ov.Tensor(xb))
        req.infer()
        preds = req.get_tensor(cm.output(0)).data.argmax(axis=1)
        clean_correct += int((preds == yb).sum())
        xb_trig = apply_trigger(xb, trigger, area)
        req.set_tensor("input", ov.Tensor(xb_trig))
        req.infer()
        preds_trig = req.get_tensor(cm.output(0)).data.argmax(axis=1)
        attack_success += int((preds_trig == target_class).sum())
    acc = 100.0 * clean_correct / total if total else 0.0
    asr = 100.0 * attack_success / total if total else 0.0
    print(f"eval_samples={total}")
    print(f"eval_target_class={target_class}")
    print(f"eval_clean_acc={acc:.1f}")
    print(f"eval_asr={asr:.1f}")
    return {"clean_acc": acc, "asr": asr, "total": total}


def busy_pause_us(us: int) -> None:
    if us <= 0:
        return
    deadline = time.perf_counter() + float(us) / 1e6
    while time.perf_counter() < deadline:
        pass


def wait_go() -> None:
    print("WAITING_GO")
    sys.stdout.flush()
    for raw in sys.stdin:
        if raw.strip() == "GO":
            print("OK GO")
            sys.stdout.flush()
            return
    raise RuntimeError("stdin closed before GO")


def _resolve(script_dir: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(script_dir, path)


def _open_weights(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    manifest_path = _resolve(script_dir, args.manifest)
    model_path = _resolve(script_dir, args.model)
    checkpoint_path = _resolve(script_dir, args.checkpoint)
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
    if getattr(args, "weights", None):
        combined_file = _resolve(script_dir, args.weights)
    else:
        combined_file = os.path.join(os.path.dirname(manifest_path), manifest["combined_weights_file"])
    lookup = {w["tensor_name"]: w for w in manifest["weights"]}
    combined = np.memmap(combined_file, dtype=np.int8, mode="c")
    return manifest_path, model_path, checkpoint_path, lookup, combined


def _compile_igpu(model_path: str):
    ie = Core()
    model = ie.read_model(model_path)
    ctx = ie.get_default_context("GPU")
    cm = ie.compile_model(model, ctx)
    return ie, model, ctx, cm


def _bind_weights(model, req, combined, lookup) -> dict[str, np.ndarray]:
    source_views: dict[str, np.ndarray] = {}
    n_ok = 0
    n_alias = 0
    n_w = 0
    for inp in model.inputs:
        name = inp.get_any_name()
        if name == "input":
            continue
        n_w += 1
        entry = lookup[name]
        view = combined_weight_view(combined, entry)
        source_views[name] = view
        req.set_tensor(name, ov.Tensor(view, shared_memory=True))
        bound = req.get_tensor(name).data
        n_ok += 1
        n_alias += int(array_addr(bound) == array_addr(view))
    print(f"bind_ok={n_ok}/{n_w}")
    print(f"bind_alias={n_alias}/{n_ok if n_ok else 0}")
    return source_views


def run_live(args):
    manifest_path, model_path, _checkpoint, lookup, combined = _open_weights(args)
    sys.stdout.reconfigure(line_buffering=True)
    fault_memmap(combined)
    print(f"pid={os.getpid()}")
    print(f"mmap_base={hex(array_addr(combined))}")
    print(f"nbytes={combined.nbytes}")
    print("READY")
    wait_go()
    if not HAS_OPENVINO:
        print("ERR OpenVINO missing")
        return
    ie, model, ctx, cm = _compile_igpu(model_path)
    print("MARKER COMPILE_DONE")
    req = cm.create_infer_request()
    print("MARKER CIR_DONE")
    sys.stdout.flush()
    busy_pause_us(int(getattr(args, "pre_bind_pause_us", 0)))
    source_views = _bind_weights(model, req, combined, lookup)
    fault_memmap(combined)
    print("MARKER BIND_DONE")
    sys.stdout.flush()
    in_name, xin = first_clean_input(Path(manifest_path), model.inputs)
    req.set_input_tensor(0, ov.Tensor(xin))
    print(f"MARKER INPUT_SET name={in_name} shape={list(xin.shape)}")
    sys.stdout.flush()
    busy_pause_us(int(getattr(args, "pre_infer_pause_us", 2000)))
    t0 = time.perf_counter()
    req.infer()
    infer_ms = (time.perf_counter() - t0) * 1000.0
    out = req.get_output_tensor(0).data
    print(f"infer_ms={infer_ms:.1f}")
    print(f"infer_out_argmax={int(np.argmax(out))}")
    print("MARKER INFER_DONE")
    evaluate_backdoor(
        req,
        cm,
        checkpoint_path=Path(_checkpoint) if _checkpoint else None,
        manifest_path=Path(manifest_path),
        eval_samples=int(getattr(args, "eval_samples", 256)),
    )
    print("OK LIVE")


def run_cmd_loop(args):
    manifest_path, model_path, checkpoint_path, lookup, combined = _open_weights(args)
    sys.stdout.reconfigure(line_buffering=True)
    print(f"pid={os.getpid()}")
    print(f"mmap_base={hex(array_addr(combined))}")
    print(f"nbytes={combined.nbytes}")
    print("READY")
    if not HAS_OPENVINO:
        print("ERR OpenVINO missing")
        return
    ie = model = cm = req = ctx = None
    source_views: dict[str, np.ndarray] = {}
    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        try:
            if cmd == "FAULTALL":
                fault_memmap(combined)
                print("OK FAULTALL")
            elif cmd == "COMPILE":
                ie, model, ctx, cm = _compile_igpu(model_path)
                req = cm.create_infer_request()
                print("OK COMPILE")
            elif cmd == "BIND":
                if req is None or model is None:
                    print("ERR BIND not compiled")
                    continue
                source_views = _bind_weights(model, req, combined, lookup)
                print("OK BIND")
            elif cmd == "INFER":
                if req is None or model is None:
                    print("ERR INFER not compiled")
                    continue
                in_name, xin = first_clean_input(Path(manifest_path), model.inputs)
                req.set_tensor(in_name, ov.Tensor(xin))
                t0 = time.perf_counter()
                req.infer()
                infer_ms = (time.perf_counter() - t0) * 1000.0
                out = req.get_output_tensor(0).data
                print(f"infer_ms={infer_ms:.1f}")
                print(f"infer_out_argmax={int(np.argmax(out))}")
                print("OK INFER")
            elif cmd == "EVAL":
                if req is None or cm is None:
                    print("ERR EVAL not compiled")
                    continue
                evaluate_backdoor(
                    req,
                    cm,
                    checkpoint_path=checkpoint_path,
                    manifest_path=manifest_path,
                    eval_samples=int(getattr(args, "eval_samples", 256)),
                )
                print("OK EVAL")
            elif cmd == "EXIT":
                print("OK EXIT")
                break
            else:
                print("ERR unknown")
        except Exception as exc:
            print(f"ERR {cmd} {type(exc).__name__}: {exc}")
            sys.stdout.flush()


def run_standalone(args):
    args.cmd_loop = False
    manifest_path, model_path, checkpoint_path, lookup, combined = _open_weights(args)
    print(f"pid={os.getpid()}")
    print(f"nbytes={combined.nbytes}")
    fault_memmap(combined)
    if not HAS_OPENVINO:
        print("ERR OpenVINO missing")
        return
    ie, model, ctx, cm = _compile_igpu(model_path)
    req = cm.create_infer_request()
    _bind_weights(model, req, combined, lookup)
    in_name, xin = first_clean_input(Path(manifest_path), model.inputs)
    req.set_tensor(in_name, ov.Tensor(xin))
    t0 = time.perf_counter()
    req.infer()
    infer_ms = (time.perf_counter() - t0) * 1000.0
    out = req.get_output_tensor(0).data
    print(f"infer_ms={infer_ms:.1f}")
    print(f"infer_out_argmax={int(np.argmax(out))}")
    evaluate_backdoor(
        req,
        cm,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        eval_samples=int(args.eval_samples),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT)
    parser.add_argument("--weights", default=None)
    parser.add_argument("--cmd-loop", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--pre-bind-pause-us", type=int, default=0)
    parser.add_argument("--pre-infer-pause-us", type=int, default=2000)
    parser.add_argument("--eval-samples", type=int, default=256)
    args = parser.parse_args()
    if args.cmd_loop:
        run_cmd_loop(args)
    elif args.live:
        run_live(args)
    else:
        run_standalone(args)


if __name__ == "__main__":
    main()
