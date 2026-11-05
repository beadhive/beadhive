"""``labels define --prune`` end to end on GitHub, offline: the intake ``LabelProjection`` over
the root-narrowed GitHub adapter over ``FakeGitHub`` (the package's ``MockTransport`` stand-in,
loaded from the package's test directory).

The adapter's ``delete_labels`` is the optional ``LabelPruner`` the projection needs for a prune,
and the root narrowing view (``beadhive.bootstrap.tracker.narrow``) passes it through only while
``labels.define`` is enabled. An unmapped owned label no open issue carries is deleted under ONE
repository grant, a label an open issue carries is kept, a human's label is never touched, and a
re-run converges. No clock, no sleep, no socket.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive.bootstrap.tracker import narrow
from beadhive.modules.intake.application import LabelProjection
from beadhive.modules.intake.domain import (
    IntakeRefused,
    LabelDefineRequest,
    LabelSettings,
    LeakPolicy,
    OutboxPolicy,
    RefusalReason,
    StorePosture,
)
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    LabelDefinition,
    LabelPruner,
    Operation,
)
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    RecordingSanitizer,
    SequentialIdSource,
)

_FAKE = Path(__file__).resolve().parents[3] / "packages/beadhive-tracker-github/tests"


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "tracker_github_fake_for_root", _FAKE / "tracker_github_fake.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # the fake's dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


_fake = _load_fake()
FakeGitHub, adapter_for = _fake.FakeGitHub, _fake.adapter_for
DEFINE = Operation.LABELS_DEFINE
OPERATOR = "alice"
LEAKS = LeakPolicy(
    bead_prefixes=frozenset({"acme", "pub"}),
    private_dimensions=frozenset({"intake", "origin", "delivery", "notified", "review"}),
    owned_label_prefixes=("bh:",),
)
WHEN = datetime(2026, 1, 2, tzinfo=UTC)


def projection(world: FakeGitHub, *, enabled=READ_OPERATIONS | {DEFINE}) -> LabelProjection:
    adapter = adapter_for(world, enabled=enabled, label_prefixes=("bh:",))
    return LabelProjection(
        MemoryReportStore(SCOPE),
        narrow(adapter, frozenset(enabled)),
        InProcessIntakeLock(),
        ids=SequentialIdSource(),
        registry={},
        settings=LabelSettings(),
        leak_policy=LEAKS,
        sanitizer=RecordingSanitizer.of([]),
        posture=StorePosture(published=False),
        outbox=OutboxPolicy(enabled=True, labels_enabled=True),
    )


def apply(projection: LabelProjection, *, prune: bool):
    plan = projection.define(LabelDefineRequest(OPERATOR, prune=prune)).plan
    return projection.define(LabelDefineRequest(OPERATOR, approve=plan.short_digest, prune=prune))


def test_prune_deletes_an_unmapped_owned_label_under_one_grant_and_a_rerun_converges():
    world = FakeGitHub()
    hive = projection(world)
    assert apply(hive, prune=False).applied  # the catalog itself: create/update, no prune

    for name in ("bh:legacy/carried", "bh:legacy/unused", "bug"):
        world.seed_label_definition(LabelDefinition(name, "ededed", ""))
    carrier = world.add_issue("open", at=WHEN, labels=("bh:legacy/carried",))
    before = world.write_requests()

    plan = hive.define(LabelDefineRequest(OPERATOR, prune=True)).plan
    assert plan.delete == ("bh:legacy/unused",)
    assert [(k.name, k.carried_by) for k in plan.kept] == [
        ("bh:legacy/carried", (carrier.identifier,))
    ]
    applied = apply(hive, prune=True)

    assert applied.applied and len(applied.grant_ids) == 1  # only a delete: one grant
    assert world.write_requests() == before + 1  # one DELETE
    names = {d.name for d in world.label_definitions()}
    assert {"bh:legacy/carried", "bug"} <= names and "bh:legacy/unused" not in names
    assert "bh:legacy/carried" in world.server_labels(carrier)

    after = world.write_requests()
    assert not hive.define(LabelDefineRequest(OPERATOR, prune=True)).plan.changed
    assert world.write_requests() == after  # converged: nothing sent


def test_a_prune_is_refused_when_the_view_has_no_pruner_because_define_is_off():
    world = FakeGitHub()
    world.seed_label_definition(LabelDefinition("bh:legacy/unused", "ededed", ""))
    hive = projection(world, enabled=READ_OPERATIONS)
    with pytest.raises(IntakeRefused) as refused:
        apply(hive, prune=True)
    assert refused.value.reason is RefusalReason.CAPABILITY_UNUSABLE
    assert world.write_requests() == 0
    assert not isinstance(adapter_for(world, label_prefixes=("bh:",)), LabelPruner)
