"""CIFAR-10 eval inputs and trigger."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "artifacts" / "resnet20_int8" / "eval" / "cifar10_test256.npz"
CIFAR_MEAN = np.array([125.3, 123.0, 113.9], dtype=np.float32).reshape(1, 3, 1, 1) / 255.0
CIFAR_STD = np.array([63.0, 62.1, 66.7], dtype=np.float32).reshape(1, 3, 1, 1) / 255.0


def load_model_config(manifest_path: Path) -> dict:
    cfg = manifest_path.parent / "model_config.json"
    if cfg.is_file():
        return json.loads(cfg.read_text(encoding="utf-8"))
    return {}


def load_trigger(manifest_path: Path, checkpoint_path: Path | None = None) -> tuple[np.ndarray, tuple[int, int], int]:
    cfg = load_model_config(manifest_path)
    target_class = int(cfg.get("target_class", 2))
    area = (21, 31)
    npy = manifest_path.parent / str(cfg.get("trigger_pattern") or "trigger_pattern.npy")
    trigger = np.load(npy).astype(np.float32) if npy.is_file() else None
    if trigger is None and checkpoint_path and Path(checkpoint_path).is_file():
        try:
            import torch

            ckpt = torch.load(checkpoint_path, map_location="cpu")
        except Exception:
            ckpt = None
        if isinstance(ckpt, dict):
            if "trigger_pattern" in ckpt:
                trigger = ckpt["trigger_pattern"].float().numpy()
            if "trigger_area" in ckpt:
                raw = ckpt["trigger_area"]
                area = tuple(raw[0]) if isinstance(raw[0], (list, tuple)) else tuple(raw)
            if "target_class" in ckpt:
                target_class = int(ckpt["target_class"])
    if trigger is None:
        raise FileNotFoundError(f"missing trigger at {npy}")
    return trigger, area, target_class


def _normalize_nchw(images: np.ndarray) -> np.ndarray:
    x = images.astype(np.float32)
    if x.max() > 1.5:
        x = x / 255.0
    return (x - CIFAR_MEAN) / CIFAR_STD


def load_cifar_cache(path: Path = DEFAULT_CACHE, limit: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    data = np.load(path)
    images = np.asarray(data["images"], dtype=np.float32)
    labels = np.asarray(data["labels"], dtype=np.int64)
    if images.ndim != 4 or images.shape[1] != 3:
        raise ValueError(f"{path}: expected NCHW CIFAR batch, got {images.shape}")
    peak = float(images.max())
    floor = float(images.min())
    if peak > 10.0 or (floor >= 0.0 and peak <= 1.5):
        images = _normalize_nchw(images)
    if limit is not None:
        images = images[: int(limit)]
        labels = labels[: int(limit)]
    return images, labels


def load_eval_batch(
    manifest_path: Path,
    *,
    eval_samples: int,
    cache: Path = DEFAULT_CACHE,
) -> tuple[np.ndarray, np.ndarray]:
    if cache.is_file():
        return load_cifar_cache(cache, limit=eval_samples)
    root = ROOT / "cifar_data" / "cifar-10-batches-py" / "test_batch"
    if not root.is_file():
        raise FileNotFoundError(
            f"missing {cache} and {root}; run: python3 scripts/download_cifar.py"
        )
    import pickle

    with root.open("rb") as f:
        batch = pickle.load(f, encoding="bytes")
    raw = batch[b"data"].reshape(-1, 3, 32, 32)
    labels = np.array(batch[b"labels"], dtype=np.int64)
    images = _normalize_nchw(raw[:eval_samples])
    return images, labels[:eval_samples]


def apply_trigger(images: np.ndarray, trigger: np.ndarray, area: tuple[int, int]) -> np.ndarray:
    out = images.copy()
    start, end = int(area[0]), int(area[1])
    patch = trigger.astype(np.float32)
    if patch.ndim == 2:
        patch = np.stack([patch] * out.shape[1], axis=0)
    out[:, :, start:end, start:end] = patch
    return out


def first_clean_input(manifest_path: Path, model_inputs=None) -> tuple[str, np.ndarray]:
    images, _ = load_eval_batch(manifest_path, eval_samples=1)
    name = "input"
    if model_inputs is not None:
        weight_names = set()
        try:
            weight_names = {
                inp.get_any_name()
                for inp in model_inputs
                if inp.get_any_name() != "input"
            }
            for inp in model_inputs:
                n = inp.get_any_name()
                if n not in weight_names:
                    name = n
                    break
        except Exception:
            name = "input"
    return name, images[:1].astype(np.float32)
