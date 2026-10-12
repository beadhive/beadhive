"""Epoch-fencing scenarios 1, 2 and 4 on the product adopt over the in-memory fence (bh-utvyj).

``tests/test_epoch_fencing_int.py`` proved these on real bd/Dolt frames through the prototype
harness. The invariants they assert about the PROTOCOL — handoff ordering, the push
compare-and-set, mark retirement, placement CAS loss and the step-2 data check — are asserted
here on the product code (:func:`beadhive.writer_adopt.coexistence_adopt`,
:func:`~beadhive.writer_adopt.place`, :func:`~beadhive.writer_adopt.run_step2` and the write
gate :func:`beadhive.guard.writer_refusal`) over the HQ storage port's fakes:

* every frame is a peer :class:`~beadhive_hq_storage.InMemoryHiveFence` node of ONE simulated
  remote (a clone each, with its own local ``main``), so a push from a stale base is a genuine
  non-fast-forward ``Conflict``;
* placement is the port's ``PlacementStore`` view of
  :class:`~beadhive_hq_storage.InMemoryHqStorage`;
* a data write is :meth:`~beadhive_hq_storage.InMemoryHiveFence.local_write`, used only where
  the product write gate admits it (the frame's local ``bh_writer`` names it).

What stays on real Dolt, and where (``tests/test_epoch_fencing_int.py``):

* the guard TRIGGERS refusing a stale frame's write on ``main`` (``GUARD_REFUSAL``) — scenario 3
  asserts it for the embedded and the server-mode frame, after the same sync to the new head;
* bd's settle gate refusing a stale frame's pull (``SETTLE_REFUSAL``) and a stale bd push being
  rejected as non-fast-forward — scenario 5 (``bd-pull-then-push``, ``bd-dolt-push``);
* Dolt's push being a true compare-and-set under concurrency — the ``file://`` push CAS test.

No clock is read: nothing here depends on time. No sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from beadhive import guard
from beadhive import writer_adopt as wa
from beadhive_hq_storage import (
    Applied,
    BumpRequest,
    Conflict,
    FenceSnapshot,
    InMemoryHiveFence,
    InMemoryHqStorage,
    RecordKind,
    WriterRow,
    adopt_sentinel,
    new_operation_id,
)
from harness.clock import FakeClock

PREFIX = "fx"
#: The fenced cluster's starting state: the fence installed from frame ``c`` at HQ epoch 1.
FOUNDER = "c"


class NoLegacyRef:
    """A cut-over hive whose legacy ``refs/bh/epoch`` is gone (Φ3+): no lockstep CAS."""

    def read(self):
        return None

    def cas(self, frame, epoch, *, expected):  # pragma: no cover - never called without a ref
        raise AssertionError("no legacy ref to CAS")


class CountingNode:
    """A node that counts its pushes (the int test read the adopter's gate log for this)."""

    def __init__(self, node: InMemoryHiveFence):
        self.node, self.pushes = node, 0

    def push(self):
        self.pushes += 1
        return self.node.push()

    def __getattr__(self, name):
        return getattr(self.node, name)


@dataclass
class Hive:
    """One hive: HQ placement and three frame nodes on one remote."""

    hq: InMemoryHqStorage
    nodes: dict[str, InMemoryHiveFence] = field(default_factory=dict)

    @property
    def placements(self):
        return self.hq.placements()

    def __getitem__(self, name: str) -> InMemoryHiveFence:
        return self.nodes[name]

    def remote(self) -> FenceSnapshot:
        node = self.nodes[FOUNDER]
        return node.snapshot_at(node.remote_head())

    def remote_head(self):
        return self.nodes[FOUNDER].remote_head()

    def placed(self) -> wa.PlacementView:
        view = self.placements.read(PREFIX)
        assert view is not None
        return view

    def place(self, name: str) -> int:
        """The placement CAS (adopt step 1, product): ``name`` at one past the current epoch,
        from the placement as read now."""
        current = self.placed()
        return wa.place(
            self.placements,
            PREFIX,
            name,
            current.epoch + 1,
            expected=current,
            cause=wa.PLACEMENT_CAUSE_PLANNED,
        ).epoch

    def adopt(self, name: str, node=None, *, before_push=None) -> wa.CoexistenceOutcome:
        """The product coexistence adopt from ``name``'s node: placement, then step 2."""
        return wa.coexistence_adopt(
            node if node is not None else self.nodes[name],
            self.placements,
            NoLegacyRef(),
            prefix=PREFIX,
            frame=name,
            before_push=before_push,
        )

    def step2(self, name: str, epoch: int, *, before_push=None) -> wa.Step2Result:
        """Adopt step 2 alone, at an epoch the director already placed."""
        return wa.run_step2(
            self.nodes[name],
            self.placements,
            prefix=PREFIX,
            frame=name,
            epoch=epoch,
            before_push=before_push,
        )


@pytest.fixture
def hive() -> Hive:
    clock = FakeClock()
    hq = InMemoryHqStorage(clock)
    hq.provision(RecordKind.PLACEMENT, PREFIX, {"frame": FOUNDER, "epoch": 1})
    founder = InMemoryHiveFence(clock, writer=WriterRow(FOUNDER, 1))
    nodes = {FOUNDER: founder, "a": founder.peer(), "b": founder.peer()}
    return Hive(hq, nodes)


def _admitted(node: InMemoryHiveFence, name: str) -> bool:
    """The product write gate's decision from the node's LOCAL ``bh_writer`` (no clock, no HQ)."""
    writer = node.writer()
    assert writer is not None
    return guard.writer_refusal((PREFIX, name, writer)) == ""


def _write(node: InMemoryHiveFence, name: str, title: str) -> None:
    """One tracker write the gate admits: a local data commit with its guard mark."""
    assert _admitted(node, name), f"the write gate refuses {name}: {node.writer()}"
    node.local_write(f"mark-{title}", bead=title)


def _writer(state: FenceSnapshot) -> tuple[str, int]:
    assert state.writer is not None
    return state.writer.frame, state.writer.epoch


def _bump_commits(hive: Hive, since) -> list[tuple[str, int]]:
    """The adopt bumps on the remote head's history newer than ``since``, oldest first."""
    node = hive[FOUNDER]
    out = []
    for revision in node.chain(node.remote_head()):
        if revision == since:
            break
        if node.sentinel_of(revision) is not None:
            out.append(_writer(node.snapshot_at(revision)))
    return out[::-1]


# =============================================================================================
# Scenario 1 — planned handoff while the old writer is idle
# =============================================================================================


def test_s1_planned_handoff_fences_the_idle_old_writer(hive):
    """``c``'s bump lands; the old writer's next push is non-fast-forward; once the old writer
    has the bump, its local ``bh_writer`` names ``c`` and the write gate refuses it."""
    for name in ("a", "b"):
        old = hive[name]
        held = hive.adopt(name).epoch
        assert hive.adopt(name).resumed  # landed: a re-run is a no-op at the same epoch
        _write(old, name, f"s1-{name}-legit")
        assert isinstance(old.push(), Applied)
        legit = (f"mark-s1-{name}-legit", held)
        assert hive.remote().marks == frozenset({(adopt_sentinel(held), held), legit})

        before = hive.remote_head()
        handoff = hive.adopt(FOUNDER)
        new = handoff.epoch
        assert handoff.step2.landed and new == held + 1
        after = hive.remote()
        assert _writer(after) == (FOUNDER, new) and after.live_epoch == new
        # the bump retired every mark of the old epoch; only its own sentinel is left
        assert after.marks == frozenset({(adopt_sentinel(new), new)})
        assert f"s1-{name}-legit" in after.beads
        assert hive.remote_head() != before
        assert _bump_commits(hive, before) == [(FOUNDER, new)]

        # the old writer has not seen the bump: its gate still admits the write locally ...
        _write(old, name, f"s1-{name}-late")
        before = hive.remote_head()
        assert isinstance(old.push(), Conflict)  # ... and its push is non-fast-forward
        assert hive.remote_head() == before

        old.sync_to_remote()  # what the managed path does on seeing a newer epoch
        assert _writer(old.snapshot_at(old.local_head())) == (FOUNDER, new)
        assert not _admitted(old, name)
        assert not {f"s1-{name}-late", f"s1-{name}-after"} & set(hive.remote().beads)


# =============================================================================================
# Scenario 2 — handoff while the old writer is mid-push
# =============================================================================================


def test_s2_handoff_mid_write_lands_before_the_bump_or_is_rejected(hive):
    """The old writer's write is waiting at its push CAS while the adopter runs. Bump first:
    the waiting push is rejected. Write first: it lands, the adopter's push is rejected, step 2
    retries from the new head, so the write sits BELOW the bump. Never after it."""
    for name in ("a", "b"):
        old = hive[name]

        # --- order 1: the bump lands while the old writer's push waits ----------------------
        hive.adopt(name)
        _write(old, name, f"s2-{name}-parked")
        new = hive.adopt(FOUNDER).epoch
        before = hive.remote_head()
        assert isinstance(old.push(), Conflict)
        assert hive.remote_head() == before
        assert f"s2-{name}-parked" not in hive.remote().beads
        old.sync_to_remote()

        # --- order 2: the waiting write is released first, inside the adopter's push --------
        hive.adopt(name)
        _write(old, name, f"s2-{name}-first")
        landed: list = []
        adopter = CountingNode(hive[FOUNDER])
        start = hive.remote_head()
        out = hive.adopt(
            FOUNDER, adopter, before_push=lambda old=old, landed=landed: landed.append(old.push())
        )
        new = out.epoch
        assert out.step2.landed
        assert landed and isinstance(landed[0], Applied), landed
        remote = hive.remote()
        assert f"s2-{name}-first" in remote.beads
        assert _writer(remote) == (FOUNDER, new)
        assert remote.marks == frozenset({(adopt_sentinel(new), new)})
        # The adopter's first push lost the race and was retried from the new head.
        assert adopter.pushes >= 2 and out.step2.attempts == 2
        # The write sits below the bump: the bump is the newest commit, the write its ancestor.
        chain = hive[FOUNDER].chain(hive.remote_head())
        assert hive[FOUNDER].sentinel_of(chain[0]) == adopt_sentinel(new)
        written = [r for r in chain if f"s2-{name}-first" in hive[FOUNDER].snapshot_at(r).beads]
        assert written and chain.index(written[-1]) > 0
        assert _bump_commits(hive, start) == [(FOUNDER, new)]
        old.sync_to_remote()


# =============================================================================================
# Scenario 4 — two adopters race for the same epoch
# =============================================================================================


def test_s4_one_placement_cas_wins_and_a_loser_that_reached_step_2_abandons(hive):
    a, b = hive["a"], hive["b"]
    base = hive.placed()

    # HQ CAS: both read epoch `base` and race; exactly one wins.
    results: dict[str, object] = {}
    for name in ("a", "b"):
        try:
            results[name] = wa.place(
                hive.placements,
                PREFIX,
                name,
                base.epoch + 1,
                expected=base,
                cause=wa.PLACEMENT_CAUSE_PLANNED,
            )
        except wa.PlacementLost as exc:
            results[name] = exc
    winners = [n for n, r in results.items() if isinstance(r, wa.PlacementView)]
    assert len(winners) == 1, results
    loser = results[{"a": "b", "b": "a"}[winners[0]]]
    assert isinstance(loser, wa.PlacementLost) and "moved since it was read" in str(loser)
    assert (hive.placed().frame, hive.placed().epoch) == (winners[0], base.epoch + 1)

    # Loser-in-data: `a` won epoch e at HQ and reached step 2 (HQ check passed), then the
    # director failed over to `b` at e+1, which landed first. a's push is non-fast-forward;
    # its retry sees b@e+1 >= e in the data and abandons.
    epoch = hive.place("a")

    def fail_over_to_b():
        later = hive.place("b")
        assert later == epoch + 1
        assert hive.step2("b", later).landed

    outcome = hive.step2("a", epoch, before_push=fail_over_to_b)
    assert outcome.outcome is wa.Step2Outcome.SUPERSEDED, outcome
    assert str(outcome.outcome) == "superseded in data"
    remote = hive.remote()
    assert _writer(remote) == ("b", epoch + 1) and remote.live_epoch == epoch + 1
    a.sync_to_remote()
    assert not _admitted(a, "a")

    # Reverse order: the stale adopter's bump lands first, writes once, then the current
    # adopter's push is rejected, retries on top and retires the stale epoch.
    stale_epoch = hive.place("a")
    current_epoch = hive.place("b")
    start = hive.remote_head()

    def a_lands_first():
        a.sync_to_remote()
        revision = wa.fresh_revision("a", stale_epoch)
        stale = BumpRequest("a", stale_epoch, revision, new_operation_id())
        assert isinstance(a.commit_bump(stale), Applied)
        assert isinstance(a.push(), Applied)
        _write(a, "a", "s4-a-at-stale-epoch")
        assert isinstance(a.push(), Applied)
        assert hive.remote().marks == frozenset(
            {(adopt_sentinel(stale_epoch), stale_epoch), ("mark-s4-a-at-stale-epoch", stale_epoch)}
        )

    landed = hive.step2("b", current_epoch, before_push=a_lands_first)
    assert landed.landed and landed.attempts == 2
    remote = hive.remote()
    assert _writer(remote) == ("b", current_epoch) and remote.live_epoch == current_epoch
    assert "s4-a-at-stale-epoch" in remote.beads
    assert remote.marks == frozenset({(adopt_sentinel(current_epoch), current_epoch)})
    assert _bump_commits(hive, start) == [("a", stale_epoch), ("b", current_epoch)]

    # The stale adopter is fenced: its local data still names it, so its gate admits the write,
    # but the push is non-fast-forward and nothing reaches the remote.
    _write(a, "a", "s4-a-stale")
    before = hive.remote_head()
    assert isinstance(a.push(), Conflict)
    assert hive.remote_head() == before
    assert "s4-a-stale" not in hive.remote().beads
    a.sync_to_remote()
    assert not _admitted(a, "a")
    assert b is hive["b"] and _admitted(b, "b")
