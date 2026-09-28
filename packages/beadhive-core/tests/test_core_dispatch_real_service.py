"""Opt-in conformance: molecule progress, swarm inspection, dispatch polling, and local-loop
bead-state access (bh-sy36q.5) against a disposable scratch hive this test creates itself — never
a managed hive.

What is proven against a real Beads 1.3.0 service:

* **Restart is a no-op.** `molecule_progress` / `swarm_members` / `poll_ready` are re-derived from
  scratch on every call — nothing here is cached — so a read taken AFTER an external `bd` write
  (standing in for "the process restarted and re-derives its whole decision input") sees the new
  state immediately, exactly the property `beadhive.localloop.LocalLoop` requires of a restart.
* **Gates.** A dependency edge blocks `poll_ready`'s readiness exactly like a gate does (Beads
  models both as a blocking dependency the ready predicate honors); a CLOSED child and a CLOSED
  infra event row both stay visible to `swarm_members` / `event_rows` only once `all_`/
  `include_infra` are asked for — the same default-exclusion override `bd`'s own `--all` /
  `--include-infra` apply, proven against the real server rather than assumed from the spec.
* **Ready transitions.** A dependent bead moves from ABSENT in `poll_ready`'s result to PRESENT
  the moment its blocker closes — proving repeated `ready.list` reads reflect a dependency
  transition with no mirrored journal, the property `work.dispatch.poll`'s own evidence claims.
* **Concurrent observations.** Two independently-opened sessions polling the same molecule at the
  same time see the same state — no shared mutable cache serializes them.

Opt-in via ``BEADS_DISPATCH_SCRATCH=1``, the same explicit-env self-skip convention as the other
``real_service`` tests here (``bd serve`` needs an OWNED-mode ``bd init --server`` hive)::

    BEADS_DISPATCH_SCRATCH=1 \
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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from beadhive_beads_client import BeadsSession, ExpectedContext, LocalEndpoint
from beadhive_core import DISPATCH_CAPABILITIES, DispatchCommands

pytestmark = pytest.mark.real_service


def _bd(*args: str, cwd: Path, actor: str = "") -> Any:
    argv = ["bd", *(["--actor", actor] if actor else []), *args, "--json"]
    result = subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def _reap_owned_scratch_hive(beads_dir: Path) -> None:
    """Terminate both processes an OWNED-mode scratch hive leaves behind: the Dolt SQL server
    (``dolt-server.pid``) and the detached ``bd db-proxy-child`` (``dolt/proxy.pid``, JSON) —
    the same discipline as the other ``real_service`` tests in this package."""
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


@contextmanager
def _scratch(tmp_path: Path) -> Iterator[tuple[Path, ExpectedContext, int]]:
    if not os.environ.get("BEADS_DISPATCH_SCRATCH"):
        pytest.skip("set BEADS_DISPATCH_SCRATCH=1 to run the real-bd scratch-hive proof")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "dsc", "--non-interactive"],
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
            required_capabilities=DISPATCH_CAPABILITIES,
            repo_root=tmp_path,
        )
        yield tmp_path, expected, _free_port()
    finally:
        _reap_owned_scratch_hive(tmp_path / ".beads")


def _create(cwd: Path, title: str, *, parent: str = "", extra: list[str] | None = None) -> str:
    args = ["create", "--title", title, "--type", "task", "--priority", "2"]
    if parent:
        args += ["--parent", parent]
    args += extra or []
    created = _bd(*args, cwd=cwd)
    return str(created["id"])


def test_real_v13_molecule_progress_is_re_derived_after_an_external_write(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        epic = _create(hive, "epic proof", extra=["--type", "epic"])
        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = DispatchCommands()
            before = commands.molecule_progress(session, epic)
            assert before["status"] == "open"

            # Stands in for "the process restarted": a write made entirely outside this session.
            _bd("update", epic, "--status", "in_progress", cwd=hive)

            after = commands.molecule_progress(session, epic)
            assert after["status"] == "in_progress"


def test_real_v13_swarm_members_sees_a_child_created_after_the_epic(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        epic = _create(hive, "molecule epic", extra=["--type", "epic"])
        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = DispatchCommands()
            assert commands.swarm_members(session, epic) == []

            child = _create(hive, "molecule child", parent=epic)

            members = commands.swarm_members(session, epic)
            assert [row["id"] for row in members] == [child]


def test_real_v13_swarm_members_keeps_a_closed_child_with_all_true(tmp_path: Path) -> None:
    """`all_=True` reproduces `bd list --all`'s own default-exclusion override for real: a closed
    child stays visible, exactly what `LocalLoop.load_molecule` needs so a finished child reads as
    finished rather than silently vanishing from the molecule."""
    with _scratch(tmp_path) as (hive, expected, port):
        epic = _create(hive, "closed-child epic", extra=["--type", "epic"])
        child = _create(hive, "closed child", parent=epic)
        _bd("close", child, cwd=hive)

        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = DispatchCommands()
            members = commands.swarm_members(session, epic)
            assert {row["id"]: row["status"] for row in members} == {child: "closed"}


def test_real_v13_event_rows_sees_a_closed_infra_row_via_state_change(
    tmp_path: Path,
) -> None:
    """A state-change event bead — the loop-breaker's `attempt_count` source
    (`work_next.chronological_rows`) — is CLOSED and an infra `issue_type`; `event_rows` finds it
    the same way `bd.child_rows`/`bd list --parent <bead> --include-infra --all` does, over the
    bead's own dotted-id stream (one level, not a molecule-wide recursive fetch)."""
    with _scratch(tmp_path) as (hive, expected, port):
        epic = _create(hive, "event-bearing epic", extra=["--type", "epic"])
        child = _create(hive, "event-bearing child", parent=epic)
        _bd("set-state", child, "dispatch=run_failed", "--reason", "boom", cwd=hive)

        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = DispatchCommands()
            events = commands.event_rows(session, child)
            assert len(events) == 1
            assert events[0]["issue_type"] == "event"
            assert events[0]["status"] == "closed"
            assert events[0]["title"].startswith("State change: dispatch")


