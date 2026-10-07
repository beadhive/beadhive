"""The clock-free write gate on cut-over hives, and expiry-advisory everywhere (bh-12hev).

ADR ``docs/design/hive-writer-partitioning-adr.md`` §4: "Time triggers reassignment; it never
gates a write." On a hive whose data carries ``bh_writer``, :func:`guard.guard_primary`,
:func:`guard.live_epoch` and :func:`guard.guard_claim_epoch` read the LOCAL ``bh_writer`` row
through the in-data fence adapter (:func:`beadhive.host_adopt.fence_data_for`, the M1 seam) and
nothing else — no wall clock, no HQ, no frame-authority read, and no lease renewal. Every
other hive keeps the lease gate, with ``expires_at`` advisory; it is still renewed best-effort
as a liveness hint (``test_host_lease_renewal.py``), never as a reason to refuse.

The hive's data here is a fake :class:`~beadhive.writer_adopt.FenceData` (only ``writer()`` is
read); the real-Dolt run against the composed prototype is ``test_guard_writer_gate_int.py``.
The lease store is a scratch HQ clone under ``tmp_path``.
"""

from __future__ import annotations

import subprocess

import pytest
import typer

from beadhive import (
    claim_authority,
    config,
    fence_data_port,
    frame_eligibility,
    gitref,
    guard,
    host,
    host_adopt,
    host_lease,
    registry,
)
from beadhive import host_lease_contracts as contracts
from beadhive.writer_adopt import WriterRow

PREFIX = "tt"
THIS = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0
FAR_FUTURE = T0 + 10 * 365 * 86400


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


@pytest.fixture
def hq(tmp_path, monkeypatch):
    path = tmp_path / "hq"
    path.mkdir()
    _git(["init", "-q"], path)
    monkeypatch.setenv("BH_HQ", str(path))
    return path


@pytest.fixture
def this_host(monkeypatch):
    monkeypatch.setattr(host, "host_id", lambda: THIS)
    return THIS


@pytest.fixture
def hive(tmp_path, monkeypatch):
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}
    monkeypatch.setattr(registry, "hive_dir_for", lambda _cfg, _hive: tmp_path / "hive")
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    return entry


class Data:
    """The hive's local ``bh_writer`` as the fence adapter reports it."""

    def __init__(self, writer: WriterRow | None = None, *, error: Exception | None = None):
        self.local, self.error, self.reads = writer, error, 0

    def writer(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.local


@pytest.fixture
def data(monkeypatch):
    """Register a fake in-data fence adapter. ``monkeypatch`` restores the dormant resolver."""
    fake = Data()
    seen: list[tuple] = []

    def resolver(prefix, hive_dir):
        seen.append((prefix, hive_dir))
        return fake

    monkeypatch.setattr(fence_data_port, "_fence_data_resolver", resolver)
    fake.seen = seen
    return fake


def _cache(hq_dir, lease):
    sha = gitref.write_object(lease.to_record(), cwd=hq_dir)
    gitref.set_local(host_lease.lease_ref(PREFIX), sha, cwd=hq_dir)


def _lease(holder, *, epoch=7, ttl=600.0):
    return host_lease.HostLease(
        host_id=holder,
        label="frame",
        epoch=epoch,
        adopted_at=host_lease.now_stamp(T0),
        expires_at=host_lease.now_stamp(T0 + ttl),
    )


def _clock(monkeypatch, at):
    monkeypatch.setattr(contracts.time, "time", lambda: at)
    monkeypatch.setattr(host_lease.time, "time", lambda: at)


def _no_hq(monkeypatch):
    """Every HQ / authority / lease read now explodes, as does any renewal."""

    def boom(*_a, **_k):
        raise AssertionError("the cut-over gate must not read HQ, the lease or frame authority")

    for owner, name in (
        (host_lease, "read_cached"),
        (host_lease, "read"),
        (host_lease, "renew"),
        (host_lease, "renew_if_due"),
        (frame_eligibility, "require_intake"),
        (frame_eligibility, "authoritative_primary"),
        (frame_eligibility, "incumbent_primary"),
    ):
        monkeypatch.setattr(owner, name, boom)
    monkeypatch.setattr(config, "fleet_sql_selected", lambda: True)


def _record(epoch):
    return claim_authority.ClaimRecord(
        bead="tt-1", seat="dev/x", worktree="/w", issued_at="t", host_id=THIS, epoch=epoch
    )


# ---- acceptance 1: guard_primary on a cut-over hive reads bh_writer only ---------------------


def test_cut_over_writer_frame_writes_with_hq_down_and_the_clock_far_past_any_expiry(
    tmp_path, hive, this_host, data, monkeypatch
):
    data.local = WriterRow(THIS, 9, "r")
    monkeypatch.setenv("BH_HQ", str(tmp_path / "hq-gone"))  # no HQ clone at all
    _no_hq(monkeypatch)
    _clock(monkeypatch, FAR_FUTURE)
    for _ in range(3):  # keeps flowing, write after write
        guard.guard_primary("", cfg={}, verb="claim")
    assert data.reads == 3
    assert data.seen[0] == (PREFIX, tmp_path / "hive")


def test_cut_over_refuses_when_bh_writer_names_another_frame_without_reading_hq(
    hq, hive, this_host, data, monkeypatch, capsys
):
    data.local = WriterRow(OTHER, 9, "r")
    _no_hq(monkeypatch)
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    err = capsys.readouterr().err
    assert guard.PRIMARY_REFUSAL_MARKER in err
    assert OTHER in err and "epoch 9" in err
    assert guard.WRITER_REFUSAL_SOURCE in err


def test_data_wins_over_a_live_cached_lease_naming_this_host(
    hq, hive, this_host, data, monkeypatch
):
    """A superseded writer whose cached lease still names it is stopped by the data."""
    _clock(monkeypatch, T0 + 1)
    _cache(hq, _lease(THIS))
    data.local = WriterRow(OTHER, 8, "r")
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})


