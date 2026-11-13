"""beadlog-sources: operator decisions (bh-2sicb) -- the decision bead, B5 step 0, the projector.

A decision is one closed ``decision`` bead per unit key; identify consults it before every other
step; ``skip`` projects nothing and is never residual; a stale ``link`` / ``noise`` is reported and
not applied; a run with no decisions is byte-identical to one without this feature.
"""

from __future__ import annotations

from typing import Any

import pytest

import beadlog
from beadlog.testing import InMemoryStore
from beadlog_sources import (
    ClusterOptions,
    DecisionError,
    IdentifyOptions,
    IdentifyRun,
    ProjectOptions,
    collate,
    decision_events,
    decision_id,
    dumps_collation,
    loads_collation,
    project,
    read_decisions,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000dd"
PREFIX = "bh"
AT = "2026-11-13T12:00:00Z"
LABELS = ("origin:backfill", "provider:github", "org:o", "repo:r")
KEY = "doc:docs/design/queue.md"


def bead(ident: str, title: str, *, type_: str = "task", ref: str | None = None):
    fields: dict[str, Any] = {"title": title}
    if ref is not None:
        fields["external_ref"] = ref
    return beadlog.mint_event(
        "create",
        beadlog.BeadRef(GROUP, ident),
        {"type": type_, "fields": fields},
        occurred_at="2026-07-01T00:00:00Z",
        recorded_at="2026-07-01T00:00:00Z",
        extracted_at="2026-11-01T00:00:00Z",
        source=("bd", f"bd:create/{ident}"),
        provenance="observed",
    )


def record(kind: str, target: str | None = None, key: str = KEY, **kw: Any):
    kw.setdefault("decided_at", AT)
    return decision_events(
        prefix=PREFIX,
        group=GROUP,
        key=key,
        kind=kind,
        target=target,
        decided_by="dev/x",
        labels=LABELS,
        **kw,
    )


BASE = (bead("bh-epic", "Queue area", type_="epic"), bead("bh-1", "Hand made queue bead"))


def run(baseline, *, warn=None):
    corpus = ev.corpus(ev.design_record("docs/design/queue.md", title="Queue design"))
    warned: list[str] = []
    col = collate(
        corpus,
        baseline,
        InMemoryLedgerDirectory({}),
        local=PREFIX,
        options=ClusterOptions(),
        identify_options=IdentifyOptions(new_threshold=100),
        identify_run=IdentifyRun(warn=warned.append),
    )
    out = project(
        col, corpus, baseline, prefix=PREFIX, group=GROUP, extracted_at=AT, options=ProjectOptions()
    )
    return col, out, warned


def verdict(col):
    (item,) = [v for v in col.verdicts if v["unit"] == KEY and v["class"] is not None]
    return item


def test_the_record_is_a_closed_decision_bead_with_the_adr_shape() -> None:
    events = record("link", "bh-1", reason="same work")
    store = InMemoryStore()
    store.load(events, dry_run=False)
    ((ref, state),) = beadlog.fold(store.export()).items()
    assert ref.id == decision_id(PREFIX, GROUP, KEY)
    assert state.type == "decision" and state.phase == "closed"
    assert set(LABELS) | {"source:decision"} == set(state.labels)
    assert state.fields["external_ref"] == "bfdec:" + KEY
    assert state.fields["metadata.backfill.decision"] == "link"
    assert state.fields["metadata.backfill.unit"] == KEY
    assert state.fields["metadata.backfill.target"] == "bh-1"
    assert state.fields["metadata.backfill.reason"] == "same work"
    assert state.fields["metadata.backfill.decided_by"] == "dev/x"
    assert state.fields["metadata.backfill.decided_at"] == AT
    (found,) = read_decisions(store.export()).values()
    assert (found.unit, found.kind, found.target) == (KEY, "link", "bh-1")


@pytest.mark.parametrize(
    ("kind", "target"), [("skip", "bh-1"), ("link", None), ("noise", None), ("bogus", None)]
)
def test_a_decision_the_adr_does_not_admit_is_refused(kind: str, target: str | None) -> None:
    with pytest.raises(DecisionError):
        record(kind, target)


def test_a_new_decision_updates_the_same_bead() -> None:
    first = record("skip")
    state = beadlog.fold(first)[beadlog.BeadRef(GROUP, decision_id(PREFIX, GROUP, KEY))]
    again = record("link", "bh-1", existing=state.fields, decided_at="2026-11-14T00:00:00Z")
    assert {e.bead for e in again} == {e.bead for e in first}
    assert all(str(e.kind) == "set" for e in again)
    (final,) = read_decisions([*first, *again]).values()
    assert (final.kind, final.target) == ("link", "bh-1")


def test_no_decisions_is_byte_identical() -> None:
    col, out, warned = run(BASE)
    assert warned == []
    assert out.counts() == {"NEW": 1, "PRESENT": 0, "NOISE": 0, "residual": 0}
    assert all(v["step"] != "decision" for v in col.verdicts)


def test_skip_projects_nothing_and_is_not_residual() -> None:
    col, out, _ = run((*BASE, *record("skip")))
    assert verdict(col)["step"] == "decision" and verdict(col)["class"] == "SKIP"
    assert col.residual == () and out.events == () and out.residual == ()
    assert out.counts()["skip"] == 1 and out.unit(KEY).decision == "skip"
    assert loads_collation(dumps_collation(col)) == col  # the strict loader admits the extension


def test_link_is_present_against_the_named_bead() -> None:
    col, out, _ = run((*BASE, *record("link", "bh-1")))
    assert (verdict(col)["class"], verdict(col)["bead"]) == ("PRESENT", "bh-1")
    assert out.unit(KEY).decision == "PRESENT" and out.unit(KEY).bead == "bh-1"
    assert not [e for e in out.events if str(e.kind) == "create"]


def test_noise_is_noise_against_the_named_epic() -> None:
    col, out, _ = run((*BASE, *record("noise", "bh-epic")))
    assert (verdict(col)["class"], verdict(col)["bead"]) == ("NOISE", "bh-epic")
    assert out.events == () and out.unit(KEY).decision == "NOISE"


@pytest.mark.parametrize(
    ("kind", "target", "why"),
    [("link", "bh-gone", "no longer"), ("noise", "bh-1", "not an epic")],
)
def test_a_stale_target_is_reported_and_not_applied(kind: str, target: str, why: str) -> None:
    col, out, warned = run((*BASE, *record(kind, target)))
    assert len(warned) == 1 and why in warned[0] and KEY in warned[0]
    assert verdict(col)["step"] != "decision"  # fell through to normal identification
    assert out.unit(KEY).decision == "NEW"


def test_decision_beads_are_never_candidates_or_refs() -> None:
    # the decision's own title shares tokens with the unit, and bfdec:<key> is not a unit ref
    only_decision = (*BASE, *record("skip", key="doc:docs/other.md"))
    col, out, warned = run(only_decision)
    assert warned == [] and verdict(col)["step"] != "decision"
    assert verdict(col)["class"] == "NEW" and out.unit(KEY).decision == "NEW"


def test_deleted_bead_then_decision_is_not_proposed_again() -> None:
    _, first, _ = run(BASE)
    store = InMemoryStore()
    store.load(BASE, dry_run=False)
    store.load(first.events, dry_run=False)
    gone = first.unit(KEY).bead
    survivors = [e for e in store.export() if e.bead.id != gone]
    assert run(survivors)[1].unit(KEY).decision == "NEW"  # today: proposed again
    assert run((*survivors, *record("skip")))[1].unit(KEY).decision == "skip"
