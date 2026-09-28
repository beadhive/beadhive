"""Opt-in: `bh work next --epic`'s API route (bh-7ip8t) against a disposable scratch hive this test
creates itself — never a managed hive.

Two properties are proven against a real Beads 1.3.0 ``bd serve``, not a transport fixture,
because this is a WRITE (claim) path:

* **Candidate-set parity with the CLI-compatibility route.** The route the shell falls back to when
  no capable session opens (``beadhive.work_dispatch._next_via_cli``) scopes ``bd ready --limit 0``
  to the epic plus ``bd list --parent <epic> --limit 0 --include-infra --all`` narrowed to the
  parent edge (``beadhive_bd_cli.reads.children``). :meth:`QueueCommands.molecule_members` and
  :meth:`QueueCommands.epic_candidates` must return the SAME sets over HTTP, for a molecule holding
  every awkward member kind at once: an infrastructure-type (event) member, open and closed; a
  closed task; a nested sub-epic with its own ready leaf; a blocked member; a detached dotted-id
  prefix match that carries no parent edge; and ready work outside the molecule. The CLI side is
  computed here by running the same ``bd`` argv that route runs, with an independent edge check —
  this package does not import the shell or the ``bd`` CLI package.

  Nested means ONE level, deliberately (bh-sh6yt): the sub-epic is a member, its own leaf is not —
  a nested epic is dispatched as a bead and driven by its own loop, and an outer loop that recursed
  would claim a grandchild out from under the loop that owns it. Both routes agree on that, which
  is what this asserts.
* **Exclusivity under real contention.** Several real OS threads race
  :meth:`QueueCommands.claim_next_in_epic` over one molecule: exactly one wins a single ready
  member, and with two ready members exactly two distinct winners emerge — never the same bead
  twice, never a bead outside the molecule — and the store agrees with every believed winner.

Opt-in via ``BEADS_EPIC_CLAIM_SCRATCH=1``, the same explicit-env self-skip convention as the other
``real_service`` tests here (``bd serve`` needs an OWNED-mode ``bd init --server`` hive)::

    BEADS_EPIC_CLAIM_SCRATCH=1 \
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from beadhive_beads_client import BeadsSession, ExpectedContext, LocalEndpoint, RemoteEndpoint
from beadhive_core import QUEUE_CAPABILITIES, QueueCommands, eligible

pytestmark = pytest.mark.real_service


def _bd(*args: str, cwd: Path) -> Any:
    result = subprocess.run(
        ["bd", *args, "--json"], cwd=cwd, check=True, capture_output=True, text=True
    )
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
    if not os.environ.get("BEADS_EPIC_CLAIM_SCRATCH"):
        pytest.skip("set BEADS_EPIC_CLAIM_SCRATCH=1 to run the real-bd scratch-hive proof")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "esc", "--non-interactive"],
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
            required_capabilities=QUEUE_CAPABILITIES,
            repo_root=tmp_path,
        )
        yield tmp_path, expected, _free_port()
    finally:
        _reap_owned_scratch_hive(tmp_path / ".beads")


def _create(
    cwd: Path, title: str, *, type_: str = "task", parent: str = "", priority: int = 2
) -> str:
    args = ["create", "--title", title, "--type", type_, "--priority", str(priority)]
    if parent:
        args += ["--parent", parent]
    return str(_bd(*args, cwd=cwd)["id"])


def _cli_edge(row: Mapping[str, Any], parent: str) -> bool:
    """The parent edge in either representation bd emits (top-level ``parent`` or a
    ``parent-child`` dependency) — written out independently here, not imported, so the parity
    check does not compare the core's narrowing against itself."""
    if str(row.get("parent") or "") == parent:
        return True
    return any(
        isinstance(dep, Mapping)
        and dep.get("type") == "parent-child"
        and str(dep.get("depends_on_id") or "") == parent
        for dep in row.get("dependencies") or []
    )


def _cli_members(cwd: Path, epic: str) -> list[dict]:
    """The CLI-compatibility route's membership read, argv for argv."""
    rows = _bd("list", "--parent", epic, "--limit", "0", "--include-infra", "--all", cwd=cwd)
    return [row for row in rows or [] if isinstance(row, dict) and _cli_edge(row, epic)]


def _cli_candidates(cwd: Path, epic: str) -> list[str]:
    """The CLI-compatibility route's scoped ready rows: `bd ready --limit 0` filtered to the epic
    plus its members, in bd's ready order."""
    members = {str(row.get("id") or "") for row in _cli_members(cwd, epic)} | {epic}
    ready = _bd("ready", "--limit", "0", cwd=cwd) or []
    return [str(row["id"]) for row in ready if str(row.get("id") or "") in members]


