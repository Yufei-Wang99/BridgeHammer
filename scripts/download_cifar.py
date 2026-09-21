#!/usr/bin/env python3
"""Download CIFAR-10 and write the eval cache."""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "artifacts" / "resnet20_int8" / "eval" / "cifar10_test256.npz"
MEAN = np.array([125.3, 123.0, 113.9], dtype=np.float32).reshape(1, 3, 1, 1) / 255.0
STD = np.array([63.0, 62.1, 66.7], dtype=np.float32).reshape(1, 3, 1, 1) / 255.0


def write_cache(test_batch: Path, n: int = 256) -> Path:
    with test_batch.open("rb") as f:
        batch = pickle.load(f, encoding="bytes")
    raw = batch[b"data"].reshape(-1, 3, 32, 32).astype(np.float32) / 255.0
    labels = np.array(batch[b"labels"], dtype=np.int64)
    images = (raw - MEAN) / STD
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(CACHE, images=images[:n], labels=labels[:n])
    return CACHE


def main() -> int:
    from torchvision import datasets

    root = ROOT / "cifar_data"
    root.mkdir(parents=True, exist_ok=True)
    datasets.CIFAR10(root=str(root), train=False, download=True)
    cache = write_cache(root / "cifar-10-batches-py" / "test_batch")
    print(f"CIFAR-10 test set in {root}")
    print(f"eval cache {cache}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
