"""Opt-in conformance: the lifecycle cohort's HTTP mutation, its conflict, and its mixed-route
read-after-write, against a disposable scratch hive this test creates itself — never a managed
hive.

What is proven against a real Beads 1.3.0 service:

* ``assign`` is a guarded ``work.issue.update``: the write is attributed to the orchestrator and
  a bead that changed after the guard read (here: a competing real ``bd update --claim``) is
  refused with ``409 precondition_failed`` and left untouched — the conflict the legacy
  ``bd assign`` silently overwrote.
* ``claim`` / ``abandon`` take the renewable lease through the real ``bd`` CLI route and verify it
  by re-reading over HTTP: the service reports the CLI write immediately (no stale read that would
  make a won claim look lost, or a released one look held). A release by an actor other than the
  holder is refused by bd's anti-steal fence and ``abandon`` reports it rather than a ✓.

Opt-in via ``BEADS_LIFECYCLE_SCRATCH=1``, the same explicit-env self-skip convention as the other
``real_service`` tests here (``bd serve`` needs an OWNED-mode ``bd init --server`` hive)::

    BEADS_LIFECYCLE_SCRATCH=1 \
    uv run --locked --all-packages pytest packages/beadhive-core/tests -m real_service
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from beadhive_beads_client import BeadsSession, ExpectedContext, LocalEndpoint
from beadhive_core import (
    LIFECYCLE_CAPABILITIES,
    Gate,
    LifecycleCommands,
    LifecycleFailed,
    Provisioned,
    SessionIssues,
    WriteFailed,
)

pytestmark = pytest.mark.real_service


def _bd(*args: str, cwd: Path, actor: str = "") -> Any:
    argv = ["bd", *(["--actor", actor] if actor else []), *args, "--json"]
    result = subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def _row(cwd: Path, bead: str) -> dict[str, Any]:
    shown = _bd("show", bead, cwd=cwd)
    return shown[0] if isinstance(shown, list) else shown


def _reap_owned_scratch_hive(beads_dir: Path) -> None:
    """Terminate both processes an OWNED-mode scratch hive leaves behind: the Dolt SQL server
    (``dolt-server.pid``) and the detached ``bd db-proxy-child`` (``dolt/proxy.pid``, JSON) —
    the same discipline as ``tests/test_work_queue.py``'s helper, kept local to this package."""
    for pid_file, is_json in (
        (beads_dir / "dolt-server.pid", False),
        (beads_dir / "dolt" / "proxy.pid", True),
    ):
        if not pid_file.is_file():
            continue
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError, KeyError):
            text = pid_file.read_text().strip()
            pid = int(json.loads(text)["pid"]) if is_json else int(text)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