def _molecule(hive: Path) -> dict[str, str]:
    """One molecule holding every member kind the parity claim covers, plus outside work."""
    ids: dict[str, str] = {}
    ids["outsider"] = _create(hive, "ready work outside the molecule", priority=0)
    ids["epic"] = _create(hive, "parity epic", type_="epic")
    ids["ready"] = _create(hive, "ready member", parent=ids["epic"], priority=1)
    ids["closed"] = _create(hive, "closed member", parent=ids["epic"])
    _bd("close", ids["closed"], cwd=hive)
    ids["infra_open"] = _create(hive, "open infra member", type_="event", parent=ids["epic"])
    ids["infra_closed"] = _create(hive, "closed infra member", type_="event", parent=ids["epic"])
    _bd("close", ids["infra_closed"], cwd=hive)
    ids["sub_epic"] = _create(hive, "nested sub-epic", type_="epic", parent=ids["epic"])
    ids["grandchild"] = _create(hive, "ready leaf of the sub-epic", parent=ids["sub_epic"])
    ids["blocked"] = _create(hive, "blocked member", parent=ids["epic"])
    _bd("dep", "add", ids["blocked"], ids["outsider"], cwd=hive)
    # A dotted-id prefix match with no parent edge: bd's raw `--parent` read still returns it.
    ids["detached"] = _create(hive, "detached member", parent=ids["epic"])
    _bd("update", ids["detached"], "--parent", "", cwd=hive)
    return ids


def test_real_v13_epic_candidates_match_the_cli_route(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        ids = _molecule(hive)
        epic = ids["epic"]
        cli_members = {str(row["id"]) for row in _cli_members(hive, epic)}
        cli_candidates = _cli_candidates(hive, epic)

        with BeadsSession(LocalEndpoint(hive, port=port), expected) as session:
            commands = QueueCommands()
            api_members = {str(row["id"]) for row in commands.molecule_members(session, epic)}
            api_rows = commands.epic_candidates(session, epic)
        api_candidates = [str(row["id"]) for row in api_rows]

        # Membership: identical sets, and they really do contain every awkward member kind.
        assert api_members == cli_members
        for kind in ("ready", "closed", "infra_open", "infra_closed", "sub_epic", "blocked"):
            assert ids[kind] in api_members, f"{kind} member missing from membership"
        for kind in ("grandchild", "detached", "outsider"):
            assert ids[kind] not in api_members, f"{kind} must not be a member"

        # Candidates: the same ready rows, in the same order.
        assert api_candidates == cli_candidates
        assert ids["ready"] in api_candidates
        for kind in ("closed", "infra_closed", "blocked", "grandchild", "detached", "outsider"):
            assert ids[kind] not in api_candidates, f"{kind} must never be a candidate"
        # What either route would actually try: the shared eligibility policy drops the open
        # infra row; the ready task member stays (epic rows are left to the seat check).
        tried = eligible(api_rows, "dev/probe")
        assert ids["ready"] in tried
        assert ids["infra_open"] not in tried


def _developer_seat(actor: str, row: Mapping[str, Any]) -> str | None:
    """The shell's seat rule for a declared ``dev/`` racer: an epic row is refused before any
    write (``work_dispatch._next_seat_actor``), anything else is claimed as ``actor``."""
    return None if row.get("issue_type") == "epic" else actor


def _race(url: str, expected: ExpectedContext, epic: str, racers: int) -> list[Any]:
    commands = QueueCommands()
    barrier = Barrier(racers)

    def contest(actor: str) -> Any:
        with BeadsSession(RemoteEndpoint(url), expected) as claimant:
            barrier.wait(timeout=10)  # every racer arrives before any of them claims
            return commands.claim_next_in_epic(
                claimant, epic, actor, seat_actor=lambda row: _developer_seat(actor, row)
            )

    with ThreadPoolExecutor(max_workers=racers) as pool:
        return list(pool.map(contest, [f"dev/racer-{i}" for i in range(racers)]))


def _held(hive: Path, bead: str) -> dict:
    shown = _bd("show", bead, cwd=hive)
    return shown[0] if isinstance(shown, list) else shown


def test_real_v13_epic_claim_picks_exactly_one_winner_under_contention(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        outsider = _create(hive, "ready work outside the molecule", priority=0)
        epic = _create(hive, "contended epic", type_="epic")
        member = _create(hive, "the one ready member", parent=epic)

        with BeadsSession(LocalEndpoint(hive, port=port), expected):
            outcomes = _race(f"http://127.0.0.1:{port}", expected, epic, racers=4)

        won = [outcome for outcome in outcomes if outcome.claimed]
        assert len(won) == 1, f"exactly one racer may win the only ready member, got {outcomes}"
        assert won[0].claimed == member
        final = _held(hive, member)
        assert (final["assignee"], final["status"]) == (won[0].claim_actor, "in_progress")
        assert _held(hive, outsider)["status"] == "open", "nothing outside the molecule is taken"


def test_real_v13_epic_claim_never_hands_one_member_to_two_racers(tmp_path: Path) -> None:
    with _scratch(tmp_path) as (hive, expected, port):
        outsider = _create(hive, "ready work outside the molecule", priority=0)
        epic = _create(hive, "two-member epic", type_="epic")
        first = _create(hive, "first ready member", parent=epic, priority=1)
        second = _create(hive, "second ready member", parent=epic, priority=2)

        with BeadsSession(LocalEndpoint(hive, port=port), expected):
            outcomes = _race(f"http://127.0.0.1:{port}", expected, epic, racers=6)

        won = [outcome for outcome in outcomes if outcome.claimed]
        assert sorted(outcome.claimed for outcome in won) == sorted([first, second]), outcomes
        for outcome in won:
            final = _held(hive, outcome.claimed)
            assert (final["assignee"], final["status"]) == (outcome.claim_actor, "in_progress")
        assert _held(hive, outsider)["status"] == "open", "nothing outside the molecule is taken"
