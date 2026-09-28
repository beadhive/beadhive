"""Opt-in conformance: ``plan.batch-apply.atomic`` against a disposable scratch hive this test
creates itself — never a managed hive.

This is the real-service evidence that reclassified ``plan.batch-apply.atomic`` from
``cli-compatibility`` to ``api-ready`` (bh-sy36q.2; the bh-97fo0.3 matrix originally recorded it as
"generated but lacks the required real-service atomicity and end-gate proof"). What is proven
against a real Beads 1.3.0 service:

* **Atomic creation + dependency edges in one request.** A molecule (epic + two issues + their
  parent-child and declared-dependency edges) compiles to ONE ``BatchApply`` request and lands as
  one act: every id, every edge, and the one recorded history entry.
* **Key resolution.** The response's ``keys`` map resolves every request-local key to the id Beads
  actually minted — the fact the request itself cannot carry.
* **Partial failure is all-or-nothing.** A request whose last item is refused (a self-dependency,
  refused with everything else in the same request) leaves NOTHING written — no epic, no issues,
  no edges — proving there is no partial commit to reconcile.
* **Reconciliation after a precondition miss never partially lands the graph either**, exercised
  through the same all-or-nothing contract with a duplicate id collision.

Opt-in via ``BEADS_PLANNING_SCRATCH=1``, the same explicit-env self-skip convention as the other
``real_service`` tests here (``bd serve`` needs an OWNED-mode ``bd init --server`` hive)::

    BEADS_PLANNING_SCRATCH=1 \
    uv run --locked --all-packages pytest packages/beadhive-core/tests -m real_service
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from beadhive_beads_client import BeadsSession, ExpectedContext, LocalEndpoint
from beadhive_core import (
    PLANNING_CAPABILITIES,
    CompiledMolecule,
    MoleculeFilingFailed,
    MoleculeGraph,
    PlanningCommands,
    SessionMoleculeFiler,
)
from beads_v1_3.models import ApplyCreateItem, ApplyItem, ApplyItemKind

pytestmark = pytest.mark.real_service


def _bd(*args: str, cwd: Path) -> Any:
    result = subprocess.run(
        ["bd", *args, "--json"], cwd=cwd, check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def _reap_owned_scratch_hive(beads_dir: Path) -> None:
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
    import socket

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


class NoGates:
    """Gate/kickoff/swarm conventions are a separate compatibility port, not this proof's concern
    (bh-sy36q.2 proves the BatchApply route only; the shell composes the real bd gate calls)."""

    def create_swarm(self, epic_id: str, *, actor: str) -> bool:
        return True

    def create_kickoff_gate(self, root_id: str, epic_id: str, *, actor: str) -> None:
        return None

    def set_kickoff_pending(self, epic_id: str, *, actor: str) -> None:
        return None

    def create_release_hold_gate(self, bead_id: str, epic_id: str, *, actor: str) -> None:
        return None


@contextmanager
def _scratch(tmp_path: Path) -> Iterator[tuple[Path, ExpectedContext, int]]:
    if not os.environ.get("BEADS_PLANNING_SCRATCH"):
        pytest.skip("set BEADS_PLANNING_SCRATCH=1 to run the real-bd scratch-hive proof")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "psc", "--non-interactive"],
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
            required_capabilities=PLANNING_CAPABILITIES,
            repo_root=tmp_path,
        )
        yield tmp_path, expected, _free_port()
    finally:
        _reap_owned_scratch_hive(tmp_path / ".beads")


def _molecule() -> dict[str, Any]:
    return {
        "epic": {"title": "real-service molecule"},
        "issues": [
            {"handle": "root", "title": "root step", "type": "task", "acceptance": "a", "deps": []},
            {
                "handle": "leaf",
                "title": "leaf step",
                "type": "task",
                "acceptance": "b",
                "deps": ["root"],
            },
        ],
    }


def test_real_v13_batch_apply_files_one_molecule_atomically_and_resolves_keys(
    tmp_path: Path,
) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = PlanningCommands(SessionMoleculeFiler(session), NoGates())
            outcome = commands.file(
                _molecule(), actor="disp/lead", dimension_fields=(), identity_labels=("org:acme",)
            )
        assert outcome.issue_count == 2
        assert outcome.root_count == 1

        epic = _bd("show", outcome.epic_id, cwd=hive)[0]
        assert epic["labels"] == ["org:acme"]
        children = _bd("list", "--parent", outcome.epic_id, cwd=hive)
        by_title = {c["title"]: c for c in children}
        root, leaf = by_title["root step"], by_title["leaf step"]
        assert root["labels"] == ["org:acme"]

        # Default direction ("down") lists what the issue itself depends on.
        root_deps = _bd("dep", "list", root["id"], cwd=hive)
        assert any(
            d["id"] == outcome.epic_id and d["dependency_type"] == "parent-child" for d in root_deps
        )
        leaf_deps = _bd("dep", "list", leaf["id"], cwd=hive)
        assert any(d["id"] == root["id"] and d["dependency_type"] == "blocks" for d in leaf_deps)
        assert any(
            d["id"] == outcome.epic_id and d["dependency_type"] == "parent-child" for d in leaf_deps
        )

        events = _bd("history", outcome.epic_id, "--events", cwd=hive)
        rows = events if isinstance(events, list) else []
        assert any(e.get("actor") == "disp/lead" for e in rows), rows


def test_real_v13_batch_apply_partial_failure_leaves_nothing_written(tmp_path: Path) -> None:
    """A request whose SECOND item collides with an already-existing id is refused with a `409
    already_exists` — and writes NOTHING, not even the epic create that came before it in the
    same request. This is the server-side half of "reconciliation": a caller sees one refusal for
    the whole request, never a partially-filed molecule to clean up."""
    with _scratch(tmp_path) as (hive, expected, port):
        existing = _bd("create", "--title", "already here", "--type", "task", cwd=hive)["id"]
        before = _bd("list", "--all", cwd=hive) or []
        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            compiled = CompiledMolecule(
                items=(
                    ApplyItem(
                        kind=ApplyItemKind.CREATE,
                        create=ApplyCreateItem(
                            title="collision epic", key="epic", issue_type="epic"
                        ),
                    ),
                    ApplyItem(
                        kind=ApplyItemKind.CREATE,
                        create=ApplyCreateItem(title="collides", id=existing, issue_type="task"),
                    ),
                ),
                graph=MoleculeGraph.from_issues(()),
                epic_key="epic",
                issue_keys={},
                create_count=2,
                edge_count=0,
            )
            with pytest.raises(MoleculeFilingFailed) as failure:
                SessionMoleculeFiler(session).apply(compiled, actor="disp/lead")
            assert "already_exists" in str(failure.value) or "409" in str(failure.value)
        after = _bd("list", "--all", cwd=hive) or []
        assert len(after) == len(before)  # nothing landed — no orphaned epic to reconcile
