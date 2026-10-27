"""The composition root of the supervised outbox (I9a): ``beadhive.intake_services``.

Pins what only the root adds to the sender: the ``IntakeLock`` implementation (the host guard,
an exclusive never-waiting ``fcntl.flock`` per binding, the live epoch), the random id source,
the fleet leak policy and store posture, and :func:`intake_services.outbox_service_for`, which
binds NO sender unless the caller passes an enabled policy and never resolves the public store.
The tracker is always injected (``MemoryTracker``); nothing here reads a clock, sleeps, opens a
socket or reaches a real tracker.
"""

from __future__ import annotations

import hashlib

import pytest
import typer

from beadhive import guard, intake_services, private_paths, registry, store_target
from beadhive.modules.intake import (
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    OwnIdentity,
    RefusalReason,
    ReportDraft,
    SourceSnapshot,
)
from beadhive.modules.intake.contracts import (
    OpState,
    OutboxApprove,
    ReplyDraft,
    is_grant_id,
    is_outbox_id,
)
from beadhive.modules.intake.domain import OutboxPolicy
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, OwnMarker
from beadhive.testing.intake import (
    SCOPE,
    InProcessIntakeLock,
    MemoryReportStore,
    SequentialIdSource,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ENTRY = {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "acme"}
CFG = {"managed_repos": [dict(ENTRY)]}
ME = OwnIdentity("acme/widgets", "inst-1")


def marker_line(marker: OwnMarker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


class Untouchable:
    """A tracker that fails on any use: the off-by-default composition must never reach it."""

    def __getattr__(self, name):
        raise AssertionError(f"the tracker was touched: {name}")


def _lock(tmp_path, *, refuse: bool = False, epoch: int = 7) -> intake_services.FlockIntakeLock:
    def primary() -> None:
        if refuse:
            raise IntakeRefused(RefusalReason.NOT_PRIMARY, "not this host")

    return intake_services.FlockIntakeLock(tmp_path / "locks", primary=primary, epoch=lambda: epoch)


# -- the lock ----------------------------------------------------------------------------------


def test_the_flock_lock_yields_the_live_epoch_and_refuses_a_second_holder_at_once(tmp_path):
    lock = _lock(tmp_path)
    with lock.hold("memory:acme/widgets") as epoch:
        assert epoch == 7
        with pytest.raises(IntakeRefused) as busy:  # a second open file description, same key
            with lock.hold("memory:acme/widgets"):
                pass
        assert busy.value.reason is RefusalReason.INTAKE_BUSY
        with lock.hold("memory:someone/else"):  # another binding is its own lock
            pass
    with lock.hold("memory:acme/widgets"):  # released on exit
        pass
    name = hashlib.sha256(b"memory:acme/widgets").hexdigest()
    assert (tmp_path / "locks" / f"{name}.lock").is_file()


def test_the_flock_lock_is_released_when_the_holder_raises(tmp_path):
    lock = _lock(tmp_path)
    with pytest.raises(RuntimeError):
        with lock.hold("k"):
            raise RuntimeError("the operation failed")
    with lock.hold("k"):
        pass


def test_a_host_that_is_not_primary_takes_no_lock(tmp_path):
    with pytest.raises(IntakeRefused) as refused:
        with _lock(tmp_path, refuse=True).hold("k"):
            raise AssertionError("must not run")
    assert refused.value.reason is RefusalReason.NOT_PRIMARY
    assert not (tmp_path / "locks").exists()  # refused before the process lock


def test_the_production_lock_asks_guard_primary_and_live_epoch(tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        guard, "guard_primary", lambda hive, *, cfg, verb: calls.append((hive, verb))
    )
    monkeypatch.setattr(guard, "live_epoch", lambda hive, *, cfg: 3)
    monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: tmp_path / ".bh")
    lock = intake_services.intake_lock(CFG, "/hive", hive="acme")
    with lock.hold("k") as epoch:
        assert epoch == 3
    assert calls == [("acme", "work outbox")]
    assert any((tmp_path / ".bh" / "intake" / "locks").iterdir())

    def refuse(hive, *, cfg, verb):
        raise typer.Exit(1)

    monkeypatch.setattr(guard, "guard_primary", refuse)
    with pytest.raises(IntakeRefused) as refused:
        with lock.hold("k"):
            pass
    assert refused.value.reason is RefusalReason.NOT_PRIMARY


def test_a_hive_without_private_state_cannot_lock(monkeypatch):
    monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: None)
    with pytest.raises(IntakeStoreError):
        intake_services.lock_dir("/not-a-repo")


# -- ids, leak policy, posture ------------------------------------------------------------------


