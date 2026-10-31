"""Rollback inverses per kind (format ADR section 5): each restores the bead's fold just before
the event it undoes; a create is undone by a retire, never a delete; comment and tombstone have
no inverse by design."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

import beadlog
from beadhive_backfill import ROLLBACK_REASON, Stamper, has_inverse, inverse_of

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000cc"
BEAD = beadlog.BeadRef(GROUP, "inv-1")
OTHER = beadlog.BeadRef(GROUP, "inv-2")
HISTORY = "2026-07-01T00:00:00Z"
APPLIED = "2026-08-01T00:00:00Z"
NOW = "2026-10-31T00:00:00Z"


def event(kind: str, data: Mapping[str, Any], *, at: str = APPLIED, ref: str = "") -> Any:
    return beadlog.mint_event(
        kind,
        BEAD,
        data,
        occurred_at=at,
        recorded_at=at,
        extracted_at=at,
        source=("test", ref or f"{kind}/{sorted(data.items())}"),
        provenance="observed",
    )


def history() -> list[beadlog.BeadEvent]:
    return [
        event("create", {"type": "task", "fields": {"title": "Old", "priority": 2}}, at=HISTORY),
        event("label_add", {"label": "keep"}, at=HISTORY),
        event("edge_add", {"type": "blocks", "target": OTHER.to_json()}, at=HISTORY),
        event("phase", {"phase": "in_progress"}, at=HISTORY),
    ]


def undo(base: list[beadlog.BeadEvent], applied: beadlog.BeadEvent) -> Any:
    before = beadlog.fold(base).get(BEAD)
    stamp = Stamper(NOW, [*base, applied])
    inverses = inverse_of(applied, before, stamp=stamp, scope="sha256:" + "0" * 64)
    assert inverses is not None
    restored = beadlog.fold([*base, applied, *inverses]).get(BEAD)
    return before, inverses, restored


@pytest.mark.parametrize(
    "applied",
    [
        event("set", {"field": "title", "value": "New"}),
        event("set", {"field": "notes", "value": "added"}),
        event("set", {"field": "type", "value": "bug"}),
        event("label_add", {"label": "fresh"}),
        event("label_add", {"label": "keep"}),
        event("label_remove", {"label": "keep"}),
        event("label_remove", {"label": "absent"}),
        event("edge_add", {"type": "related", "target": OTHER.to_json()}),
        event("edge_remove", {"type": "blocks", "target": OTHER.to_json()}),
        event("phase", {"phase": "closed", "reason": "done"}),
        event("retire", {"reason": "superseded"}),
        event("delete", {"reason": "duplicate"}),
        event("create", {"type": "epic", "fields": {"title": "Re-created"}}),
    ],
    ids=lambda item: f"{item.kind}:{sorted(item.data)}",
)
def test_inverse_restores_the_fold_before_the_event(applied: beadlog.BeadEvent) -> None:
    before, inverses, restored = undo(history(), applied)

    assert beadlog.changed_facets(before, restored) == ()
    assert all(item.kind != "delete" for item in inverses)
    assert all(item.source.system == "beadhive-backfill" for item in inverses)
    assert all(item.provenance == "derived" for item in inverses)


def test_create_of_a_new_bead_is_undone_by_a_retire_never_a_delete() -> None:
    created = event("create", {"type": "task", "fields": {"title": "Brand new"}})

    before, inverses, restored = undo([], created)

    assert before is None
    assert [item.kind for item in inverses] == ["retire"]
    assert inverses[0].data["reason"] == ROLLBACK_REASON
    assert restored.exists and not restored.deleted and restored.phase == "retired"


def test_a_seeding_checkpoint_is_undone_like_a_create() -> None:
    covers = {"occurred_at": APPLIED, "recorded_at": APPLIED, "id": "sha256:" + "1" * 64}
    seeded = beadlog.mint_event(
        "checkpoint",
        BEAD,
        {
            "state": {
                "type": "task",
                "phase": "open",
                "fields": {"title": "Seeded"},
                "labels": [],
                "edges": [],
                "deleted": False,
            },
            "covers_through": covers,
        },
        occurred_at=APPLIED,
        recorded_at=APPLIED,
        extracted_at=APPLIED,
        source=("test", "seed"),
        provenance="derived",
    )
    inverses = inverse_of(seeded, None, stamp=Stamper(NOW, [seeded]), scope="s")
    assert inverses is not None and [item.kind for item in inverses] == ["retire"]
    assert (
        inverse_of(seeded, beadlog.fold(history())[BEAD], stamp=Stamper(NOW, []), scope="s") == ()
    )


@pytest.mark.parametrize(
    "applied",
    [
        event("comment", {"body": "hello"}),
        event("tombstone", {"scope": "bead", "reason": "erasure"}),
    ],
    ids=["comment", "tombstone"],
)
def test_comment_and_tombstone_have_no_inverse(applied: beadlog.BeadEvent) -> None:
    assert not has_inverse(applied)
    before = beadlog.fold(history())[BEAD]
    assert inverse_of(applied, before, stamp=Stamper(NOW, []), scope="s") is None


def test_inverse_ids_are_stable_and_distinct_per_plan() -> None:
    applied = event("label_add", {"label": "fresh"})
    before = beadlog.fold(history())[BEAD]

    def ids(scope: str) -> tuple[str, ...]:
        found = inverse_of(applied, before, stamp=Stamper(NOW, history()), scope=scope)
        assert found is not None
        return tuple(item.id for item in found)

    assert ids("plan-a") == ids("plan-a")
    assert ids("plan-a") != ids("plan-b")


def test_stamper_is_strictly_after_every_dated_event_and_the_clock() -> None:
    future = event("label_add", {"label": "x"}, at="2027-01-01T00:00:00.5Z")
    stamp = Stamper(NOW, [future, *history()])
    first, second = stamp(), stamp()
    assert beadlog.instant(first) > beadlog.instant("2027-01-01T00:00:00.5Z")
    assert beadlog.instant(second) > beadlog.instant(first)

    plain = Stamper(NOW, history())
    assert plain() == NOW