class RealLeases:
    """The ``work.lease.*`` CLI routes, run for real (the shell's ``CliLeases`` argv)."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    def _run(self, args: list[str], actor: str) -> None:
        result = subprocess.run(
            ["bd", "--actor", actor, *args], cwd=self._cwd, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise WriteFailed(result.returncode, result.stderr.strip())

    def acquire(self, bead: str, *, actor: str) -> None:
        self._run(["update", bead, "--claim"], actor)

    def release(self, bead: str, *, actor: str) -> None:
        self._run(["update", bead, "--status", "open", "--assignee", ""], actor)


class RealStates:
    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        subprocess.run(
            ["bd", "--actor", actor, "set-state", bead, f"{dimension}={value}", "--reason", reason],
            cwd=self._cwd,
            check=True,
            capture_output=True,
        )

    def get_state(self, bead: str, dimension: str) -> str:
        return ""


class NoGates:
    def gates_for(self, bead: str) -> list[Gate]:
        return []

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        raise AssertionError("no gate is resolved in this proof")


class FakeWorkspace:
    """Worktree effects are root capabilities, not part of this proof."""

    def refresh(self) -> None: ...

    def publish(self, actor: str, message: str) -> None: ...

    def dispatch_gate(self, issue: Mapping[str, Any], bead: str) -> None: ...

    def open_container(self, bead: str) -> None: ...

    def provision(self, bead: str, kind: str) -> Provisioned:
        return Provisioned(Path(f"/scratch/{bead}"))

    def batch_checkout(self, group: str) -> Provisioned | None:
        return None

    def stamp(self, checkout: Provisioned, actor: str) -> None: ...

    def record_claim(self, bead: str, actor: str, checkout: Provisioned) -> None: ...

    def remove(self, bead: str) -> bool:
        return False


class Lines:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, text: str, *, error: bool = False) -> None:
        self.lines.append(text)

    def feedback(self, bead: str) -> None: ...


@contextmanager
def _scratch(tmp_path: Path) -> Iterator[tuple[Path, ExpectedContext, int]]:
    if not os.environ.get("BEADS_LIFECYCLE_SCRATCH"):
        pytest.skip("set BEADS_LIFECYCLE_SCRATCH=1 to run the real-bd scratch-hive proof")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "lsc", "--non-interactive"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        context = _bd("context", cwd=tmp_path)
        expected = ExpectedContext(
            context["project_id"],
            context["database"],
            required_capabilities=LIFECYCLE_CAPABILITIES,
            repo_root=tmp_path,
        )
        yield tmp_path, expected, _free_port()
    finally:
        _reap_owned_scratch_hive(tmp_path / ".beads")


def _commands(session: BeadsSession, cwd: Path, out: Lines) -> LifecycleCommands:
    states = RealStates(cwd)
    return LifecycleCommands(
        SessionIssues(session),
        RealLeases(cwd),
        states,
        states,
        NoGates(),
        FakeWorkspace(),
        out,
    )


def _create(cwd: Path, title: str) -> str:
    created = _bd("create", "--title", title, "--type", "task", "--priority", "2", cwd=cwd)
    return str(created["id"])


def test_real_v13_assign_is_attributed_and_refuses_a_stale_read(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        assigned, contested = _create(hive, "assign proof"), _create(hive, "conflict proof")
        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            out = Lines()
            _commands(session, hive, out).assign(assigned, "dev/carol", "disp/lead")
            assert _row(hive, assigned)["assignee"] == "dev/carol"
            assert _row(hive, assigned)["status"] == "open"  # assign never claims

            issues = SessionIssues(session)
            stale = issues.get(contested)
            assert stale is not None
            # A competing real claim lands between the guard read and the assign write.
            RealLeases(hive).acquire(contested, actor="dev/racer")
            with pytest.raises(WriteFailed) as refused:
                issues.assign(contested, "dev/carol", actor="disp/lead", read=stale)
            assert "changed since it was read" in refused.value.detail
            assert _row(hive, contested)["assignee"] == "dev/racer"

        events = _bd("history", assigned, "--events", cwd=hive)
        rows = events if isinstance(events, list) else []
        assert any(e.get("actor") == "disp/lead" for e in rows), rows


def test_real_v13_cli_lease_is_verified_by_the_http_re_read(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        bead, held = _create(hive, "claim proof"), _create(hive, "held proof")
        RealLeases(hive).acquire(held, actor="dev/bob")
        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            out = Lines()
            commands = _commands(session, hive, out)

            outcome = commands.claim(bead, "dev/alice")
            assert outcome.disposition == "claimed"
            assert outcome.issue["assignee"] == "dev/alice"
            assert outcome.issue["status"] == "in_progress"

            with pytest.raises(LifecycleFailed):
                commands.claim(held, "dev/alice")
            assert "refusing to steal" in out.lines[-1]

            commands.abandon(bead, "dev/alice")
            assert out.lines[-1] == f"✓ abandoned {bead}; worktree kept"

            # bd 1.3's anti-steal fence refuses a foreign actor's release; abandon reports the
            # route failure instead of an unqualified ✓ (the bead is still held).
            with pytest.raises(LifecycleFailed):
                commands.abandon(held, "disp/lead")
            assert out.lines[-1] == f"⚠ abandoned {held} with bd errors (see above)"
        assert _row(hive, held)["assignee"] == "dev/bob"
        final = _row(hive, bead)
        assert (final["status"], final.get("assignee") or "") == ("open", "")
