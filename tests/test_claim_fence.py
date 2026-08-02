"""The claim **fencing token** — ``ClaimRecord.host_id`` + ``ClaimRecord.epoch`` (bh-ytbb.10).

Covers the acceptance bar directly:
  * ``ClaimRecord`` gains ``host_id`` and ``epoch``, issued at claim and verified at submit;
  * a recorded epoch older than the live epoch REFUSES the submit and escalates (the same
    structured ``log.warning`` channel every other multi-host write refusal uses);
  * the refusal explicitly states the branch is still pushable, and how to recover;
  * existing seat verification is unchanged — ``verify()`` never consults the token;
  * the headline case: a worker claims under epoch N, the host lease is lost and re-adopted
    mid-work, and the submit is refused at the write boundary.

The lease progression is driven through the REAL ``host_lease`` CAS machinery against a scratch
BARE repo under ``tmp_path`` — never a network remote, and never the operator's real HQ (``BH_HQ``
is repointed, on top of conftest's autouse ``_sandbox_bh_home``). Time is injected via ``at=``;
nothing sleeps.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import typer

from beadhive import claim_authority, config, guard, host, host_lease, work
from beadhive.claim_authority import ClaimRecord

PREFIX = "tt"
THIS_HOST = "11111111-1111-4111-8111-111111111111"
OTHER_HOST = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0
BEAD = "bh-ytbb.10"
SEAT = "dev/claimrecord"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


@pytest.fixture
def hq_remote(tmp_path):
    """A scratch BARE repo standing in for the HQ remote the lease CASes against."""
    path = tmp_path / "hq.git"
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return str(path)


@pytest.fixture
def hq(tmp_path, monkeypatch):
    """THIS host's local HQ clone — where the CACHED lease `guard.live_epoch` reads lives."""
    path = tmp_path / "hq"
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    monkeypatch.setenv("BH_HQ", str(path))
    return path


@pytest.fixture
def other_hq(tmp_path):
    """The OTHER host's HQ clone — the object db its takeover CAS pushes from."""
    path = tmp_path / "hq-other"
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    return path


@pytest.fixture
def this_host(monkeypatch):
    monkeypatch.setattr(host, "host_id", lambda: THIS_HOST)
    return THIS_HOST


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """A registered hive whose prefix the guard resolves to (mirrors test_guard_primary)."""
    from beadhive import registry

    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}
    monkeypatch.setattr(registry, "hive_dir_for", lambda _cfg, _hive: tmp_path / "hive")
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    return entry


@pytest.fixture
def worktree(tmp_path):
    """A throwaway git repo standing in for the bead worktree the record is stored in."""
    path = tmp_path / "wt"
    path.mkdir()
    _git(["init", "-q"], path)
    return path


def _adopt_here(hq_remote, hq, *, at=T0, ttl=600.0, host_id=THIS_HOST):
    """Adopt as `host_id` and mirror the won CAS into THIS host's local cache — i.e. exactly
    what `host_adopt` does, so `guard.live_epoch` sees the new generation."""
    outcome = host_lease.adopt(
        hq_remote, PREFIX, host_id=host_id, label="deskmac", cwd=hq, ttl=ttl, at=at
    )
    host_lease.cache(PREFIX, outcome, cwd=hq)
    return outcome


class _Recorder:
    """Captures the structured escalation events, same idiom as test_host_lease."""

    def __init__(self):
        self.seen: list[tuple] = []

    def warning(self, event, **kw):
        self.seen.append((event, kw))


# ---- the record carries the token -------------------------------------------------


def test_claim_record_gains_host_id_and_epoch():
    record = ClaimRecord(bead=BEAD, seat=SEAT, worktree="/tmp/x", issued_at="")

    assert record.host_id == ""  # unfenced default: a factory that never adopted
    assert record.epoch == 0
    assert (
        ClaimRecord(
            bead=BEAD, seat=SEAT, worktree="/tmp/x", issued_at="", host_id="h", epoch=4
        ).epoch
        == 4
    )


def test_issue_stamps_the_token_and_read_round_trips_it(worktree):
    # Arrange
    authority = claim_authority.get_authority("local")

    # Act
    issued = authority.issue(BEAD, SEAT, worktree, host_id=THIS_HOST, epoch=7)
    back = authority.read(worktree)

    # Assert: read() sees exactly what issue() minted, token included.
    assert (issued.host_id, issued.epoch) == (THIS_HOST, 7)
    assert back == issued


