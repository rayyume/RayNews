import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import runtime_memory
import web_server


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
        "kernel_bytes": 384,
        "slab_bytes": 256,
    }


def test_read_cgroup_memory_derives_v2_kernel_from_non_overlapping_components(
    tmp_path,
):
    _write(tmp_path / "memory.current", "1000\n")
    _write(tmp_path / "memory.max", "2000\n")
    _write(
        tmp_path / "memory.stat",
        "anon 500\n"
        "file 300\n"
        "kernel_stack 10\n"
        "pagetables 20\n"
        "percpu 30\n"
        "sock 40\n"
        "vmalloc 50\n"
        "slab 60\n"
        "slab_reclaimable 25\n"
        "slab_unreclaimable 35\n",
    )

    memory = runtime_memory.read_cgroup_memory(tmp_path)

    assert memory["kernel_bytes"] == 210
    assert memory["slab_bytes"] == 60


def test_read_cgroup_memory_uses_v1_total_kernel_components_before_local_ones(
    tmp_path,
):
    _write(tmp_path / "memory.usage_in_bytes", "2048\n")
    _write(tmp_path / "memory.limit_in_bytes", "4096\n")
    _write(
        tmp_path / "memory.stat",
        "rss 999\n"
        "cache 888\n"
        "kernel_stack 777\n"
        "pagetables 666\n"
        "slab 555\n"
        "total_rss 1024\n"
        "total_cache 512\n"
        "total_kernel_stack 128\n"
        "total_pagetables 64\n"
        "total_percpu 32\n"
        "total_sock 16\n"
        "total_vmalloc 8\n"
        "total_slab 256\n"
        "total_slab_reclaimable 100\n"
        "total_slab_unreclaimable 156\n",
    )

    memory = runtime_memory.read_cgroup_memory(tmp_path)

    assert memory["anon_bytes"] == 1024
    assert memory["file_bytes"] == 512
    assert memory["kernel_bytes"] == 504
    assert memory["slab_bytes"] == 256


def test_read_cgroup_memory_tolerates_non_utf8_stat_file(tmp_path):
    _write(tmp_path / "memory.current", "104857600\n")
    _write(tmp_path / "memory.max", "max\n")
    (tmp_path / "memory.stat").write_bytes(b"anon \xff\xfe garbage\n")

    memory = runtime_memory.read_cgroup_memory(tmp_path)

    assert memory["version"] == "v2"
    assert memory["current_bytes"] == 104857600
    assert memory["max_bytes"] is None
    assert memory["anon_bytes"] is None


def test_read_cgroup_memory_prefers_aggregate_kernel_counters(tmp_path):
    v2_root = tmp_path / "v2"
    _write(v2_root / "memory.current", "100\n")
    _write(v2_root / "memory.max", "200\n")
    _write(
        v2_root / "memory.stat",
        "kernel 900\nkernel_stack 10\npagetables 20\nslab 30\n",
    )

    v1_root = tmp_path / "v1"
    _write(v1_root / "memory.usage_in_bytes", "100\n")
    _write(v1_root / "memory.limit_in_bytes", "200\n")
    _write(
        v1_root / "memory.stat",
        "total_kernel 800\ntotal_kernel_stack 10\ntotal_pagetables 20\ntotal_slab 30\n",
    )

    assert runtime_memory.read_cgroup_memory(v2_root)["kernel_bytes"] == 900
    assert runtime_memory.read_cgroup_memory(v1_root)["kernel_bytes"] == 800


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


MIB = 1024 * 1024


def _snapshot(current_bytes, rss_values=(70, 10, 60, 20, 50, 30, 40)):
    return {
        "cgroup": {
            "version": "v2",
            "current_bytes": current_bytes,
            "max_bytes": 1024 * MIB,
            "anon_bytes": 500 * MIB,
            "file_bytes": 250 * MIB,
            "kernel_bytes": 50 * MIB,
            "slab_bytes": 10 * MIB,
        },
        "processes": [
            {
                "pid": pid,
                "name": f"process-{pid}",
                "cmdline": f"python process-{pid}.py",
                "rss_bytes": rss * MIB,
                "threads": pid,
            }
            for pid, rss in enumerate(rss_values, start=1)
        ],
    }