def test_data_wins_over_a_cached_lease_naming_another_host(hq, hive, this_host, data, monkeypatch):
    """Placement moved first (adopt incomplete fails old): the data still names this frame."""
    _clock(monkeypatch, T0 + 1)
    _cache(hq, _lease(OTHER, epoch=8))
    data.local = WriterRow(THIS, 7, "r")
    guard.guard_primary("", cfg={})  # no raise


def test_a_host_without_a_minted_identity_is_never_the_writer(hive, data, monkeypatch):
    def missing():
        raise FileNotFoundError("no host.yaml")

    monkeypatch.setattr(host, "host_id", missing)
    data.local = WriterRow("", 9, "r")
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})


def test_an_unreadable_bh_writer_fails_closed(hive, this_host, data, capsys):
    data.error = RuntimeError("database is locked")
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    assert "cannot read the local bh_writer" in capsys.readouterr().err


def test_an_adapter_whose_data_has_no_bh_writer_takes_the_legacy_lease_gate(
    hq, hive, this_host, data, monkeypatch
):
    data.local = None  # adapter registered, data not cut over
    _clock(monkeypatch, T0 + 1)
    _cache(hq, _lease(OTHER))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    _cache(hq, _lease(THIS))
    guard.guard_primary("", cfg={})


def test_the_dormant_default_resolver_never_cuts_a_hive_over(hq, hive, this_host):
    assert host_adopt.fence_data_for(PREFIX, hq) is None
    assert guard.writer_state("", cfg={}) is None


# ---- acceptance 2: live_epoch ----------------------------------------------------------------


def test_live_epoch_reads_bh_writer_epoch_on_a_cut_over_hive(
    hq, hive, this_host, data, monkeypatch
):
    _clock(monkeypatch, T0 + 1)
    _cache(hq, _lease(THIS, epoch=7))
    data.local = WriterRow(THIS, 12, "r")
    _no_hq(monkeypatch)
    assert guard.live_epoch("", cfg={}) == 12


def test_live_epoch_reads_the_lease_epoch_otherwise(hq, hive, this_host, data, monkeypatch):
    _clock(monkeypatch, T0 + 1)
    _cache(hq, _lease(THIS, epoch=7))
    data.local = None
    assert guard.live_epoch("", cfg={}) == 7


# ---- acceptance 3: expires_at is advisory in every mode ------------------------------------


def test_held_by_ignores_expiry_but_is_expired_still_reports_it():
    lease = _lease(THIS, ttl=600.0)
    assert lease.held_by(THIS, T0 + 10_000) and lease.held_by(THIS)
    assert lease.is_expired(T0 + 10_000)  # the failover hint survives
    assert not lease.held_by(OTHER, T0)
    assert not lease.held_by("", T0)
    tombstone = host_lease.HostLease("", "", 3, "t", host_lease.now_stamp(T0))
    assert not tombstone.held_by("")


def test_a_legacy_primary_keeps_writing_past_its_expiry_even_when_renewal_fails(
    hq, hive, this_host, monkeypatch
):
    _cache(hq, _lease(THIS, ttl=600.0))

    def boom(*_a, **_k):
        raise RuntimeError("HQ is down")

    monkeypatch.setattr(host_lease, "renew", boom)
    monkeypatch.setattr(host_lease, "refresh_cached", boom)
    for at in (T0 + 1, T0 + 599, T0 + 601, FAR_FUTURE):
        _clock(monkeypatch, at)
        guard.guard_primary("", cfg={})  # no raise: a failed liveness renewal never refuses