def test_a_record_written_before_this_bead_reads_back_unfenced(worktree):
    """Upgrade window: an on-disk record with no token must not become unreadable, and must
    not start refusing submits for a token it was never issued."""
    # Arrange: a pre-bh-ytbb.10 record, written by hand in the old shape.
    authority = claim_authority.get_authority("local")
    path = claim_authority._record_path(worktree)
    path.write_text(
        json.dumps(
            {
                "bead": BEAD,
                "seat": SEAT,
                "worktree": str(worktree),
                "issued_at": "2026-07-01T00:00:00Z",
                "attestation": "none",
            }
        )
    )

    # Act
    record = authority.read(worktree)

    # Assert
    assert record is not None and record.seat == SEAT
    assert (record.host_id, record.epoch) == ("", 0)
    assert record.is_fenced() is False
    assert record.is_stale(99) is False  # fails OPEN: nothing to compare


def test_a_corrupt_epoch_degrades_to_unfenced_not_to_current(worktree):
    """Fail-safe direction: garbage reads as "no token", never as a spuriously live one."""
    authority = claim_authority.get_authority("local")
    path = claim_authority._record_path(worktree)
    path.write_text(
        json.dumps({"bead": BEAD, "seat": SEAT, "worktree": str(worktree), "epoch": "nonsense"})
    )

    assert authority.read(worktree).epoch == 0


@pytest.mark.parametrize(
    ("recorded", "live", "stale"),
    [
        (3, 4, True),  # an adopt happened mid-work -> superseded
        (3, 9, True),  # several did
        (3, 3, False),  # same generation -> healthy
        (3, 2, False),  # a BEHIND live epoch is not staleness (guard_primary's problem)
        (0, 5, False),  # unfenced record -> nothing to compare, fails open
    ],
)
def test_is_stale_truth_table(recorded, live, stale):
    record = ClaimRecord(bead=BEAD, seat=SEAT, worktree="/tmp/x", issued_at="", epoch=recorded)
    assert record.is_stale(live) is stale


# ---- existing seat verification is UNCHANGED --------------------------------------


def test_verify_ignores_the_token_entirely(worktree):
    """The bead ADDS a check on a new axis; it must not move the seat check. A hopelessly
    stale record still verifies its SEAT exactly as before — the epoch refusal is a separate
    decision, so neither can mask the other."""
    authority = claim_authority.get_authority("local")
    stale = authority.issue(BEAD, "dev/alice", worktree, host_id=OTHER_HOST, epoch=1)

    assert stale.is_stale(99) is True  # token superseded ...
    assert authority.verify(stale, "submit", "") is True  # ... seat behaviour untouched
    assert authority.verify(stale, "submit", "dev/alice") is True
    assert authority.verify(stale, "submit", "dev/mallory") is False
    assert authority.verify(None, "submit", "") is False


def test_submits_seat_resolution_does_not_consult_the_token():
    """Structural: the actor/seat path and the fence path are different functions, and the
    seat one never reads epoch/host_id."""
    import inspect

    src = inspect.getsource(work._resolve_submit_actor)
    assert "epoch" not in src
    assert "host_id" not in src


# ---- the live epoch ----------------------------------------------------------------


def test_live_epoch_is_zero_when_nothing_was_ever_adopted(hq, hive, this_host):
    assert guard.live_epoch("", cfg={}) == 0


def test_live_epoch_reads_the_cached_lease_with_no_network(hq_remote, hq, hive, this_host):
    """The claim hot path must stay local — 'workers must not poll'."""
    _adopt_here(hq_remote, hq)

    assert guard.live_epoch("", cfg={}) == 1
    assert host_lease.read_cached(PREFIX, cwd=config.hq_dir()).epoch == 1


def test_claim_stamps_this_hosts_identity_and_the_live_epoch(
    hq_remote, hq, hive, this_host, worktree
):
    """`_issue_claim` is the real claim-time seam (bh work claim / resume both call it)."""
    _adopt_here(hq_remote, hq)

    work._issue_claim({}, {}, BEAD, SEAT, worktree, "")

    record = claim_authority.get_authority("local").read(worktree)
    assert (record.seat, record.host_id, record.epoch) == (SEAT, THIS_HOST, 1)


