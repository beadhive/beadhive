"""Real process-tree coverage for the host-local stream lifetime owner (bh-jksq.5)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from beadhive import run as run_mod
from beadhive import state_stream, state_stream_polling
from beadhive.state_stream_process import StreamProcessScope

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="process groups use Linux /proc")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return False


def _wait_for_pid(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text().strip()
        except FileNotFoundError:
            value = ""
        if value:
            return int(value)
        time.sleep(0.02)
    raise AssertionError(f"process did not publish its descendant pid to {path}")


def _wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _forking_script(pid_path: Path, *, parent_exits: bool = False) -> str:
    return (
        "import os,time\n"
        "child=os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        f"open({str(pid_path)!r}, 'w').write(str(child))\n"
        + ("os._exit(0)\n" if parent_exits else "time.sleep(300)\n")
    )


def _successful_orphan_script(pid_path: Path, parent_source: str) -> str:
    """Exit the leader successfully after an output-detached child remains in its group."""

    return (
        "import os,time\n"
        "child=os.fork()\n"
        "if child == 0:\n"
        "    os.close(0)\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        f"open({str(pid_path)!r}, 'w').write(str(child))\n"
        f"{parent_source}\n"
    )


def test_timeout_reaps_a_real_grandchild(tmp_path):
    pid_path = tmp_path / "grandchild.pid"
    with StreamProcessScope(timeout=0.2, term_grace=0.1) as processes:
        with pytest.raises(run_mod.ChildTimeout):
            processes.run([sys.executable, "-c", _forking_script(pid_path)])

    grandchild = _wait_for_pid(pid_path)
    assert _wait_gone(grandchild), "the backend grandchild outlived the export timeout"


def test_polling_provider_routes_explicit_backend_argv_through_scope(tmp_path, monkeypatch):
    hive = tmp_path / "hive"
    hive.mkdir()
    monkeypatch.setattr(state_stream_polling.registry, "resolve_hive", lambda _cfg, _slug: {})
    monkeypatch.setattr(state_stream_polling.registry, "hive_dir", lambda _entry: hive)

    class CommandBackend:
        name = "controlled"

        def export_jsonl(self, _cwd, _out_path, *, env=None):  # pragma: no cover - forbidden arm
            raise AssertionError("opaque export bypassed the stream process owner")

        def stream_export_command(self, _cwd, out_path):
            record = {
                "id": "bh-1",
                "title": "owned export",
                "updated_at": "2026-08-24T00:00:00Z",
                "dependencies": [],
            }
            script = (
                "import pathlib; "
                f"pathlib.Path({str(out_path)!r}).write_text({json.dumps(record)!r} + '\\n')"
            )
            return [sys.executable, "-c", script]

        def stream_gate_list_command(self, _cwd):
            return [sys.executable, "-c", "print('[]')"]

    with StreamProcessScope(timeout=5, term_grace=0.1) as processes:
        provider = state_stream_polling.PollingStateStreamProvider(
            {}, backend=CommandBackend(), process_scope=processes
        )
        snapshot = provider.refresh(state_stream.StreamRequest("hive", hive="beadhive"))

    assert [issue.id for issue in snapshot.issues] == ["bh-1"]
    assert snapshot.partial is False


def test_successful_export_and_gate_groups_are_reaped_at_scope_exit(tmp_path, monkeypatch):
    """Native exit must not discard groups retained by either concrete backend command path."""

    hive = tmp_path / "hive"
    hive.mkdir()
    export_descendant = tmp_path / "export-descendant.pid"
    gate_descendant = tmp_path / "gate-descendant.pid"
    monkeypatch.setattr(state_stream_polling.registry, "resolve_hive", lambda _cfg, _slug: {})
    monkeypatch.setattr(state_stream_polling.registry, "hive_dir", lambda _entry: hive)

    class SuccessfulOrphanBackend:
        name = "successful-orphan"

        def stream_export_command(self, _cwd, out_path):
            record = {
                "id": "bh-1",
                "title": "owned export",
                "updated_at": "2026-08-24T00:00:00Z",
                "dependencies": [],
            }
            parent_source = (
                "import pathlib; "
                f"pathlib.Path({str(out_path)!r}).write_text({json.dumps(record)!r} + '\\n')"
            )
            return [
                sys.executable,
                "-c",
                _successful_orphan_script(export_descendant, parent_source),
            ]

        def stream_gate_list_command(self, _cwd):
            return [
                sys.executable,
                "-c",
                _successful_orphan_script(gate_descendant, "print('[]')"),
            ]

    with StreamProcessScope(timeout=5, term_grace=0.1) as processes:
        provider = state_stream_polling.PollingStateStreamProvider(
            {}, backend=SuccessfulOrphanBackend(), process_scope=processes
        )
        snapshot = provider.refresh(state_stream.StreamRequest("hive", hive="beadhive"))
        export_pid = _wait_for_pid(export_descendant)
        gate_pid = _wait_for_pid(gate_descendant)
        assert snapshot.issues[0].id == "bh-1"
        assert _alive(export_pid)
        assert _alive(gate_pid)

    assert _wait_gone(export_pid), "successful export left its detached descendant alive"
    assert _wait_gone(gate_pid), "successful gate list left its detached descendant alive"


def test_scope_fails_closed_for_an_opaque_backend(tmp_path):
    class OpaqueBackend:
        name = "opaque"

    with StreamProcessScope() as processes:
        with pytest.raises(TypeError, match="does not expose a supervised stream export command"):
            processes.export_jsonl(OpaqueBackend(), tmp_path, tmp_path / "out.jsonl")


def test_closed_scope_refuses_to_spawn_a_late_backend_process() -> None:
    processes = StreamProcessScope(timeout=1, term_grace=0.01)
    processes.close()
    with pytest.raises(RuntimeError, match="process scope is closed"):
        processes.run([sys.executable, "-c", "raise SystemExit(0)"])


def test_timeout_reaps_descendant_after_direct_child_already_exited(tmp_path):
    """The group id must be remembered at spawn; getpgid(leader) is already impossible here."""

    pid_path = tmp_path / "grandchild.pid"
    with StreamProcessScope(timeout=0.2, term_grace=0.1) as processes:
        with pytest.raises(run_mod.ChildTimeout):
            processes.run([sys.executable, "-c", _forking_script(pid_path, parent_exits=True)])

    grandchild = _wait_for_pid(pid_path)
    assert _wait_gone(grandchild), "the pipe-holding descendant survived its dead group leader"


def test_broken_output_pipe_finalizer_cancels_an_active_backend_tree(tmp_path):
    pid_path = tmp_path / "grandchild.pid"
    processes = StreamProcessScope(timeout=30, term_grace=0.1)
    outcome: list[object] = []

    def run_backend() -> None:
        try:
            outcome.append(processes.run([sys.executable, "-c", _forking_script(pid_path)]))
        except BaseException as exc:  # pragma: no cover - diagnostic if ownership regresses
            outcome.append(exc)

    worker = threading.Thread(target=run_backend)
    with pytest.raises(BrokenPipeError):
        with processes:
            worker.start()
            grandchild = _wait_for_pid(pid_path)
            raise BrokenPipeError("consumer closed stdout")

    worker.join(timeout=10)
    assert not worker.is_alive(), "backend communicate remained blocked after stream finalization"
    assert _wait_gone(grandchild), "the backend grandchild outlived a broken output pipe"
    assert outcome, "backend worker never completed"


@pytest.mark.parametrize(
    ("cancel_signal", "expected_status"),
    [(signal.SIGTERM, -signal.SIGTERM), (signal.SIGINT, -signal.SIGINT)],
)
def test_signal_cancellation_reaps_tree_and_preserves_nonzero_exit(
    tmp_path, cancel_signal, expected_status
):
    grandchild_path = tmp_path / "grandchild.pid"
    backend_path = tmp_path / "backend.py"
    backend_path.write_text(_forking_script(grandchild_path))
    parent_path = tmp_path / "stream_parent.py"
    source_root = str(Path(run_mod.__file__).parent.parent)
    parent_path.write_text(
        "import sys\n"
        f"sys.path.insert(0, {source_root!r})\n"
        "from beadhive.state_stream_process import StreamProcessScope\n"
        "with StreamProcessScope(timeout=30, term_grace=0.1) as processes:\n"
        f"    processes.run([{sys.executable!r}, {str(backend_path)!r}])\n"
    )
    parent = subprocess.Popen(
        [sys.executable, str(parent_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        grandchild = _wait_for_pid(grandchild_path)
        parent.send_signal(cancel_signal)
        parent.wait(timeout=10)
        assert parent.returncode == expected_status
        assert _wait_gone(grandchild), (
            f"backend grandchild outlived parent cancellation by {cancel_signal.name}"
        )
    finally:
        if parent.poll() is None:
            parent.kill()
