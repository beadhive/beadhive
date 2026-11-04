"""Integration: an intake value can return to ``untriaged`` on REAL bd (bh-tvpb6).

``intake:needs-info`` (bh-61xkc) makes the intake dimension non-terminal for the first time: every
value written so far (accepted, rejected, ...) was an end state. Before anything builds on that,
this pins what bd 1.3.x does when the dimension walks ``untriaged -> needs-info -> untriaged ->
needs-info`` on a held, bound report, on a scratch store in ``tmp_path`` (never a real hive).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from beadhive.adapters.intake import BdReportStore
from beadhive.testing.intake import SCOPE, draft
from beadhive_bd_cli import CliRoutes, SubprocessBd, err_line
from harness.beads import embedded_env, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BD = SubprocessBd(timeout=120, env=embedded_env())
_ACTOR = "dev/test"
_WALK = ("untriaged", "needs-info", "untriaged", "needs-info", "untriaged")


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("intake-reset-template")
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, SCOPE.operational_prefix)
    return root


@pytest.fixture
def scratch(template: Path, tmp_path: Path) -> Path:
    target = tmp_path / "store"
    shutil.copytree(template, target)
    return target


def _labels(scratch: Path, bead: str) -> list[str]:
    row = CliRoutes(_BD, scratch).issue_show_raw(bead)
    row = row[0] if isinstance(row, list) else row
    return sorted(row.get("labels") or [])


def _intake_labels(scratch: Path, bead: str) -> list[str]:
    return [label for label in _labels(scratch, bead) if label.startswith("intake:")]


def _ready(scratch: Path) -> tuple[str, ...]:
    rows = CliRoutes(_BD, scratch).ready_all()
    return tuple(sorted(str(r["id"]) for r in rows or ()))


def _history_hashes(scratch: Path, bead: str) -> list[str]:
    result = _BD.run(["history", bead, "--json"], scratch, capture=True)
    assert result.returncode == 0, err_line(result)
    return [str(row["CommitHash"]) for row in json.loads(result.stdout)]


def _transitions(scratch: Path, bead: str) -> list[tuple[str, str]]:
    """The ``intake`` event beads bd wrote for ``bead``, oldest first, as ``(title, first line)``.

    ``bd set-state`` records each change as a closed child event bead (``<bead>.<n>``); the
    label is only the lookup cache. Listing them shows each transition on its own."""
    result = _BD.run(
        ["list", "--parent", bead, "--all", "--type", "event", "--json", "-n", "0"],
        scratch,
        capture=True,
    )
    assert result.returncode == 0, err_line(result)
    rows = sorted(json.loads(result.stdout), key=lambda r: int(str(r["id"]).rsplit(".", 1)[1]))
    for row in rows:
        assert row["status"] == "closed"  # an event never becomes open work
    return [(str(r["title"]), str(r["description"]).splitlines()[0]) for r in rows]


def test_intake_walks_untriaged_needs_info_untriaged_twice_on_a_held_report(scratch) -> None:
    """bd 1.3.0: ``set-state intake=<v>`` replaces the one ``intake:`` label, writes a distinct
    event bead and Dolt commit per call (a repeat of the same transition included), and leaves
    ``bd ready`` and the intake-hold gate exactly as they were."""
    store = BdReportStore(CliRoutes(_BD, scratch), SCOPE, error_line=err_line)
    work = store.create(draft("work"))
    report = store.create(draft(bound=True))  # born held: an open intake-hold gate blocks it
    (hold,) = store.holds()
    assert hold.blocks == (report,)
    ready_before = _ready(scratch)
    assert report not in ready_before and work in ready_before

    commits = _history_hashes(scratch, report)
    for step, value in enumerate(_WALK):
        store.set_state(report, "intake", value, reason=f"step {step}: {value}", actor=_ACTOR)
        # exactly one intake value at every step: the previous one is replaced, never stacked
        assert _intake_labels(scratch, report) == [f"intake:{value}"]
        # the intake-hold gate keeps excluding the report; nothing else's readiness moves
        assert _ready(scratch) == ready_before
        # every transition, return trips included, lands as its own new version of the report
        now = _history_hashes(scratch, report)
        assert len(now) > len(commits) and set(commits) <= set(now)
        commits = now
    assert [h.blocks for h in store.holds()] == [(report,)]  # the hold survives the whole walk

    title = "State change: intake \u2192 "
    assert _transitions(scratch, report) == [
        (title + "untriaged", "Set intake to untriaged"),
        (title + "needs-info", "Changed intake from untriaged to needs-info"),
        (title + "untriaged", "Changed intake from needs-info to untriaged"),
        (title + "needs-info", "Changed intake from untriaged to needs-info"),
        (title + "untriaged", "Changed intake from needs-info to untriaged"),
    ]


def test_a_report_that_came_back_to_untriaged_is_still_closed_through_the_hold(scratch) -> None:
    """The reset leaves the report's lifetime hold intact: ReportStore.close still force-closes it
    first and resolves the hold after, with the event beads under it already closed."""
    store = BdReportStore(CliRoutes(_BD, scratch), SCOPE, error_line=err_line)
    report = store.create(draft(bound=True))
    for value in _WALK:
        store.set_state(report, "intake", value, reason=value, actor=_ACTOR)

    store.close(report, reason="delivered", actor=_ACTOR)

    row = CliRoutes(_BD, scratch).issue_show_raw(report)
    row = row[0] if isinstance(row, list) else row
    assert row["status"] == "closed" and store.holds() == ()
    assert report not in _ready(scratch)
