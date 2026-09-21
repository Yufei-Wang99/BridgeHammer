"""OpenVINO 2025.1 victim environment."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OV251_PYTHON = ROOT / ".venv_ov251" / "bin" / "python3"
_CONDA_ENV_KEYS = (
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CONDA_PYTHON_EXE",
    "CONDA_EXE",
    "CONDA_SHLVL",
    "PYTHONHOME",
)


def ov251_python() -> Path:
    override = os.environ.get("BRIDGEHAMMER_PYTHON")
    if override:
        return Path(override)
    if not OV251_PYTHON.is_file():
        raise SystemExit(
            f"missing {OV251_PYTHON}"
        )
    return OV251_PYTHON


def igpu_victim_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = {**(base or os.environ), "PYTHONUNBUFFERED": "1"}
    for key in _CONDA_ENV_KEYS:
        env.pop(key, None)
    system_libs = "/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu"
    current = env.get("LD_LIBRARY_PATH", "")
    kept = [
        p
        for p in current.split(":")
        if p and "conda" not in p.lower() and "anaconda" not in p.lower()
    ]
    env["LD_LIBRARY_PATH"] = f"{system_libs}:{':'.join(kept)}" if kept else system_libs
    env.setdefault("MALLOC_MMAP_THRESHOLD_", "4096")
    return env
