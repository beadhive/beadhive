"""Integration: the lifetime intake-hold against a REAL ``bd`` store (acceptance item 6).

One test replays bh-6wj9y E4 (the scratch-store readiness probe, bd 1.3.0) through the product's
own seams, so it pins the bd semantics the design relies on:

* a ``tracks`` edge alone leaves a report ready; the open ``intake-hold:`` gate is what removes it
  from ``bd ready``;
* a plain ``bd close`` of the held report is refused and the report stays open and held;
* ``ReportStore.close`` closes it with ``bd close --force`` FIRST and resolves the hold after, so
  the report is never in ``bd ready`` at any instant (resolving the hold first re-admits it: the
  window the order exists to avoid, shown on a second report);
* metadata is writable on the closed report and ``--has-metadata-key`` finds it, closed included.

The sweep tests then break each invariant by hand on the real store and repair it. Marked
``integration``; self-skips without ``bd`` on PATH. Nothing here touches a real hive: every store
lives in ``tmp_path``, and the readiness probe is a read of that store, never a clock or a sleep.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from beadhive.adapters.intake import SOURCE_KEY, BdReportStore
from beadhive.modules.intake import (
    HoldFindingKind,
    HoldSweepRequest,
    IntakeRefused,
    IntakeService,
    RefusalReason,
)
from beadhive.testing.intake import SCOPE, URL, draft
from beadhive_bd_cli import CliRoutes, SubprocessBd, err_line
from harness.beads import embedded_env, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BD = SubprocessBd(timeout=120, env=embedded_env())


class _NoLabels:
    def violations(self, labels):
        return ()


class _ReadyProbe:
    """A transport that, after every mutating bd call, records what ``bd ready`` lists.

    It wraps the real transport, so the store runs exactly the argv it always runs; the probe adds
    only reads. ``windows`` is then the evidence: the ready set at every instant between the first
    mutation and the last, in order."""

    def __init__(self, inner: SubprocessBd, cwd: Path) -> None:
        self._inner = inner
        self._cwd = cwd
        self.windows: list[tuple[str, tuple[str, ...]]] = []

    def run(self, args, cwd, **kwargs):
        result = self._inner.run(args, cwd, **kwargs)
        if args and args[0] in {"close", "gate"}:
            self.windows.append((" ".join(args[:3]), _ready_ids(self._inner, self._cwd)))
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _ready_ids(bd: Any, cwd: Path) -> tuple[str, ...]:
    rows = CliRoutes(bd, cwd).ready_all()
    return tuple(sorted(str(r["id"]) for r in rows or ()))


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("intake-hold-template")
    git("init", "-q", "-b", "main", cwd=root)
    init_embedded(root, SCOPE.operational_prefix)
    return root


@pytest.fixture
def scratch(template: Path, tmp_path: Path) -> Path:
    target = tmp_path / "store"
    shutil.copytree(template, target)
    return target


def _store(scratch: Path, bd: Any = _BD) -> BdReportStore:
    return BdReportStore(CliRoutes(bd, scratch), SCOPE, error_line=err_line)


def _routes(scratch: Path) -> CliRoutes:
    return CliRoutes(_BD, scratch)


def _show(scratch: Path, bead: str) -> dict:
    row = _routes(scratch).issue_show_raw(bead)
    return row[0] if isinstance(row, list) else row


def test_e4_replay_hold_refused_plain_close_forced_close_without_a_window(scratch) -> None:
    probe = _ReadyProbe(_BD, scratch)
    store = _store(scratch, probe)
    routes = _routes(scratch)

    # W (work), a bound report R (born held), a native report R2 (never held), and U: a bead with
    # an external ref and NO hold, to show a tracks edge alone is not a hold (E4 row 1).
    work = store.create(draft("work"))
    report = store.create(draft(bound=True))
    native = store.create(draft("born native"))
    unheld = routes.create_report(
        "unheld", "bug", ",".join(SCOPE.hive.labels()), external_ref=URL + "9"
    )
    assert unheld.returncode == 0
    unheld_id = json.loads(unheld.stdout)["id"]
    assert routes.dependency_add(unheld_id, work, "tracks").returncode == 0
    store.track(report, work, actor="dev/test")

    ready = _ready_ids(_BD, scratch)
    assert report not in ready  # the open hold gate removes the bound report from `bd ready`
    assert {work, native, unheld_id} <= set(ready)  # tracks alone does not (E4 row 1)
    (hold,) = store.holds()
    assert hold.blocks == (report,)

    # a plain close is refused, the report stays open, held and out of ready (E4 row 4)
    refused = routes.issue_close(report, reason="no", actor="dev/test", capture=True)
    assert refused.returncode != 0 and "cannot close blocked issue" in err_line(refused)
    assert _show(scratch, report)["status"] == "open"
    assert report not in _ready_ids(_BD, scratch)

    # ReportStore.close: the plain attempt is refused, the forced close lands, the hold resolves
    probe.windows.clear()
    store.close(report, reason="delivered", actor="dev/test")

    assert _show(scratch, report)["status"] == "closed"
    assert store.holds() == ()
    # the plain attempt (refused by bd), the forced close, then the resolve: in that order
    assert [step for step, _ in probe.windows] == [
        f"close {report} --reason",
        f"close {report} --reason",
        f"gate resolve {hold.gate}",
    ]
    for step, ready_then in probe.windows:
        assert report not in ready_then, f"{report} was ready after `{step}`"  # no window

    # the order matters: resolving a hold BEFORE the close re-admits an open report (E4 row 7)
    other = store.create(draft("another", bound=True))
    (other_hold,) = store.holds()
    assert routes.gate_resolve(other_hold.gate).returncode == 0
    assert other in _ready_ids(_BD, scratch)

    # metadata writes succeed on the closed report and `--has-metadata-key` finds it (E4 rows 9-10)
    assert routes.issue_set_metadata_document(report, "intake.outbox", {"seq": 1}).returncode == 0
    assert _show(scratch, report)["metadata"]["intake.outbox"] == {"seq": 1}
    found = routes.issue_list(has_metadata_key="intake.outbox", all_=True, limit=0)
    assert [r["id"] for r in found] == [report]
    assert report in [v.id for v in store.bound_reports()]  # intake.source, closed included
    assert SOURCE_KEY in _show(scratch, report)["metadata"]
    # and `bd list --external-ref <url> --all` finds the closed report by URL (E4 row 11)
    assert {v.id for v in store.find_by_external_ref(URL)} == {report, other}


def test_a_report_with_another_live_blocker_is_refused_and_still_held(scratch) -> None:
    store = _store(scratch)
    routes = _routes(scratch)
    report, work = store.create(draft(bound=True)), store.create(draft("work"))
    assert routes.dependency_add(report, work, "blocks").returncode == 0

    with pytest.raises(IntakeRefused) as refused:
        store.close(report, reason="done", actor="dev/test")

    assert refused.value.reason is RefusalReason.LIVE_BLOCKER and work in refused.value.detail
    assert _show(scratch, report)["status"] == "open"
    assert [h.blocks for h in store.holds()] == [(report,)]


def test_closing_again_after_a_crash_is_a_noop_and_the_sweep_resolves_the_left_hold(
    scratch,
) -> None:
    """bd 1.3.0 closes a closed bead again with exit 0 and leaves its gates alone: the hold a
    crash left behind is the doctor sweep's, harmless meanwhile because closed is never ready."""
    store = _store(scratch)
    routes = _routes(scratch)
    report = store.create(draft(bound=True))
    forced = routes.issue_close_held(report, reason="crashed here", actor="dev/test", capture=True)
    assert forced.returncode == 0
    assert [h.blocks for h in store.holds()] == [(report,)]  # closed, hold still open

    store.close(report, reason="retry", actor="dev/test")

    assert [h.blocks for h in store.holds()] == [(report,)]
    assert report not in _ready_ids(_BD, scratch)
    sweep = IntakeService(store, _NoLabels()).sweep_holds(HoldSweepRequest("doctor", repair=True))
    assert [f.kind for f in sweep.repaired] == [HoldFindingKind.UNRESOLVED]
    assert store.holds() == () and _show(scratch, report)["status"] == "closed"