def test_a_host_with_no_minted_identity_claims_unfenced(hq_remote, hq, hive, worktree, monkeypatch):
    """No `bh config init` on this machine ⇒ no host id ⇒ no token, but the claim still works."""

    def _missing():
        raise FileNotFoundError("host.yaml not minted")

    monkeypatch.setattr(host, "host_id", _missing)
    _adopt_here(hq_remote, hq)

    work._issue_claim({}, {}, BEAD, SEAT, worktree, "")

    assert claim_authority.get_authority("local").read(worktree).host_id == ""


# ---- THE HEADLINE: lease lost mid-work, caught at submit ---------------------------


def _lose_the_lease_mid_work(hq_remote, hq, other_hq):
    """The simulated outage, using the real CAS path end to end:

      1. THIS host is primary at epoch 1 and a worker claims under it;
      2. the lease lapses and the OTHER host takes over  -> epoch 2 at HQ;
      3. hours later THIS host re-adopts and resumes being primary -> epoch 3, cached here.

    Step 3 is what makes this the case `guard_primary` cannot see: this host holds a perfectly
    live lease again, so the write verb is allowed — but every claim minted under epoch 1 is
    superseded, and only the fencing token knows it."""
    host_lease.adopt(
        hq_remote,
        PREFIX,
        host_id=OTHER_HOST,
        label="laptop",
        cwd=other_hq,
        ttl=600.0,
        at=T0 + 601,
    )
    return _adopt_here(hq_remote, hq, at=T0 + 1300)


@pytest.fixture
def stale_claim(hq_remote, hq, other_hq, hive, this_host, worktree, monkeypatch):
    """A worktree holding a claim minted under epoch 1, with the hive now at epoch 3."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1301)
    _adopt_here(hq_remote, hq)
    work._issue_claim({}, {}, BEAD, SEAT, worktree, "")  # the worker claims (epoch 1)
    outcome = _lose_the_lease_mid_work(hq_remote, hq, other_hq)
    assert outcome.lease.epoch == 3, outcome  # guard against the fixture silently no-op'ing
    return worktree


def test_submit_is_refused_when_the_lease_was_lost_mid_work(stale_claim, capsys):
    # Act: the worker comes back after hours and submits.
    with pytest.raises(typer.Exit):
        work._guard_claim_fence({}, {}, stale_claim, "")

    # Assert
    err = capsys.readouterr().err
    assert guard.STALE_CLAIM_REFUSAL_MARKER in err
    assert "epoch 1" in err  # what it was claimed under
    assert "epoch 3" in err  # what is in force now


def test_guard_primary_alone_would_NOT_have_caught_it(stale_claim):
    """The whole reason this check exists: after the re-adopt this host legitimately holds the
    hive again, so the primary gate is satisfied and would wave the stale submit straight
    through. The two checks are complementary, not redundant."""
    guard.guard_primary("", cfg={})  # no raise

    with pytest.raises(typer.Exit):
        work._guard_claim_fence({}, {}, stale_claim, "")


def test_the_refusal_says_the_branch_is_still_pushable_and_how_to_recover(stale_claim, capsys):
    """Acceptance, verbatim: gating bead WRITES rather than code pushes only helps if the
    refusal SAYS the work is salvageable — otherwise an operator assumes the branch is stuck
    and does something destructive to rescue it."""
    with pytest.raises(typer.Exit):
        work._guard_claim_fence({}, {}, stale_claim, "")
    err = capsys.readouterr().err

    # the branch is still pushable — stated, and shown
    assert "still pushable" in err
    assert "YOUR WORK IS NOT LOST" in err
    assert f"git -C {stale_claim} push" in err
    # ... and two concrete ways out
    assert "re-adopt" in err
    assert f"bh work claim {BEAD} --as {SEAT}" in err  # the exact re-ack, seat included
    assert f"bh work submit {BEAD}" in err
    assert "current primary" in err


def test_the_refusal_escalates_on_the_same_channel_as_every_other_write_refusal(
    stale_claim, monkeypatch
):
    """`guard_primary` escalates a refused write with a structured `log.warning`; so does
    this. Matched rather than reinvented, so a churning fleet shows up in one log stream."""
    recorder = _Recorder()
    monkeypatch.setattr("beadhive.log.get_logger", lambda *_a, **_k: recorder)

    with pytest.raises(typer.Exit):
        work._guard_claim_fence({}, {}, stale_claim, "")

    assert [event for event, _ in recorder.seen] == ["claim_fence_refused"]
    fields = recorder.seen[0][1]
    assert fields["claim_epoch"] == 1
    assert fields["live_epoch"] == 3
    assert fields["claim_host"] == THIS_HOST
    assert fields["bead"] == BEAD
    assert fields["verb"] == "work submit"


def test_the_two_refusals_are_distinguishable(stale_claim, capsys):
    """An operator has to be able to tell "you are not primary" from "your claim is stale" —
    they have different remedies."""
    with pytest.raises(typer.Exit):
        work._guard_claim_fence({}, {}, stale_claim, "")
    err = capsys.readouterr().err

    assert guard.STALE_CLAIM_REFUSAL_MARKER in err
    assert guard.PRIMARY_REFUSAL_MARKER not in err
    assert guard.STALE_CLAIM_REFUSAL_MARKER not in guard._primary_refusal(PREFIX, None)


# ---- everything that must NOT be refused -------------------------------------------


def test_a_current_claim_submits_untouched(hq_remote, hq, hive, this_host, worktree):
    """The overwhelmingly common path: claimed and submitted under one generation."""
    _adopt_here(hq_remote, hq)
    work._issue_claim({}, {}, BEAD, SEAT, worktree, "")

    work._guard_claim_fence({}, {}, worktree, "")  # no raise


def test_a_renewal_does_not_invalidate_an_outstanding_claim(
    hq_remote, hq, hive, this_host, worktree
):
    """`renew` holds the epoch fixed on purpose — a heartbeat is not a handoff, and bumping it
    would refuse every in-flight worker's submit every five minutes."""
    _adopt_here(hq_remote, hq)
    work._issue_claim({}, {}, BEAD, SEAT, worktree, "")
    renewed = host_lease.renew(hq_remote, PREFIX, host_id=THIS_HOST, cwd=hq, at=T0 + 60)
    host_lease.cache(PREFIX, renewed, cwd=hq)

    assert renewed.lease.epoch == 1
    work._guard_claim_fence({}, {}, worktree, "")  # no raise


