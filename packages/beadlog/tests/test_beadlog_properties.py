"""Property tests over generated logs (seeded ``random``, so every case is reproducible).

- ``union_merge`` is commutative, associative and idempotent;
- ``fold`` at any valid-time cut equals an independent "latest event per facet" model;
- ``diff(x, x)`` is empty and ``diff(proposed, baseline)`` returns exactly the missing events.

hypothesis is deliberately not used: the package's tests need only the standard library and
pytest. A failing case names its seed; rerun it with that seed to reproduce.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Iterable, Sequence

import pytest

import beadlog

GROUP = "urn:uuid:00000000-0000-4000-8000-000000000001"
SEEDS = range(40)
BEADS = tuple(beadlog.BeadRef(GROUP, f"bh-{index}") for index in range(4))
LABELS = ("a", "b", "c")
FIELDS = ("title", "priority", "owner", "metadata.k", "type")
EDGE_TYPES = ("blocks", "related")
TIMES = (
    "2026-07-01T00:00:00Z",
    "2026-07-01T00:00:00.000000001Z",
    "2026-07-01T00:00:00.5Z",
    "2026-07-01T00:00:01Z",
    "2026-07-02T12:00:00Z",
    "2026-07-03T00:00:00Z",
    "2026-07-04T23:59:59.999Z",
)
EVIDENCE = tuple(beadlog.Evidence("file", f"doc{index}.md") for index in range(4))


def _time(rng: random.Random, null_chance: float = 0.2) -> str | None:
    return None if rng.random() < null_chance else rng.choice(TIMES)


def _value(rng: random.Random, field: str) -> object:
    if field == "priority":
        return rng.choice([0, 1, 2, None])
    if field == "type":
        return rng.choice(["task", "bug"])
    return rng.choice(["x", "y", "", "ü", None])


def _data(rng: random.Random, kind: str) -> dict:
    if kind == "create":
        fields = {"title": rng.choice(["t1", "t2"])}
        if rng.random() < 0.5:
            fields["owner"] = rng.choice(["x", "y"])
        return {"type": rng.choice(["task", "epic"]), "fields": fields}
    if kind == "set":
        field = rng.choice(FIELDS)
        return {"field": field, "value": _value(rng, field)}
    if kind in ("label_add", "label_remove"):
        return {"label": rng.choice(LABELS)}
    if kind in ("edge_add", "edge_remove"):
        return {"type": rng.choice(EDGE_TYPES), "target": rng.choice(BEADS).to_json()}
    if kind == "comment":
        return {"body": rng.choice(["hi", "line\nbreak", "✓"])}
    if kind == "phase":
        return {"phase": rng.choice(["open", "in_progress", "closed"])}
    if kind == "retire":
        return {"reason": "superseded"} if rng.random() < 0.5 else {}
    return {}  # delete


KINDS = (
    "create",
    "set",
    "set",
    "label_add",
    "label_add",
    "label_remove",
    "edge_add",
    "edge_remove",
    "comment",
    "phase",
    "retire",
    "delete",
)


def _event(rng: random.Random, serial: int, *, kind: str | None = None) -> beadlog.BeadEvent:
    kind = kind or rng.choice(KINDS)
    occurred = _time(rng)
    recorded = _time(rng)
    return beadlog.mint_event(
        kind,
        rng.choice(BEADS),
        _data(rng, kind),
        occurred_at=occurred,
        recorded_at=recorded,
        occurred_precision="day" if occurred is not None and rng.random() < 0.1 else None,
        extracted_at=rng.choice(TIMES),
        source=("gen", f"ev/{serial}"),
        evidence=rng.sample(EVIDENCE, rng.randint(0, 2)),
        provenance=rng.choice(["observed", "derived", "inferred"]),
        confidence=rng.choice([None, 0.5, 0.75, 1.0]),
        actor=rng.choice([None, "dev/a", "dev/b"]),
        annotations=rng.choice([None, {"x-run": rng.randint(0, 3)}, {"x-tag": "t", "x-run": 1}]),
    )


def _variant(rng: random.Random, event: beadlog.BeadEvent) -> beadlog.BeadEvent:
    """The same event id observed again: only non-identity members differ."""
    raw = event.to_json()
    raw["extracted_at"] = rng.choice(TIMES)
    if rng.random() < 0.5:
        raw["confidence"] = rng.choice([0.25, 0.9])
    else:
        raw.pop("confidence", None)
    raw["source"]["evidence"] = [item.to_json() for item in rng.sample(EVIDENCE, rng.randint(0, 3))]
    if not raw["source"]["evidence"]:
        del raw["source"]["evidence"]
    raw["annotations"] = {"x-run": rng.randint(0, 3), "x-other": rng.choice(["p", "q"])}
    if event.kind is beadlog.EventKind.COMMENT and rng.random() < 0.5:
        del raw["data"]["body"]
        raw["redacted"] = ["/data/body"]
    return beadlog.BeadEvent.from_json(raw)


def _tombstone(
    rng: random.Random, serial: int, pool: Sequence[beadlog.BeadEvent]
) -> beadlog.BeadEvent:
    victim = rng.choice(pool)
    return beadlog.mint_event(
        "tombstone",
        victim.bead,
        {"scope": "events", "events": [victim.id], "reason": "secret"},
        occurred_at=_time(rng),
        recorded_at=_time(rng),
        extracted_at=rng.choice(TIMES),
        source=("operator", f"erase/{serial}"),
        provenance="observed",
    )


def _corpus(seed: int, size: int = 30) -> list[beadlog.BeadEvent]:
    rng = random.Random(seed)
    events = [_event(rng, serial) for serial in range(size)]
    events += [_variant(rng, rng.choice(events)) for _ in range(size // 3)]
    if rng.random() < 0.6:
        events.append(_tombstone(rng, seed, events))
    return events


def _split(
    seed: int, events: Sequence[beadlog.BeadEvent], parts: int
) -> list[list[beadlog.BeadEvent]]:
    rng = random.Random(seed * 7919 + parts)
    logs: list[list[beadlog.BeadEvent]] = [[] for _ in range(parts)]
    for event in events:
        for index in rng.sample(range(parts), rng.randint(1, parts)):
            logs[index].append(event)
    for log in logs:
        rng.shuffle(log)
    return logs


def snapshot(log: Iterable[beadlog.BeadEvent]) -> list[str]:
    return [json.dumps(event.to_json(), sort_keys=True, ensure_ascii=False) for event in log]


# -- union_merge -------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_union_merge_is_commutative(seed: int) -> None:
    a, b = _split(seed, _corpus(seed), 2)
    assert snapshot(beadlog.union_merge([a, b])) == snapshot(beadlog.union_merge([b, a]))


@pytest.mark.parametrize("seed", SEEDS)
def test_union_merge_is_associative(seed: int) -> None:
    a, b, c = _split(seed, _corpus(seed), 3)
    left = beadlog.union_merge([beadlog.union_merge([a, b]), c])
    right = beadlog.union_merge([a, beadlog.union_merge([b, c])])
    flat = beadlog.union_merge([a, b, c])
    assert snapshot(left) == snapshot(right) == snapshot(flat)


@pytest.mark.parametrize("seed", SEEDS)
def test_union_merge_is_idempotent(seed: int) -> None:
    (log,) = _split(seed, _corpus(seed), 1)
    once = beadlog.union_merge([log])
    assert snapshot(beadlog.union_merge([log, log])) == snapshot(once)
    assert snapshot(beadlog.union_merge([once])) == snapshot(once)
    assert snapshot(beadlog.union_merge([once, log])) == snapshot(once)


@pytest.mark.parametrize("seed", SEEDS)
def test_union_merge_output_is_unique_sorted_tombstone_free_and_verifiable(seed: int) -> None:
    merged = beadlog.union_merge([_corpus(seed)])
    ids = [event.id for event in merged]
    assert len(ids) == len(set(ids))
    assert list(merged) == sorted(merged, key=beadlog.part_order)
    erased, _ = beadlog.erasure(merged)
    assert not erased & set(ids)
    for event in merged:
        if event.redacted is None:
            assert beadlog.event_id(event) == event.id


# -- fold at cuts ------------------------------------------------------------------------------


def _latest(events: Iterable[beadlog.BeadEvent], touches: Callable[[beadlog.BeadEvent], bool]):
    relevant = [event for event in events if touches(event)]
    return max(relevant, key=beadlog.ordering_key) if relevant else None


def _model(events: Sequence[beadlog.BeadEvent], bead: beadlog.BeadRef) -> dict:
    """Independent reference: each facet is decided by the max-key event touching it."""
    mine = [event for event in events if event.bead == bead]
    kinds = beadlog.EventKind

    def field_value(name: str) -> object:
        if name == "type":
            latest = _latest(
                mine,
                lambda e: (
                    e.kind is kinds.CREATE or (e.kind is kinds.SET and e.data["field"] == "type")
                ),
            )
            if latest is None:
                return None
            return latest.data["type"] if latest.kind is kinds.CREATE else latest.data["value"]
        latest = _latest(
            mine,
            lambda e: (
                (e.kind is kinds.CREATE and name in e.data["fields"])
                or (e.kind is kinds.SET and e.data["field"] == name)
            ),
        )
        if latest is None:
            return None
        return latest.data["fields"][name] if latest.kind is kinds.CREATE else latest.data["value"]

    labels = tuple(
        sorted(
            label
            for label in LABELS
            if (
                latest := _latest(
                    mine,
                    lambda e, label=label: (
                        e.kind in (kinds.LABEL_ADD, kinds.LABEL_REMOVE) and e.data["label"] == label
                    ),
                )
            )
            is not None
            and latest.kind is kinds.LABEL_ADD
        )
    )
    edges = set()
    for event in mine:
        if event.kind in (kinds.EDGE_ADD, kinds.EDGE_REMOVE):
            edge = beadlog.Edge.from_json(event.data)
            latest = _latest(
                mine,
                lambda e, edge=edge: (
                    e.kind in (kinds.EDGE_ADD, kinds.EDGE_REMOVE)
                    and beadlog.Edge.from_json(e.data) == edge
                ),
            )
            if latest is not None and latest.kind is kinds.EDGE_ADD:
                edges.add(edge)
    phase_event = _latest(mine, lambda e: e.kind in (kinds.CREATE, kinds.PHASE, kinds.RETIRE))
    phase = None
    if phase_event is not None:
        phase = {kinds.CREATE: "open", kinds.RETIRE: "retired"}.get(phase_event.kind) or (
            phase_event.data["phase"]
        )
    existence = _latest(mine, lambda e: e.kind in (kinds.CREATE, kinds.DELETE))
    fields = {
        name: value
        for name in FIELDS
        if name != "type" and (value := field_value(name)) is not None
    }
    return {
        "exists": existence is not None and existence.kind is kinds.CREATE,
        "deleted": existence is not None and existence.kind is kinds.DELETE,
        "type": field_value("type"),
        "phase": phase,
        "fields": fields,
        "labels": labels,
        "edges": sorted(edges),
        "comments": [
            e.id for e in sorted(mine, key=beadlog.ordering_key) if e.kind is kinds.COMMENT
        ],
    }


def _observed(state: beadlog.BeadState) -> dict:
    return {
        "exists": state.exists,
        "deleted": state.deleted,
        "type": state.type,
        "phase": state.phase,
        "fields": dict(state.fields),
        "labels": state.labels,
        "edges": sorted(state.edges),
        "comments": [comment.id for comment in state.comments],
    }


@pytest.mark.parametrize("seed", SEEDS)
def test_fold_matches_the_latest_event_per_facet_at_every_cut(seed: int) -> None:
    events = beadlog.union_merge([_corpus(seed)])
    cuts: list[str | None] = [None, "2026-06-30T00:00:00Z", *TIMES, "2026-07-01T00:00:00.25Z"]
    for cut in cuts:
        horizon = beadlog.instant(cut) if cut is not None else None
        inside = [
            event
            for event in events
            if horizon is None or beadlog.ordering_key(event).k1 <= horizon
        ]
        states = beadlog.fold(events, at=cut)
        assert set(states) == {event.bead for event in inside}, (seed, cut)
        for bead, state in states.items():
            assert _observed(state) == _model(inside, bead), (seed, cut, bead)


@pytest.mark.parametrize("seed", SEEDS)
def test_fold_is_invariant_under_merge_order_and_duplication(seed: int) -> None:
    a, b = _split(seed, _corpus(seed), 2)
    reference = beadlog.fold(beadlog.union_merge([a, b]))
    assert beadlog.fold([*b, *a, *a]) == reference


# -- diff --------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_diff_of_a_log_with_itself_is_empty(seed: int) -> None:
    log = _corpus(seed)
    assert beadlog.diff(log, log).empty
    merged = beadlog.union_merge([log])
    assert beadlog.diff(merged, log).empty
    assert beadlog.diff(log, merged).missing == ()


@pytest.mark.parametrize("seed", SEEDS)
def test_diff_returns_exactly_the_missing_events(seed: int) -> None:
    rng = random.Random(seed + 10_000)
    baseline = _corpus(seed)
    extra = [_event(rng, 1000 + serial) for serial in range(rng.randint(0, 12))]
    known = {event.id for event in baseline}
    extra = [event for event in extra if event.id not in known]
    shared = rng.sample(baseline, rng.randint(0, len(baseline)))
    proposed = [*shared, *extra, *rng.sample(extra, len(extra) // 2)]
    rng.shuffle(proposed)

    result = beadlog.diff(proposed, baseline)

    assert [event.id for event in result.missing] == [
        event.id for event in beadlog.sort_events({event.id: event for event in extra}.values())
    ]
    touched = {event.bead for event in extra}
    assert touched <= {delta.bead for delta in result.deltas}
    for delta in result.deltas:
        assert {event.id for event in delta.events} == {
            event.id for event in extra if event.bead == delta.bead
        }
        assert delta.events or delta.changed
        assert delta.changed == beadlog.changed_facets(delta.baseline, delta.merged)
