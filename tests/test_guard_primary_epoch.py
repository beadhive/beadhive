"""The intake lock's host check and epoch read as ONE evaluation (bh-25fvb).

``guard.primary_epoch`` runs :func:`guard.guard_primary` and answers :func:`guard.live_epoch`
from the state that very allow decision read, instead of evaluating the frame-eligibility reads a
second time. It is on the security path of every outbox send, so what is pinned here is that it
changes NOTHING but the read count:

* across a cut-over writer, a frame host and a legacy host (with and without a lease), on a SQL
  and on a Git fleet, held by this host or another, it answers (or refuses) exactly what
  ``guard_primary`` followed by ``live_epoch`` answers, including an allow ``_not_primary`` made
  only after healing an expired cached lease from HQ (the epoch is then read afresh);
* ``frame_eligibility.admitted_primary`` hands ``guard_primary`` the lease ``require_intake`` just
  read only for that exact call (its sequence number and its arguments), once, on this thread.

Every source is a stub at the module seam (``writer_state``, ``require_local``,
``authoritative_primary``, ``primary_state``), so each read is counted and nothing touches HQ,
SQL, Git or the clock.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from beadhive import config_store, frame_eligibility, guard, host_lease, hq_authority_expiry
from beadhive.writer_adopt import WriterRow

PREFIX = "tt"
THIS = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0

WRITER_EPOCH, FRAME_EPOCH, CACHED_EPOCH = 12, 9, 7


def _lease(holder: str, epoch: int) -> host_lease.HostLease:
    return host_lease.HostLease(
        host_id=holder,
        label="frame",
        epoch=epoch,
        adopted_at=host_lease.now_stamp(T0),
        expires_at=host_lease.now_stamp(T0 + 600),
    )


class World:
    """One hive as each source reports it, counting every read."""

    def __init__(self, kind: str, *, sql: bool, holder: str = THIS) -> None:
        self.kind, self.sql, self.holder = kind, sql, holder
        self.frame_epoch = FRAME_EPOCH
        self.reads = {"writer": 0, "local": 0, "authoritative": 0, "cached": 0}

    def writer_state(self, hive="", *, cfg=None, hive_dir=None, entry=None):
        self.reads["writer"] += 1
        if self.kind != "writer":
            return None
        return PREFIX, THIS, WriterRow(self.holder, WRITER_EPOCH, "r")

    def require_local(self, hive="", *, cfg=None, hive_dir=None):
        self.reads["local"] += 1
        return SimpleNamespace(admission={}) if self.kind == "frame" else None

    def authoritative_primary(self, hive="", *, cfg=None, hive_dir=None):
        self.reads["authoritative"] += 1
        return PREFIX, THIS, _lease(self.holder, self.frame_epoch)

    def primary_state(self, hive="", *, cfg=None, hive_dir=None, entry=None):
        self.reads["cached"] += 1
        if self.kind == "unadopted":
            return None
        return PREFIX, THIS, _lease(self.holder, CACHED_EPOCH)


#: Long past whatever the clock reads (2001-09-09), so the cached lease is EXPIRED without a clock.
LONG_AGO = 1_000_000_000.0
HEALED_EPOCH = CACHED_EPOCH + 1


class HealingWorld(World):
    """A legacy host whose CACHED lease has expired and names another host: the refusal path
    (``_not_primary`` -> ``_refresh_expired``) re-reads HQ, which heals the local cache to
    ``healed`` (a lease at a new epoch, or ``None`` for no lease at all). Every read after the
    heal sees the healed cache, as ``host_lease.refresh_cached`` mirrors it into the local ref."""

    def __init__(self, *, sql: bool, healed: host_lease.HostLease | None) -> None:
        super().__init__("legacy", sql=sql, holder=OTHER)
        self.healed = healed
        self.refreshes = 0

    def primary_state(self, hive="", *, cfg=None, hive_dir=None, entry=None):
        self.reads["cached"] += 1
        if self.refreshes:
            return None if self.healed is None else (PREFIX, THIS, self.healed)
        stale = replace(_lease(OTHER, CACHED_EPOCH), expires_at=host_lease.now_stamp(LONG_AGO))
        return PREFIX, THIS, stale

    def refresh_cached(self, remote, prefix, *, cwd):
        self.refreshes += 1
        return self.healed


@pytest.fixture
def install(monkeypatch):
    def install(world: World) -> World:
        monkeypatch.setattr(guard, "writer_state", world.writer_state)
        monkeypatch.setattr(guard, "primary_state", world.primary_state)
        monkeypatch.setattr(guard, "_renew_liveness_hint", lambda *a, **k: None)
        if isinstance(world, HealingWorld):  # the real refusal path, with HQ's re-read stubbed
            monkeypatch.setattr(host_lease, "refresh_cached", world.refresh_cached)
            monkeypatch.setattr(guard.config, "hq_dir", lambda: Path("/nonexistent-hq"))
        else:
            monkeypatch.setattr(guard, "_not_primary", lambda *a, **k: "refused: not the primary")
        monkeypatch.setattr(frame_eligibility, "require_local", world.require_local)
        monkeypatch.setattr(frame_eligibility, "authoritative_primary", world.authoritative_primary)
        monkeypatch.setattr(hq_authority_expiry, "warn_if_expiring", lambda *a, **k: None)
        monkeypatch.setattr(config_store, "sql_selected", lambda _api: world.sql)
        return world

    return install


def _outcome(call):
    try:
        return ("epoch", call())
    except typer.Exit as exc:
        return ("refused", exc.exit_code)
    except frame_eligibility.EligibilityError as exc:
        return ("ineligible", str(exc))


def _two_evaluations() -> int:
    guard.guard_primary("", cfg={}, verb="work outbox")
    return guard.live_epoch("", cfg={})


def _one_evaluation() -> int:
    return guard.primary_epoch("", cfg={}, verb="work outbox")


KINDS = ["writer", "frame", "legacy", "unadopted"]


@pytest.mark.parametrize("holder", [THIS, OTHER], ids=["held", "held-elsewhere"])
@pytest.mark.parametrize("sql", [True, False], ids=["sql", "git"])
@pytest.mark.parametrize("kind", KINDS)
def test_primary_epoch_answers_exactly_what_guard_primary_then_live_epoch_answers(
    install, kind, sql, holder
) -> None:
    install(World(kind, sql=sql, holder=holder))
    before = _outcome(_two_evaluations)

    install(World(kind, sql=sql, holder=holder))
    assert _outcome(_one_evaluation) == before


@pytest.mark.parametrize(
    ("kind", "sql", "expected"),
    [
        ("writer", True, ("epoch", WRITER_EPOCH)),
        ("writer", False, ("epoch", WRITER_EPOCH)),
        ("frame", True, ("epoch", FRAME_EPOCH)),  # answered from the admitted lease
        ("frame", False, ("epoch", CACHED_EPOCH)),  # a Git live_epoch reads the cached lease
        ("legacy", False, ("epoch", CACHED_EPOCH)),
        ("legacy", True, ("ineligible", "frame ineligible: AUTHORITY_NOT_READY")),
        ("unadopted", False, ("epoch", 0)),
    ],
)
def test_what_each_host_kind_answers(install, kind, sql, expected) -> None:
    install(World(kind, sql=sql))
    assert _outcome(_one_evaluation) == expected


@pytest.mark.parametrize("kind", ["writer", "frame", "legacy"])
def test_a_host_that_does_not_hold_the_hive_is_refused(install, kind) -> None:
    install(World(kind, sql=kind == "frame", holder=OTHER))
    assert _outcome(_one_evaluation) == ("refused", 1)


@pytest.mark.parametrize("sql", [False, True], ids=["git", "sql"])
@pytest.mark.parametrize(
    ("healed", "on_git"),
    [
        (_lease(THIS, HEALED_EPOCH), ("epoch", HEALED_EPOCH)),  # HQ moved it to THIS host
        (None, ("epoch", 0)),  # HQ holds no lease at all: nothing was ever adopted
        (_lease(OTHER, HEALED_EPOCH), ("refused", 1)),  # still another host's, now live
    ],
    ids=["healed-to-this-host", "healed-to-no-lease", "still-held-elsewhere"],
)
def test_an_allow_that_healed_the_cache_reads_the_epoch_afresh(
    install, healed, on_git, sql
) -> None:
    """``_not_primary`` really heals here (it returns ``""`` after ``_refresh_expired`` re-read
    HQ): the epoch must be the HEALED lease's, as ``guard_primary`` then ``live_epoch`` answer,
    never the expired one (``CACHED_EPOCH``) the allow decision was first read on (B5)."""
    install(HealingWorld(sql=sql, healed=healed))
    before = _outcome(_two_evaluations)

    world = install(HealingWorld(sql=sql, healed=healed))
    after = _outcome(_one_evaluation)

    assert after == before
    assert world.refreshes == 1
    if not sql:  # a SQL legacy host is ineligible for live_epoch either way (see below)
        assert after == on_git
        assert after != ("epoch", CACHED_EPOCH)


def test_a_frame_host_on_sql_reads_the_lease_once_not_four_times(install) -> None:
    world = install(World("frame", sql=True))
    assert guard.primary_epoch("", cfg={}) == FRAME_EPOCH
    assert world.reads["authoritative"] == 1  # guard_primary twice + live_epoch twice before
    assert world.reads["local"] == 2  # require_intake's own admission re-check, once


def test_outside_primary_epoch_live_epoch_reads_afresh(install) -> None:
    world = install(World("frame", sql=True))
    assert guard.primary_epoch("", cfg={}) == FRAME_EPOCH
    world.frame_epoch = FRAME_EPOCH + 1  # adopted again since
    assert guard.live_epoch("", cfg={}) == FRAME_EPOCH + 1  # nothing kept from the scope

    guard.guard_primary("", cfg={})  # not inside primary_epoch: no decision is kept
    world.frame_epoch = FRAME_EPOCH + 2
    assert guard.live_epoch("", cfg={}) == FRAME_EPOCH + 2


def test_a_refused_primary_epoch_leaves_nothing_for_the_next_read(install) -> None:
    world = install(World("frame", sql=True, holder=OTHER))
    assert _outcome(_one_evaluation) == ("refused", 1)
    world.holder = THIS
    assert guard.live_epoch("", cfg={}) == FRAME_EPOCH


# -- frame_eligibility.admitted_primary --------------------------------------------------------


def test_the_admitted_lease_is_handed_over_once_for_that_exact_call(install) -> None:
    world = install(World("frame", sql=True))
    calls = frame_eligibility.intake_calls()
    assert frame_eligibility.require_intake("acme") is not None

    admitted = frame_eligibility.admitted_primary("acme", after=calls)
    assert admitted is not None and admitted[2].epoch == FRAME_EPOCH
    assert frame_eligibility.admitted_primary("acme", after=calls) is None  # single-use
    assert world.reads["authoritative"] == 1


@pytest.mark.parametrize(
    ("hive", "hive_dir"),
    [("other", None), ("acme", "/elsewhere")],
    ids=["another-hive", "another-dir"],
)
def test_other_arguments_get_nothing(install, hive, hive_dir) -> None:
    install(World("frame", sql=True))
    calls = frame_eligibility.intake_calls()
    frame_eligibility.require_intake("acme")
    assert frame_eligibility.admitted_primary(hive, hive_dir=hive_dir, after=calls) is None
    # and the mismatch consumed it: the right arguments get nothing afterwards either
    assert frame_eligibility.admitted_primary("acme", after=calls) is None


def test_another_call_since_gets_nothing(install) -> None:
    install(World("frame", sql=True))
    calls = frame_eligibility.intake_calls()
    frame_eligibility.require_intake("acme")
    frame_eligibility.require_intake("acme")  # a second evaluation began after ours
    assert frame_eligibility.admitted_primary("acme", after=calls) is None

    latest = frame_eligibility.intake_calls()
    frame_eligibility.require_intake("acme")
    assert frame_eligibility.admitted_primary("acme", after=latest) is not None


@pytest.mark.parametrize("kind", ["legacy", "unadopted"])
def test_a_legacy_host_gets_nothing(install, kind) -> None:
    install(World(kind, sql=False))
    calls = frame_eligibility.intake_calls()
    assert frame_eligibility.require_intake("acme") is None
    assert frame_eligibility.admitted_primary("acme", after=calls) is None


def test_a_refused_admission_leaves_nothing(install) -> None:
    install(World("frame", sql=True, holder=OTHER))
    calls = frame_eligibility.intake_calls()
    with pytest.raises(frame_eligibility.EligibilityError, match="current_hive_lease_holder"):
        frame_eligibility.require_intake("acme")
    assert frame_eligibility.admitted_primary("acme", after=calls) is None


def test_another_thread_never_sees_this_threads_admission(install) -> None:
    install(World("frame", sql=True))
    calls = frame_eligibility.intake_calls()
    frame_eligibility.require_intake("acme")
    seen: list[object] = []
    worker = threading.Thread(
        target=lambda: seen.append(frame_eligibility.admitted_primary("acme", after=calls))
    )
    worker.start()
    worker.join()
    assert seen == [None]
    assert frame_eligibility.admitted_primary("acme", after=calls) is not None
