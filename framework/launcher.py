#!/usr/bin/env python3
"""Spawn the memmap victim (cmd-loop or live)."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _parse_kv_lines(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key] = value
    return out


def launch_cmd_victim(
    *,
    python: Path,
    victim_script: Path,
    victim_args: list[str],
    env: Optional[dict[str, str]] = None,
    cwd: Optional[Path] = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(python), str(victim_script), *victim_args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(cwd or ROOT),
        env=env,
    )


def _unread_stderr(proc: subprocess.Popen[str]) -> str:
    if proc.stderr is None:
        return ""
    try:
        os.set_blocking(proc.stderr.fileno(), False)
        return proc.stderr.read() or ""
    except Exception:
        return ""


def wait_ready(proc: subprocess.Popen[str], timeout_s: float = 60.0) -> dict[str, str]:
    assert proc.stdout is not None
    deadline = time.time() + timeout_s
    info: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"victim exited before READY: {err}")
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.01)
            continue
        stripped = line.strip()
        if stripped == "READY":
            return _parse_kv_lines(info)
        info.append(stripped)
    err = _unread_stderr(proc)
    alive = proc.poll() is None
    raise TimeoutError(
        "timed out waiting for victim READY "
        f"(alive={alive} stdout={info!r} stderr={err!r})"
    )


def send_cmd(proc: subprocess.Popen[str], cmd: str, timeout_s: float = 30.0) -> None:
    seen, err = send_cmd_capture(proc, cmd, timeout_s)
    if err:
        raise RuntimeError(err)
    _ = seen


def send_cmd_capture(
    proc: subprocess.Popen[str], cmd: str, timeout_s: float
) -> tuple[list[str], str | None]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    expect = f"OK {cmd}"
    seen: list[str] = []
    if proc.poll() is not None:
        if cmd == "EXIT":
            return [], None
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"victim already exited before {cmd}: {err}")
    try:
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
    except BrokenPipeError:
        if cmd == "EXIT":
            return [], None
        raise
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                rest = proc.stdout.read() if proc.stdout else ""
                if expect in rest.splitlines() or cmd == "EXIT":
                    return seen, None
                err = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"victim exited during {cmd}: {err}")
            time.sleep(0.01)
            continue
        stripped = line.strip()
        if stripped == expect:
            return seen, None
        seen.append(stripped)
        if stripped.startswith("ERR"):
            return seen, stripped
    if cmd == "EXIT":
        return seen, None
    raise TimeoutError(f"timed out waiting for {expect}")


def stop_victim(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.write("EXIT\n")
            proc.stdin.flush()
            proc.wait(timeout=2)
            return
    except Exception:
        pass
    try:
        os.kill(proc.pid, signal.SIGCONT)
    except OSError:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    from framework.env import igpu_victim_env, ov251_python

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default="artifacts/resnet20_int8/combined_manifest.json")
    ap.add_argument("--model", default="artifacts/resnet20_int8/model.onnx")
    ap.add_argument("--weights", default="artifacts/resnet20_int8/combined_weights.bin")
    ap.add_argument("--cmd-loop", action="store_true")
    args = ap.parse_args(argv)

    victim = ROOT / "victim.py"
    victim_args = [
        "--manifest", args.manifest,
        "--model", args.model,
        "--weights", args.weights,
    ]
    if args.cmd_loop:
        victim_args.append("--cmd-loop")
    proc = launch_cmd_victim(
        python=ov251_python(),
        victim_script=victim,
        victim_args=victim_args,
        env=igpu_victim_env(),
        cwd=ROOT,
    )
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
