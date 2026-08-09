"""Small, dependency-free readers for Linux cgroup and process memory data.

The functions in this module only read kernel pseudo-filesystems.  They are
intentionally tolerant of cgroup layout differences and of processes exiting
while ``/proc`` is being scanned, so diagnostics never make their caller fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_CGROUP_FIELDS = (
    "version",
    "current_bytes",
    "max_bytes",
    "anon_bytes",
    "file_bytes",
    "kernel_bytes",
    "slab_bytes",
)


def _read_int(path: Path) -> int | None:
    """Return an integer file value, or ``None`` when it is not available."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_key_values(path: Path) -> dict[str, int]:
    """Read whitespace-separated integer pairs, retaining valid partial data."""
    result: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result

    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            result[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return result


def _empty_cgroup_memory() -> dict[str, str | int | None]:
    return {
        "version": "unknown",
        "current_bytes": None,
        "max_bytes": None,
        "anon_bytes": None,
        "file_bytes": None,
        "kernel_bytes": None,
        "slab_bytes": None,
    }


def _kernel_breakdown(
    stat: dict[str, int], prefix: str = ""
) -> tuple[int | None, int | None]:
    """Return aggregate kernel and slab values from one cgroup stat namespace.

    A kernel aggregate already includes all kernel components, so it wins when
    the kernel provides it.  Older cgroup layouts expose only non-overlapping
    components instead.  In that case, count the slab aggregate once, or (only
    when it is absent) its reclaimable and unreclaimable subcomponents.
    """
    slab = stat.get(f"{prefix}slab")
    if slab is None:
        slab_components = [
            stat.get(f"{prefix}slab_reclaimable"),
            stat.get(f"{prefix}slab_unreclaimable"),
        ]
        known_slab_components = [
            value for value in slab_components if value is not None
        ]
        if known_slab_components:
            slab = sum(known_slab_components)

    aggregate = stat.get(f"{prefix}kernel")
    if aggregate is not None:
        return aggregate, slab

    component_values = [
        stat.get(f"{prefix}kernel_stack"),
        stat.get(f"{prefix}pagetables"),
        stat.get(f"{prefix}percpu"),
        stat.get(f"{prefix}sock"),
        stat.get(f"{prefix}vmalloc"),
        slab,
    ]
    known_components = [value for value in component_values if value is not None]
    return (sum(known_components) if known_components else None), slab


def _v1_root(root: Path) -> Path:
    """Support callers passing either the memory controller or cgroup root."""
    if (root / "memory.usage_in_bytes").exists():
        return root
    return root / "memory"


def read_cgroup_memory(root: str | Path = "/sys/fs/cgroup") -> dict[str, str | int | None]:
    """Return a stable cgroup memory breakdown for v2, v1, or unavailable data."""
    root_path = Path(root)
    result = _empty_cgroup_memory()

    if (root_path / "memory.current").exists():
        stat = _read_key_values(root_path / "memory.stat")
        kernel_bytes, slab_bytes = _kernel_breakdown(stat)
        result.update(
            version="v2",
            current_bytes=_read_int(root_path / "memory.current"),
            max_bytes=_read_int(root_path / "memory.max"),
            anon_bytes=stat.get("anon"),
            file_bytes=stat.get("file"),
            kernel_bytes=kernel_bytes,
            slab_bytes=slab_bytes,
        )
        return result

    v1_root = _v1_root(root_path)
    if not (v1_root / "memory.usage_in_bytes").exists():
        return result

    stat = _read_key_values(v1_root / "memory.stat")
    has_total_kernel_data = any(
        key.startswith("total_")
        and key
        in {
            "total_kernel",
            "total_kernel_stack",
            "total_pagetables",
            "total_percpu",
            "total_sock",
            "total_vmalloc",
            "total_slab",
            "total_slab_reclaimable",
            "total_slab_unreclaimable",
        }
        for key in stat
    )
    kernel_bytes, slab_bytes = _kernel_breakdown(
        stat, "total_" if has_total_kernel_data else ""
    )
    result.update(
        version="v1",
        current_bytes=_read_int(v1_root / "memory.usage_in_bytes"),
        max_bytes=_read_int(v1_root / "memory.limit_in_bytes"),
        anon_bytes=stat.get("total_rss", stat.get("rss")),
        file_bytes=stat.get("total_cache", stat.get("cache")),
        kernel_bytes=kernel_bytes,
        slab_bytes=slab_bytes,
    )
    return result


def _parse_process_status(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


def _status_int(status: dict[str, str], key: str) -> int | None:
    value = status.get(key)
    if not value:
        return None
    try:
        return int(value.split()[0])
    except (IndexError, ValueError):
        return None


def _read_cmdline(path: Path) -> str:
    try:
        arguments = path.read_bytes().split(b"\0")
    except OSError:
        return ""
    return " ".join(argument.decode("utf-8", errors="replace") for argument in arguments if argument)


def read_process_memory(proc_root: str | Path = "/proc") -> list[dict[str, Any]]:
    """Return processes in descending RSS order, ignoring per-PID read races."""
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return []

    processes: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        status = _parse_process_status(entry / "status")
        if status is None:
            continue
        rss_kib = _status_int(status, "VmRSS")
        processes.append(
            {
                "pid": int(entry.name),
                "name": status.get("Name", ""),
                "cmdline": _read_cmdline(entry / "cmdline"),
                "rss_bytes": rss_kib * 1024 if rss_kib is not None else None,
                "threads": _status_int(status, "Threads"),
            }
        )

    return sorted(
        processes,
        key=lambda process: (-(process["rss_bytes"] or 0), process["pid"]),
    )


def runtime_memory_snapshot(
    cgroup_root: str | Path = "/sys/fs/cgroup",
    proc_root: str | Path = "/proc",
) -> dict[str, Any]:
    """Combine cgroup and process readers into a stable top-level schema."""
    return {
        "cgroup": read_cgroup_memory(cgroup_root),
        "processes": read_process_memory(proc_root),
    }
