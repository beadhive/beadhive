"""``union_merge`` combining and conflict rules (format ADR sections 6 and 10) and ``diff``."""

from __future__ import annotations

import json

import pytest

import beadlog

GROUP = "urn:uuid:00000000-0000-4000-8000-000000000002"
BEAD = beadlog.BeadRef(GROUP, "bh-m")


def _comment(body: str = "hello", **extra: object) -> beadlog.BeadEvent:
    return beadlog.mint_event(
        "comment",
        BEAD,
        {"body": body},
        occurred_at="2026-03-01T10:00:00Z",
        recorded_at="2026-03-01T10:00:01Z",
        extracted_at="2026-10-01T00:00:00Z",
        source=("bd", "bd:events/7"),
        provenance="observed",
        actor="dev/a",
        **extra,  # type: ignore[arg-type]
    )


def _copy(event: beadlog.BeadEvent, **changes: object) -> beadlog.BeadEvent:
    raw = event.to_json()
    for key, value in changes.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    return beadlog.BeadEvent.from_json(raw)


def test_equal_ids_combine_non_identity_members_by_their_joins() -> None:
    base = _comment(confidence=0.5, evidence=[beadlog.Evidence("file", "b.md")])
    other = _copy(
        base,
        extracted_at="2026-09-01T00:00:00.5Z",
        confidence=0.9,
        source={
            "system": "bd",
            "ref": "bd:events/7",
            "evidence": [{"type": "file", "ref": "a.md"}, {"type": "file", "ref": "b.md"}],
        },
        annotations={"x-run": 2, "x-only-here": True},
    )
    third = _copy(base, confidence=None, annotations={"x-run": 1})

    (merged,) = beadlog.union_merge([[base], [other], [third]])

    assert merged.id == base.id == beadlog.event_id(merged)
    assert merged.extracted_at == "2026-09-01T00:00:00.5Z"
    assert merged.confidence == 0.9
    assert [item.ref for item in merged.source.evidence] == ["a.md", "b.md"]
    assert dict(merged.annotations or {}) == {"x-run": 1, "x-only-here": True}


def test_differing_identity_under_one_id_is_a_collision_never_a_choice() -> None:
    base = _comment()
    forged = beadlog.BeadEvent.from_json(dict(_comment("other body").to_json(), id=base.id))
    with pytest.raises(beadlog.IdCollisionError) as excinfo:
        beadlog.union_merge([[base], [forged]])
    assert excinfo.value.event_id == base.id


def test_an_unredacted_copy_supersedes_a_redacted_one() -> None:
    base = _comment("the secret plan")
    raw = base.to_json()
    del raw["data"]["body"]
    raw["redacted"] = ["/data/body"]
    redacted = beadlog.BeadEvent.from_json(raw)

    assert beadlog.union_merge([[redacted]]) == (redacted,)
    for logs in ([[redacted], [base]], [[base], [redacted]]):
        (merged,) = beadlog.union_merge(logs)
        assert merged.redacted is None
        assert merged.data["body"] == "the secret plan"
    (folded,) = beadlog.fold([redacted]).values()
    assert folded.comments[0].body is None


def test_two_redacted_copies_keep_every_member_and_intersect_pointers() -> None:
    edge = beadlog.mint_event(
        "edge_add",
        BEAD,
        {"type": "related", "target": {"group": GROUP, "id": "bh-private"}},
        occurred_at="2026-03-02T00:00:00Z",
        recorded_at=None,
        extracted_at="2026-10-01T00:00:00Z",
        source=("bd", "bd:events/8"),
        provenance="observed",
        actor="dev/a",
    )
    no_target = edge.to_json()
    del no_target["data"]["target"]
    no_target["redacted"] = ["/data/target"]
    no_actor = edge.to_json()
    del no_actor["actor"]
    no_actor["redacted"] = ["/actor"]
    both = edge.to_json()
    del both["actor"], both["data"]["target"]
    both["redacted"] = ["/actor", "/data/target"]

    copies = [beadlog.BeadEvent.from_json(raw) for raw in (no_target, no_actor, both)]
    (merged,) = beadlog.union_merge([[copies[0]], [copies[1]]])
    assert merged.redacted is None and merged == edge
    (still,) = beadlog.union_merge([[copies[2]], [copies[0]]])
    assert still.redacted == ("/data/target",)
    assert still.actor == "dev/a" and "target" not in still.data
    assert beadlog.fold([still])[BEAD].edges == ()  # an edge with a withheld target is unknown


def test_tombstones_are_absorbing_across_merges() -> None:
    secret = _comment("token=hunter2")
    tombstone = beadlog.mint_event(
        "tombstone",
        BEAD,
        {"scope": "events", "events": [secret.id], "reason": "secret"},
        occurred_at="2026-03-05T00:00:00Z",
        recorded_at="2026-03-05T00:00:00Z",
        extracted_at="2026-10-01T00:00:00Z",
        source=("operator", "erase/1"),
        provenance="observed",
    )
    erased = beadlog.union_merge([[secret], [tombstone]])
    assert erased == (tombstone,)
    # An older archive that still holds the content is erased again on merge.
    assert beadlog.union_merge([erased, [secret]]) == (tombstone,)


def test_merge_writes_part_order() -> None:
    late = _comment("b")
    early = beadlog.mint_event(
        "label_add",
        beadlog.BeadRef(GROUP, "bh-a"),
        {"label": "x"},
        occurred_at=None,
        recorded_at=None,
        extracted_at="2026-10-01T00:00:00Z",
        source=("gen", "1"),
        provenance="inferred",
    )
    assert beadlog.union_merge([[late, early]]) == (early, late)  # bead id sorts first


def test_diff_reports_missing_events_and_the_facets_they_change() -> None:
    def ev(kind: str, data: dict, at: str, ref: str) -> beadlog.BeadEvent:
        return beadlog.mint_event(
            kind,
            BEAD,
            data,
            occurred_at=at,
            recorded_at=None,
            extracted_at="2026-10-01T00:00:00Z",
            source=("docs", ref),
            provenance="inferred",
        )

    create = ev("create", {"type": "task", "fields": {"title": "t"}}, "2026-01-01T00:00:00Z", "1")
    label = ev("label_add", {"label": "origin:backfill"}, "2026-01-02T00:00:00Z", "2")
    retitle = ev("set", {"field": "title", "value": "t2"}, "2026-01-03T00:00:00Z", "3")
    restate = ev("set", {"field": "title", "value": "t"}, "2025-12-01T00:00:00Z", "4")

    result = beadlog.diff([create, label, retitle, restate], [create])

    assert [event.id for event in result.missing] == [restate.id, label.id, retitle.id]
    (delta,) = result.deltas
    assert delta.bead == BEAD
    assert delta.changed == ("fields.title", "label:origin:backfill")
    assert delta.baseline is not None and delta.baseline.fields["title"] == "t"
    assert delta.merged is not None and delta.merged.fields["title"] == "t2"
    assert delta.proposed is not None and delta.proposed.labels == ("origin:backfill",)
    # An older restatement of a known value is missing by id but changes nothing.
    assert beadlog.diff([restate], [create]).deltas[0].changed == ()
    assert json.dumps([e.to_json() for e in result.missing])  # serializable plan material