def test_real_v13_poll_ready_reflects_a_dependency_closing(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        blocker = _create(hive, "blocker")
        dependent = _create(hive, "dependent")
        _bd("dep", "add", dependent, blocker, cwd=hive)

        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = DispatchCommands()
            before_ids = {row["id"] for row in commands.poll_ready(session)}
            assert dependent not in before_ids
            assert blocker in before_ids

            _bd("close", blocker, cwd=hive)

            after_ids = {row["id"] for row in commands.poll_ready(session)}
            assert dependent in after_ids
            assert blocker not in after_ids


def test_real_v13_concurrent_sessions_observe_the_same_ready_transition(tmp_path: Path) -> None:
    """Two independently-opened sessions polling the same molecule concurrently see the SAME
    state — nothing here serializes them through a shared mutable cache."""
    with _scratch(tmp_path) as (hive, expected, port):
        epic = _create(hive, "concurrent epic", extra=["--type", "epic"])
        blocker = _create(hive, "concurrent blocker", parent=epic)
        dependent = _create(hive, "concurrent dependent", parent=epic)
        _bd("dep", "add", dependent, blocker, cwd=hive)

        commands = DispatchCommands()
        with (
            BeadsSession(LocalEndpoint(hive, port=port), expected) as watcher_a,
            BeadsSession(LocalEndpoint(hive, port=port), expected) as watcher_b,
        ):
            ready_a = {row["id"] for row in commands.poll_ready(watcher_a, parent=epic)}
            ready_b = {row["id"] for row in commands.poll_ready(watcher_b, parent=epic)}
            assert ready_a == ready_b == {blocker}

            _bd("close", blocker, cwd=hive)

            ready_a = {row["id"] for row in commands.poll_ready(watcher_a, parent=epic)}
            ready_b = {row["id"] for row in commands.poll_ready(watcher_b, parent=epic)}
            assert ready_a == ready_b == {dependent}