def test_random_ids_are_opaque_and_never_a_bead_id():
    fixed = intake_services.RandomIdSource(lambda n: bytes(range(n)))
    assert is_outbox_id(fixed.outbox_id()) and is_grant_id(fixed.grant_id())
    real = intake_services.RandomIdSource()
    first, second = real.outbox_id(), real.outbox_id()
    assert is_outbox_id(first) and first != second and not first.startswith("acme-")


def test_the_leak_policy_names_every_fleet_prefix_and_closed_dimension():
    cfg = {
        "managed_repos": [
            dict(ENTRY),
            {"provider": "github", "org": "acme", "repo": "gadgets", "prefix": "gad"},
            {
                **ENTRY,
                "repo": "public",
                "prefix": "pubint",
                "stores": {
                    "internal": {"remote": "x"},
                    "public": {"prefix": "pub", "path": "/s/pub", "remote": "git-origin"},
                },
            },
        ]
    }
    policy = intake_services.leak_policy(cfg, owned_label_prefixes=("beadhive:",))
    assert {"acme", "gad", "pubint", "pub"} <= policy.bead_prefixes
    assert {"intake", "delivery", "notified"} <= policy.private_dimensions
    assert policy.owned_label_prefixes == ("beadhive:",)


def test_the_store_posture_is_published_for_a_one_store_or_code_origin_hive(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "hive_dir", lambda e: tmp_path / "hive")
    scopes: list = []
    real = store_target.resolve_store

    def spy(e, scope=None, **kw):
        scopes.append(scope)
        return real(e, scope, **kw)

    monkeypatch.setattr(store_target, "resolve_store", spy)
    legacy = intake_services.store_posture(ENTRY)
    private = intake_services.store_posture({**ENTRY, "stores": {"internal": {"remote": "priv"}}})
    origin = intake_services.store_posture(
        {**ENTRY, "stores": {"internal": {"remote": "git-origin"}}}, acknowledged=True
    )
    assert (legacy.published, private.published, origin.published) == (True, False, True)
    assert origin.acknowledged and not legacy.acknowledged
    assert scopes and store_target.SCOPE_PUBLIC not in scopes


# -- the composition ----------------------------------------------------------------------------


def test_the_outbox_is_off_unless_an_enabled_policy_is_passed_and_the_tracker_is_untouched(
    monkeypatch,
):
    store = MemoryReportStore(SCOPE)
    monkeypatch.setattr(intake_services, "report_store", lambda d, scope: store)
    oid = "ob1_" + "a" * 26
    for policy in (None, OutboxPolicy()):
        service = intake_services.outbox_service_for(
            CFG,
            ENTRY,
            "/hive",
            tracker=Untouchable(),
            identity=ME,
            marker_line=marker_line,
            policy=policy,
        )
        for name, argument in (
            ("send", (oid,)),
            ("approve", (OutboxApprove(oid, "0" * 12, "alice"),)),
            ("draft_reply", (ReplyDraft("acme-0001", "hello", "alice"),)),
        ):
            kwargs = {"actor": "alice"} if name == "send" else {}
            with pytest.raises(IntakeRefused) as refused:
                getattr(service, name)(*argument, **kwargs)
            assert refused.value.reason is RefusalReason.OUTBOX_DISABLED
    assert store.writes == 0


def test_an_enabled_composition_drafts_approves_and_sends_once_on_the_injected_tracker(
    monkeypatch, tmp_path
):
    world = MemoryWorld()
    issue = world.add_issue("a bug", at=at(1))
    world.set_write_time(at(2))
    tracker = MemoryTracker(world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    store = MemoryReportStore(SCOPE)
    report = store.create(
        ReportDraft(
            "a bug",
            "bug",
            "origin:github",
            "dev/import",
            binding=IssueBinding("", "memory", world.locator, issue.url, issue.identifier),
            snapshot=SourceSnapshot("a bug", "", IssueStatus.OPEN, "r1"),
        )
    )
    monkeypatch.setattr(intake_services, "report_store", lambda d, scope: store)
    monkeypatch.setattr(
        intake_services,
        "store_posture",
        lambda entry, *, acknowledged: intake_services.StorePosture(published=False),
    )
    service = intake_services.outbox_service_for(
        CFG,
        ENTRY,
        "/hive",
        tracker=tracker,
        identity=ME,
        marker_line=marker_line,
        policy=OutboxPolicy(enabled=True),
        lock=InProcessIntakeLock(),
        ids=SequentialIdSource(),
    )
    oid = service.draft_reply(ReplyDraft(report, "Thanks, fixed in the next release.", "alice"))
    shown = service.render(oid.outbox_id).renders[0]
    assert shown.findings == ()  # the shared ScrubSanitizer is bound: not held for no_sanitizer
    service.approve(OutboxApprove(oid.outbox_id, shown.short_digest, "alice"))
    sent = service.send(oid.outbox_id, actor="alice")

    assert sent.state is OpState.SENT and sent.notified == "replied"
    assert world.write_requests() == 1 and world.comment_count(issue) == 1