def test_renew_if_due_is_a_no_op_on_a_cut_over_hive(tmp_path, data, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a cut-over hive must not renew or read the lease")

    data.local = WriterRow(THIS, 7, "seed")
    monkeypatch.setattr(host_lease, "renew", boom)
    monkeypatch.setattr(host_lease, "read_cached", boom)
    monkeypatch.setattr(host_lease, "_frame_plane", boom)
    out = host_lease.renew_if_due(
        "origin", PREFIX, host_id=THIS, cwd=tmp_path, at=T0 + 599, hive_dir=tmp_path / "hive"
    )
    assert out is None
    assert data.reads == 1


# ---- acceptance 4: in-flight ClaimRecords survive cutover (seed equality, T1) --------------


def test_a_claim_minted_before_cutover_stays_valid_after_it(hq, hive, this_host, data, monkeypatch):
    _clock(monkeypatch, T0 + 1)
    _cache(hq, _lease(THIS, epoch=7))
    data.local = None  # pre-cutover: legacy model
    record = _record(guard.live_epoch("", cfg={}))
    assert record.epoch == 7

    data.local = WriterRow(THIS, 7, "seed")  # cutover seeds bh_writer.epoch = lease epoch
    _no_hq(monkeypatch)
    assert guard.live_epoch("", cfg={}) == record.epoch
    guard.guard_claim_epoch(record, "", cfg={}, verb="work submit")  # no raise

    data.local = WriterRow(THIS, 8, "bump")  # a later adopt bump supersedes the token
    with pytest.raises(typer.Exit):
        guard.guard_claim_epoch(record, "", cfg={}, verb="work submit")


# ---- the bd passthrough gate follows the same decision ------------------------------------


def test_bd_write_refusal_on_a_cut_over_hive_follows_bh_writer(
    tmp_path, hive, this_host, data, monkeypatch
):
    _no_hq(monkeypatch)
    data.local = WriterRow(THIS, 9, "r")
    assert guard.bd_write_refusal(["update", "tt-1", "--status", "open"], tmp_path) == ""
    assert guard.bd_write_refusal(["dolt", "push"], tmp_path) == ""  # lifted (bh-9c9hh)
    data.local = WriterRow(OTHER, 9, "r")
    assert guard.PRIMARY_REFUSAL_MARKER in guard.bd_write_refusal(["update", "tt-1"], tmp_path)


def test_the_prepush_hook_follows_bh_writer_on_a_cut_over_hive(
    tmp_path, hive, this_host, data, monkeypatch
):
    from beadhive import prepush

    _no_hq(monkeypatch)
    data.local = WriterRow(THIS, 9, "r")
    assert prepush.check_fence(tmp_path / "hive", cfg={}) == (True, "")
    data.local = WriterRow(OTHER, 9, "r")
    ok, detail = prepush.check_fence(tmp_path / "hive", cfg={})
    assert not ok and guard.PRIMARY_REFUSAL_MARKER in detail


# ---- bd verb policy on cut-over hives: push|sync lifted, break-glass refused (bh-9c9hh) ------

BREAK_GLASS = [
    (["dolt", "push", "--force"], "dolt push --force"),
    (["dolt", "push", "-f"], "dolt push --force"),
    (["dolt", "remote", "reset-data"], "dolt remote reset-data"),
    (["backup", "restore", "--force", "x"], "backup restore --force"),
    (["dolt", "pull", "--strategy", "theirs"], "--strategy"),
    (["vc", "merge", "--strategy=ours"], "--strategy"),
    (["conflicts", "resolve", "bh_writer"], "conflicts resolve on bh_* tables"),
    (["conflicts", "resolve"], "conflicts resolve on bh_* tables"),
    (["vc", "merge", "main"], "vc merge"),
]


@pytest.mark.parametrize("args", [["dolt", "push"], ["dolt", "sync"]])
def test_publish_is_lifted_on_a_cut_over_hive_for_the_writer(
    tmp_path, hive, this_host, data, monkeypatch, args
):
    _no_hq(monkeypatch)
    data.local = WriterRow(THIS, 9, "r")
    assert guard.bd_write_refusal(args, tmp_path) == ""
    data.local = WriterRow(OTHER, 9, "r")  # a non-writer frame is still stopped
    assert guard.PRIMARY_REFUSAL_MARKER in guard.bd_write_refusal(args, tmp_path)


@pytest.mark.parametrize(("args", "form"), BREAK_GLASS)
@pytest.mark.parametrize("writer", [THIS, OTHER])
def test_break_glass_forms_are_refused_on_a_cut_over_hive(
    tmp_path, hive, this_host, data, monkeypatch, args, form, writer
):
    _no_hq(monkeypatch)
    data.local = WriterRow(writer, 9, "r")
    refusal = guard.bd_write_refusal(args, tmp_path)
    assert form in refusal and "fence_audit" in refusal and "cut-over" in refusal


@pytest.mark.parametrize(
    "args",
    [
        ["conflicts", "resolve", "issues"],
        ["dolt", "push"],
        ["backup", "restore", "x"],
        ["dolt", "remote", "list"],
    ],
)
def test_non_break_glass_forms_are_not_named_break_glass(args):
    assert guard.break_glass_form(args) == ""


def test_legacy_hive_keeps_0_22_publish_refusal_and_ignores_break_glass_policy(
    tmp_path, hive, this_host, data, monkeypatch
):
    monkeypatch.setattr(guard, "primary_state", lambda **_k: (PREFIX, THIS, _lease(THIS, epoch=7)))
    data.local = None  # not cut over
    assert "refused" in guard.bd_write_refusal(["dolt", "push"], tmp_path)
    assert "refused" in guard.bd_write_refusal(["dolt", "sync"], tmp_path)
    # break-glass is a cut-over-only policy: a legacy primary's `vc merge` is judged as before
    assert guard.bd_write_refusal(["vc", "merge", "main"], tmp_path) == ""
