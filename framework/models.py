"""ResNet-20 INT8 paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ModelSpec:
    name: str
    directory: Path
    weight_pages: int
    notes: str = ""

    @property
    def manifest(self) -> Path:
        return self.directory / "combined_manifest.json"

    @property
    def model(self) -> Path:
        return self.directory / "model.onnx"

    @property
    def weights(self) -> Path:
        return self.directory / "combined_weights.bin"

    @property
    def targets(self) -> Path:
        return self.directory / "online_targets.json"

    @property
    def checkpoint(self) -> Path:
        return self.directory / "backdoored_model.pth"

    @property
    def trigger(self) -> Path:
        return self.directory / "trigger_pattern.npy"

    @property
    def eval_cache(self) -> Path:
        return self.directory / "eval" / "cifar10_test256.npz"


MODELS: dict[str, ModelSpec] = {
    "resnet20_int8": ModelSpec(
        "resnet20_int8",
        ROOT / "artifacts" / "resnet20_int8",
        66,
        "",
    ),
}


def get_model(name: str) -> ModelSpec:
    key = name.strip()
    if key not in MODELS:
        raise SystemExit(f"unknown model {name!r}; choose from {sorted(MODELS)}")
    spec = MODELS[key]
    if not spec.directory.is_dir():
        raise SystemExit(f"missing model dir {spec.directory}")
    return spec