def test_the_sweep_finds_each_broken_invariant_on_a_real_store_and_repairs_it(scratch) -> None:
    store = _store(scratch)
    routes = _routes(scratch)
    service = IntakeService(store, _NoLabels())
    healthy = store.create(draft(bound=True))
    unheld = store.create(draft("unheld", bound=True))
    stale = store.create(draft("stale", bound=True))
    holds = {h.blocks[0]: h.gate for h in store.holds()}
    assert service.sweep_holds(HoldSweepRequest()).clean

    # unheld: a person resolved its hold by hand, so it is ready work (the window)
    assert routes.gate_resolve(holds[unheld]).returncode == 0
    # stale: the forced close landed and the resolve never ran
    assert routes.issue_close_held(stale, reason="crash", actor="dev/test").returncode == 0
    # orphan: a hold whose blocking edge is gone
    orphaned = store.create(draft("orphan", bound=True))
    gate = next(h.gate for h in store.holds() if h.blocks == (orphaned,))
    assert routes.dependency_remove(orphaned, gate).returncode == 0
    assert unheld in _ready_ids(_BD, scratch) and orphaned in _ready_ids(_BD, scratch)

    sweep = service.sweep_holds(HoldSweepRequest())

    assert {(f.kind, f.report or f.gate) for f in sweep.findings} == {
        (HoldFindingKind.MISSING, unheld),
        (HoldFindingKind.MISSING, orphaned),
        (HoldFindingKind.UNRESOLVED, stale),
        (HoldFindingKind.ORPHANED, gate),
    }
    assert sweep.repaired == ()

    repaired = service.sweep_holds(HoldSweepRequest(actor="dev/test", repair=True))

    assert repaired.failed == () and len(repaired.repaired) == 3
    ready = _ready_ids(_BD, scratch)
    assert healthy not in ready and unheld not in ready and orphaned not in ready
    after = service.sweep_holds(HoldSweepRequest())
    # the lost gate is still a finding (never repaired): nothing but an orphan remains
    assert [(f.kind, f.gate) for f in after.findings] == [(HoldFindingKind.ORPHANED, gate)]