def test_memory_sample_warns_at_threshold_and_keeps_only_five_largest_processes(
    monkeypatch,
):
    monkeypatch.setattr(
        web_server,
        "runtime_memory_snapshot",
        lambda: _snapshot(800 * MIB),
    )
    monkeypatch.setattr(
        web_server,
        "_refresh_runtime_stats",
        lambda: {
            "article_cache_items": 3,
            "article_cache_bytes": 123456,
            "article_cache_inflight": 1,
        },
    )
    monkeypatch.setattr(web_server, "MEMORY_WARN_BYTES", 800 * MIB)

    sample = web_server._memory_sample_once()

    assert sample["warning"] is True
    assert sample["cgroup"]["anon_bytes"] == 500 * MIB
    assert sample["cgroup"]["file_bytes"] == 250 * MIB
    assert [process["pid"] for process in sample["processes"]] == [1, 3, 5, 7, 6]
    assert sample["application"]["refresh"]["article_cache_bytes"] == 123456


def test_memory_sample_does_not_warn_below_threshold(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "runtime_memory_snapshot",
        lambda: _snapshot(799 * MIB, rss_values=()),
    )
    monkeypatch.setattr(
        web_server,
        "_refresh_runtime_stats",
        lambda: {"status": "unavailable"},
    )
    monkeypatch.setattr(web_server, "MEMORY_WARN_BYTES", 800 * MIB)

    assert web_server._memory_sample_once()["warning"] is False


def test_memory_sample_marks_unavailable_refresh_metrics_as_error(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "runtime_memory_snapshot",
        lambda: _snapshot(100 * MIB, rss_values=()),
    )
    monkeypatch.setattr(
        web_server,
        "_refresh_runtime_stats",
        lambda: {"status": "unavailable"},
    )

    sample = web_server._memory_sample_once()

    assert sample["application"]["refresh"] == {"status": "unavailable"}
    assert sample["error"] == "refresh unavailable"


@pytest.mark.parametrize("failing_dependency", ["snapshot", "refresh"])
def test_memory_sample_reports_dependency_errors(monkeypatch, failing_dependency):
    monkeypatch.setattr(
        web_server,
        "runtime_memory_snapshot",
        lambda: _snapshot(100 * MIB, rss_values=()),
    )
    monkeypatch.setattr(
        web_server,
        "_refresh_runtime_stats",
        lambda: {
            "article_cache_items": 0,
            "article_cache_bytes": 0,
            "article_cache_inflight": 0,
        },
    )

    def fail():
        raise RuntimeError(f"{failing_dependency} unavailable")

    if failing_dependency == "snapshot":
        monkeypatch.setattr(web_server, "runtime_memory_snapshot", fail)
    else:
        monkeypatch.setattr(web_server, "_refresh_runtime_stats", fail)

    sample = web_server._memory_sample_once()

    assert sample["warning"] is False
    assert failing_dependency in sample["error"]
    assert "unavailable" in sample["error"]


def test_memory_monitor_loop_continues_after_sample_error_and_prints_compact_json(
    capsys,
):
    calls = 0

    def sample_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first sample failed")
        return {"warning": False, "processes": [], "note": "中文 value"}

    def stop_after_second_sample(_interval):
        if calls == 2:
            raise StopIteration

    with pytest.raises(StopIteration):
        web_server._memory_monitor_loop(
            sample_once=sample_once,
            sleep_fn=stop_after_second_sample,
            interval_seconds=10,
        )

    lines = capsys.readouterr().out.splitlines()
    assert calls == 2
    assert len(lines) == 2
    assert all(line.startswith("[memory] ") for line in lines)
    assert json.loads(lines[0].removeprefix("[memory] "))["error"] == (
        "memory sample failed: first sample failed"
    )
    assert json.loads(lines[1].removeprefix("[memory] "))["note"] == "中文 value"
    assert '"warning":false' in lines[1]
    assert '"note":"中文 value"' in lines[1]


def test_every_reserved_memory_log_line_has_a_json_payload(capsys):
    with pytest.raises(StopIteration):
        web_server._memory_monitor_loop(
            sample_once=lambda: {"warning": False, "processes": []},
            sleep_fn=lambda _interval: (_ for _ in ()).throw(StopIteration),
            interval_seconds=10,
        )
    web_server._announce_memory_monitor_started()

    lines = capsys.readouterr().out.splitlines()
    memory_lines = [line for line in lines if line.startswith("[memory] ")]

    assert len(memory_lines) == 1
    assert [
        json.loads(line.removeprefix("[memory] ")) for line in memory_lines
    ] == [{"warning": False, "processes": []}]
    assert "[memory-monitor] Background memory monitor thread started" in lines


