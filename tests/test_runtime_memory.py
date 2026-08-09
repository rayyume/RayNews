import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime_memory


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_read_cgroup_memory_reports_v2_breakdown_and_unlimited_max(tmp_path):
    _write(tmp_path / "memory.current", "104857600\n")
    _write(tmp_path / "memory.max", "max\n")
    _write(
        tmp_path / "memory.stat",
        "anon 62914560\nfile 31457280\nkernel 10485760\nslab 5242880\n",
    )

    assert runtime_memory.read_cgroup_memory(tmp_path) == {
        "version": "v2",
        "current_bytes": 104857600,
        "max_bytes": None,
        "anon_bytes": 62914560,
        "file_bytes": 31457280,
        "kernel_bytes": 10485760,
        "slab_bytes": 5242880,
    }


def test_read_cgroup_memory_falls_back_to_v1_stat_fields(tmp_path):
    _write(tmp_path / "memory.usage_in_bytes", "2048\n")
    _write(tmp_path / "memory.limit_in_bytes", "4096\n")
    _write(
        tmp_path / "memory.stat",
        "total_rss 1024\ntotal_cache 512\ntotal_kernel_stack 128\ntotal_slab 256\n",
    )

    assert runtime_memory.read_cgroup_memory(tmp_path) == {
        "version": "v1",
        "current_bytes": 2048,
        "max_bytes": 4096,
        "anon_bytes": 1024,
        "file_bytes": 512,
        "kernel_bytes": 128,
        "slab_bytes": 256,
    }


def test_read_process_memory_skips_unreadable_or_disappeared_pids_and_sorts_rss(
    tmp_path, monkeypatch
):
    _write(
        tmp_path / "10" / "status",
        "Name:\tworker\nVmRSS:\t2048 kB\nThreads:\t3\n",
    )
    (tmp_path / "10" / "cmdline").write_bytes(b"python\x00worker.py\x00--fast\x00")
    _write(
        tmp_path / "2" / "status",
        "Name:\thelper\nVmRSS:\t512 kB\nThreads:\t1\n",
    )
    (tmp_path / "2" / "cmdline").write_bytes(b"helper\x00")
    (tmp_path / "999").mkdir()
    (tmp_path / "not-a-pid").mkdir()

    real_read_text = Path.read_text

    def permission_denied_for_missing_status(path, *args, **kwargs):
        if path == tmp_path / "999" / "status":
            raise PermissionError("denied")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", permission_denied_for_missing_status)

    assert runtime_memory.read_process_memory(tmp_path) == [
        {
            "pid": 10,
            "name": "worker",
            "cmdline": "python worker.py --fast",
            "rss_bytes": 2048 * 1024,
            "threads": 3,
        },
        {
            "pid": 2,
            "name": "helper",
            "cmdline": "helper",
            "rss_bytes": 512 * 1024,
            "threads": 1,
        },
    ]


def test_runtime_memory_snapshot_combines_stable_cgroup_and_process_schemas(tmp_path):
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    _write(cgroup_root / "memory.current", "100\n")
    _write(cgroup_root / "memory.max", "200\n")
    _write(cgroup_root / "memory.stat", "anon 60\nfile 30\nkernel 10\n")
    _write(proc_root / "7" / "status", "Name:\tapp\nVmRSS:\t1 kB\nThreads:\t2\n")
    (proc_root / "7" / "cmdline").write_bytes(b"app\x00")

    assert runtime_memory.runtime_memory_snapshot(
        cgroup_root=cgroup_root, proc_root=proc_root
    ) == {
        "cgroup": {
            "version": "v2",
            "current_bytes": 100,
            "max_bytes": 200,
            "anon_bytes": 60,
            "file_bytes": 30,
            "kernel_bytes": 10,
            "slab_bytes": None,
        },
        "processes": [
            {
                "pid": 7,
                "name": "app",
                "cmdline": "app",
                "rss_bytes": 1024,
                "threads": 2,
            }
        ],
    }
