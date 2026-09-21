"""Runtime weight-integrity verification helpers (SHA-256 primary)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from typing import Any


CACHELINE = 64


class WeightIntegrityError(RuntimeError):
    """Raised when loaded/bound weights do not match the reference digest."""


def _byte_view(buffer: Any) -> memoryview:
    view = memoryview(buffer)
    if view.ndim != 1 or view.format != "B" or view.itemsize != 1:
        view = view.cast("B")
    if not view.contiguous:
        raise WeightIntegrityError("weight buffer must be C-contiguous for hashing")
    return view


def array_data_ptr(arr: Any) -> int:
    return int(arr.__array_interface__["data"][0])


def compute_sha256(buffer: Any) -> str:
    """SHA-256 over one contiguous buffer via a single hashlib call (no 4 KiB Python loop)."""
    view = _byte_view(buffer)
    return hashlib.sha256(view).hexdigest()


def compute_sha256_and_size(buffers: Any) -> tuple[str, int]:
    """SHA-256 over one buffer or an ordered sequence of contiguous buffers."""
    if isinstance(buffers, (list, tuple)):
        h = hashlib.sha256()
        nbytes = 0
        for segment in buffers:
            view = _byte_view(segment)
            h.update(view)
            nbytes += len(view)
        return h.hexdigest(), nbytes
    view = _byte_view(buffers)
    return hashlib.sha256(view).hexdigest(), len(view)


def compute_md5_and_size(buffers: Any) -> tuple[str, int]:
    """Legacy MD5 helper retained for backward compatibility only."""
    if isinstance(buffers, (list, tuple)):
        h = hashlib.md5()
        nbytes = 0
        for segment in buffers:
            view = _byte_view(segment)
            h.update(view)
            nbytes += len(view)
        return h.hexdigest(), nbytes
    view = _byte_view(buffers)
    return hashlib.md5(view).hexdigest(), len(view)


def validate_manifest_layout(
    weight_lookup: dict[str, dict[str, Any]],
    weight_names: Sequence[str],
    *,
    total_size: int | None = None,
) -> list[tuple[str, int, int]]:
    """
    Validate that named weights form a contiguous cover of [0, total_size).

    Returns canonical (name, offset, size) rows sorted by offset.
    """
    rows: list[tuple[str, int, int]] = []
    for name in weight_names:
        if name not in weight_lookup:
            continue
        entry = weight_lookup[name]
        offset = int(entry["combined_offset"])
        size = int(entry["bytes_length"])
        if size <= 0:
            raise WeightIntegrityError(f"non-positive bytes_length for {name}")
        rows.append((name, offset, size))

    if not rows:
        raise WeightIntegrityError("no weight tensors available for integrity layout")

    rows.sort(key=lambda r: r[1])
    if rows[0][1] != 0:
        raise WeightIntegrityError(
            f"canonical layout gap at start: first offset={rows[0][1]} (expected 0)"
        )

    cursor = 0
    for name, offset, size in rows:
        if offset != cursor:
            if offset < cursor:
                raise WeightIntegrityError(
                    f"overlap before {name}: offset={offset}, expected={cursor}"
                )
            raise WeightIntegrityError(
                f"gap before {name}: offset={offset}, expected={cursor}"
            )
        cursor = offset + size

    if total_size is not None and cursor != int(total_size):
        raise WeightIntegrityError(
            f"layout size mismatch: covered={cursor}, manifest_total={total_size}"
        )
    return rows


def canonical_buffers_from_combined(
    combined_arr: Any,
    weight_lookup: dict[str, dict[str, Any]],
    weight_names: Sequence[str],
) -> list[Any]:
    """Slice combined_arr into canonical weight order (validates no gaps/overlaps)."""
    total = int(getattr(combined_arr, "nbytes", len(combined_arr)))
    rows = validate_manifest_layout(weight_lookup, weight_names, total_size=total)
    return [combined_arr[offset:offset + size] for _, offset, size in rows]


def canonical_buffers_from_bound_views(
    bound_views: dict[str, Any],
    weight_lookup: dict[str, dict[str, Any]],
    weight_names: Sequence[str],
    *,
    total_size: int | None = None,
) -> list[Any]:
    """Order InferRequest-bound host views by combined_offset for hashing."""
    rows = validate_manifest_layout(
        weight_lookup,
        [n for n in weight_names if n in bound_views],
        total_size=total_size,
    )
    buffers = []
    for name, _offset, size in rows:
        view = bound_views[name]
        flat = view.reshape(-1) if hasattr(view, "reshape") else view
        nbytes = int(getattr(flat, "nbytes", len(flat)))
        if nbytes != size:
            raise WeightIntegrityError(
                f"bound view size mismatch for {name}: got={nbytes}, expected={size}"
            )
        buffers.append(flat)
    return buffers


def check_bound_aliases(
    bound_views: dict[str, Any],
    source_views: dict[str, Any],
) -> list[dict[str, Any]]:
    """Assert each bound InferRequest host view aliases the source NumPy buffer."""
    report = []
    for name, bound in bound_views.items():
        if name not in source_views:
            raise WeightIntegrityError(f"missing source view for bound tensor {name}")
        source = source_views[name]
        source_ptr = array_data_ptr(source)
        bound_ptr = array_data_ptr(bound)
        source_nbytes = int(source.nbytes)
        bound_nbytes = int(bound.nbytes)
        ok = source_ptr == bound_ptr and source_nbytes == bound_nbytes
        entry = {
            "name": name,
            "source_ptr": source_ptr,
            "bound_ptr": bound_ptr,
            "nbytes": bound_nbytes,
            "aliases": ok,
        }
        report.append(entry)
        if not ok:
            raise WeightIntegrityError(
                f"bound tensor {name} does not alias source "
                f"(source={hex(source_ptr)}, bound={hex(bound_ptr)}, "
                f"nbytes {source_nbytes} vs {bound_nbytes})"
            )
    return report


def verify_digest(
    buffers: Any,
    expected: str | None,
    *,
    algo: str = "sha256",
    abort: bool = True,
) -> dict[str, Any]:
    """Hash buffers and compare against a trusted offline digest."""
    algo = str(algo).lower()
    if not expected:
        raise WeightIntegrityError(f"missing manifest weights_{algo} reference")

    if algo == "sha256":
        actual, nbytes = compute_sha256_and_size(buffers)
    elif algo == "md5":
        actual, nbytes = compute_md5_and_size(buffers)
    else:
        raise WeightIntegrityError(f"unsupported integrity algo: {algo}")

    ok = actual.lower() == str(expected).lower()
    result = {
        "ok": ok,
        "algo": algo,
        "actual": actual,
        "expected": str(expected),
        "nbytes": int(nbytes),
    }
    if not ok and abort:
        raise WeightIntegrityError(
            f"weight integrity mismatch ({algo}): "
            f"expected={str(expected)[:12]}, got={actual[:12]}"
        )
    return result


def compute_weights_md5(buffer: Any, **_kwargs: Any) -> str:
    digest, _ = compute_md5_and_size(buffer)
    return digest


def verify_weights_md5(
    buffer: Any,
    expected_md5: str | None,
    *,
    abort: bool = True,
    **_kwargs: Any,
) -> dict[str, Any]:
    return verify_digest(buffer, expected_md5, algo="md5", abort=abort)


def verify_loaded_weights_md5(
    combined_arr: Any | None,
    weight_arrays: dict[str, Any],
    weight_names: Iterable[str],
    weight_lookup: dict[str, dict[str, Any]],
    expected_md5: str | None,
    *,
    abort: bool = True,
    **_kwargs: Any,
) -> dict[str, Any]:
    names = list(weight_names)
    if combined_arr is not None:
        buffers = canonical_buffers_from_combined(combined_arr, weight_lookup, names)
    else:
        # Per-tensor buffers when a combined mapping is not available.
        rows = validate_manifest_layout(
            weight_lookup,
            [n for n in names if n in weight_arrays],
            total_size=None,
        )
        buffers = []
        for name, _off, size in rows:
            arr = weight_arrays[name]
            if int(arr.nbytes) != size:
                raise WeightIntegrityError(
                    f"split shard size mismatch for {name}: "
                    f"got={arr.nbytes}, expected={size}"
                )
            buffers.append(arr)
    return verify_digest(buffers, expected_md5, algo="md5", abort=abort)


def verify_bound_weights(
    bound_views: dict[str, Any],
    weight_lookup: dict[str, dict[str, Any]],
    weight_names: Sequence[str],
    expected_digest: str | None,
    *,
    algo: str = "sha256",
    total_size: int | None = None,
    abort: bool = True,
) -> dict[str, Any]:
    """Hash InferRequest-bound host views in canonical order and compare."""
    buffers = canonical_buffers_from_bound_views(
        bound_views,
        weight_lookup,
        weight_names,
        total_size=total_size,
    )
    return verify_digest(buffers, expected_digest, algo=algo, abort=abort)