def test_an_unadopted_factory_is_never_refused(hq, hive, this_host, worktree):
    """Single-host default: the fence switches on when a host adopts, not when this ships."""
    work._issue_claim({}, {}, BEAD, SEAT, worktree, "")
    work._guard_claim_fence({}, {}, worktree, "")  # no raise


def test_a_missing_record_is_not_this_guards_business(hq_remote, hq, hive, this_host, worktree):
    """ "Is this bead claimed at all" belongs to submit's existing seat check; duplicating the
    judgement here would produce a second, differently-worded refusal for one condition."""
    _adopt_here(hq_remote, hq)

    guard.guard_claim_epoch(None, "", cfg={})  # no raise
    work._guard_claim_fence({}, {}, worktree, "")  # no record on disk: no raise


def test_an_unresolvable_hive_yields_no_epoch_and_no_refusal(hq, monkeypatch, worktree):
    from beadhive import registry

    def boom(*_a, **_k):
        raise RuntimeError("no such hive")

    monkeypatch.setattr(registry, "hive_dir_for", boom)

    assert guard.live_epoch("nope", cfg={}) == 0
    guard.guard_claim_epoch(
        ClaimRecord(bead=BEAD, seat=SEAT, worktree=str(worktree), issued_at="", epoch=5),
        "nope",
        cfg={},
    )  # no raise


# ---- wiring ------------------------------------------------------------------------


def test_submit_verifies_the_fence_after_resolving_the_seat():
    """Ordering, asserted on the source: the pre-existing seat guard runs FIRST, so an
    unclaimed bead or a seat mismatch still produces exactly the error it always did."""
    import inspect

    src = inspect.getsource(work.submit)
    assert src.index("_resolve_submit_actor") < src.index("_guard_claim_fence")


def test_both_claim_paths_stamp_the_token():
    """`claim` and `resume` are the two verbs that mint a record; neither may skip the token."""
    import inspect

    for fn in (work._claim_single_bead, work.resume):
        assert "_issue_claim(cfg, entry, bead, actor, target, hive)" in inspect.getsource(fn)