def test_memory_monitor_environment_defaults_and_invalid_values(monkeypatch):
    for name in (
        "MEMORY_MONITOR_ENABLED",
        "MEMORY_MONITOR_INTERVAL_SECONDS",
        "MEMORY_WARN_MB",
    ):
        monkeypatch.delenv(name, raising=False)
    assert web_server._memory_monitor_config_from_env() == {
        "enabled": True,
        "interval_seconds": 60,
        "warn_mb": 768,
    }

    monkeypatch.setenv("MEMORY_MONITOR_ENABLED", "not-a-bool")
    monkeypatch.setenv("MEMORY_MONITOR_INTERVAL_SECONDS", "not-an-int")
    monkeypatch.setenv("MEMORY_WARN_MB", "not-an-int")
    assert web_server._memory_monitor_config_from_env() == {
        "enabled": True,
        "interval_seconds": 60,
        "warn_mb": 768,
    }


def test_memory_monitor_environment_accepts_false_and_clamps_short_interval(
    monkeypatch,
):
    monkeypatch.setenv("MEMORY_MONITOR_ENABLED", "OFF")
    monkeypatch.setenv("MEMORY_MONITOR_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("MEMORY_WARN_MB", "512")

    assert web_server._memory_monitor_config_from_env() == {
        "enabled": False,
        "interval_seconds": 10,
        "warn_mb": 512,
    }


def test_memory_monitor_start_is_idempotent_and_creates_one_daemon(monkeypatch):
    created = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(web_server, "MEMORY_MONITOR_ENABLED", True)
    monkeypatch.setattr(web_server, "_memory_monitor_thread", None)

    first = web_server._start_memory_monitor_thread(thread_factory=FakeThread)
    second = web_server._start_memory_monitor_thread(thread_factory=FakeThread)

    assert first is second
    assert len(created) == 1
    assert created[0].started is True
    assert created[0].kwargs == {
        "target": web_server._memory_monitor_loop,
        "daemon": True,
        "name": "memory-monitor",
    }


def test_memory_monitor_start_does_nothing_when_disabled(monkeypatch):
    def unexpected_thread(**_kwargs):
        raise AssertionError("disabled monitor must not create a thread")

    monkeypatch.setattr(web_server, "MEMORY_MONITOR_ENABLED", False)
    monkeypatch.setattr(web_server, "_memory_monitor_thread", None)

    assert (
        web_server._start_memory_monitor_thread(thread_factory=unexpected_thread)
        is None
    )


def test_memory_monitor_concurrent_starters_create_and_start_only_one_daemon(
    monkeypatch,
):
    callers_ready = threading.Barrier(3)
    factories_ready = threading.Barrier(2)
    created = []
    started = []
    results = []
    errors = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.alive = False
            created.append(self)
            try:
                # Without a starter lock both factories arrive, making the old
                # check-then-start race deterministic. With the lock, this times
                # out once while the other caller waits outside the critical section.
                factories_ready.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                pass

        def start(self):
            self.alive = True
            started.append(self)

        def is_alive(self):
            return self.alive

    def start_monitor():
        callers_ready.wait()
        try:
            results.append(
                web_server._start_memory_monitor_thread(thread_factory=FakeThread)
            )
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(web_server, "MEMORY_MONITOR_ENABLED", True)
    monkeypatch.setattr(web_server, "_memory_monitor_thread", None)
    callers = [threading.Thread(target=start_monitor) for _ in range(2)]
    for caller in callers:
        caller.start()
    callers_ready.wait()
    for caller in callers:
        caller.join(timeout=2)

    assert all(not caller.is_alive() for caller in callers)
    assert errors == []
    assert len(created) == 1
    assert started == created
    assert results == [created[0], created[0]]


def test_memory_monitor_replaces_a_registered_thread_that_is_not_alive(monkeypatch):
    class DeadThread:
        def is_alive(self):
            return False

    class ReplacementThread:
        def __init__(self, **_kwargs):
            self.alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

    dead = DeadThread()
    monkeypatch.setattr(web_server, "MEMORY_MONITOR_ENABLED", True)
    monkeypatch.setattr(web_server, "_memory_monitor_thread", dead)

    replacement = web_server._start_memory_monitor_thread(
        thread_factory=ReplacementThread
    )

    assert replacement is not dead
    assert replacement.is_alive()
    assert web_server._memory_monitor_thread is replacement


def test_memory_monitor_start_failure_does_not_poison_registration(monkeypatch):
    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start")

    class WorkingThread:
        def __init__(self, **_kwargs):
            self.alive = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

    monkeypatch.setattr(web_server, "MEMORY_MONITOR_ENABLED", True)
    monkeypatch.setattr(web_server, "_memory_monitor_thread", None)

    with pytest.raises(RuntimeError, match="cannot start"):
        web_server._start_memory_monitor_thread(thread_factory=FailingThread)

    assert web_server._memory_monitor_thread is None
    retry = web_server._start_memory_monitor_thread(thread_factory=WorkingThread)
    assert retry.is_alive()
    assert web_server._memory_monitor_thread is retry
