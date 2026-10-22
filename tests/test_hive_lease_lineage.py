"""A hive lease survives any number of reviewed active rotations (bh-yh8sk).

Under signed/session liveness nothing renews or rebinds a lease, so it keeps the grant
snapshot that minted it while ``release_upgrade_history`` is bounded
(``frame_release_upgrade.HISTORY_LIMIT``). The incumbent must stay recognised by lineage.
"""

import copy

import pytest

from beadhive import frame_release_upgrade as upgrade
from beadhive import hq_authority_guard as guard
from beadhive.hq_framelease_contracts import HeartbeatLease
from beadhive.hq_sql_receiver import ReceiverError
from beadhive.hq_sql_runtime_schema import inbox_table

# Reuse the in-memory SQL receiver world; only the signature verifiers are stubbed there.
from test_emergency_sql_receiver import NOW
from test_emergency_sql_receiver import world as emergency_world  # noqa: F401

ROTATIONS = upgrade.HISTORY_LIMIT + 3


def _rotated(world_record, rotations=ROTATIONS):
    """The current record after ``rotations`` active rotations, with a bounded archive."""
    minted = {**copy.deepcopy(world_record["authority"]), "config_revision": "minted-rev"}
    record = copy.deepcopy(world_record)
    history = []
    for _ in range(rotations):
        archived = {k: v for k, v in record.items() if k != "release_upgrade_history"}
        history.append(
            {
                "authority_revision": "r",
                "config_head": "c",
                "plan_sha256": "sha256:" + "0" * 64,
                "record": archived,
            }
        )
        record["authority"] = {
            **record["authority"],
            "epoch": record["authority"]["epoch"] + 1,
        }
        history = history[-upgrade.HISTORY_LIMIT :]
        record["release_upgrade_history"] = history
    return {"frame_id": record["authority"]["frame_id"], **minted}, record


@pytest.fixture
def lineage(emergency_world):  # noqa: F811
    world = emergency_world
    world.record.pop("emergency", None)
    minted, record = _rotated(world.record)
    # The minting grant fell out of the bounded archive: only lineage can carry it.
    assert all(
        {"frame_id": "factory", **i["record"]["authority"]} != minted
        for i in record["release_upgrade_history"]
    )
    return world, minted, record


def test_incumbent_lease_survives_more_rotations_than_the_archive_holds(lineage):
    _world, minted, record = lineage
    assert guard.same_incumbent_after_rotation(minted, "factory", record)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, r: p.update(frame_id="other"),
        lambda p, r: p.update(key_fingerprint="SHA256:other"),
        lambda p, r: p.update(holder_identity="other-host"),
        lambda p, r: p.update(instance_ref="other"),
        lambda p, r: p.update(audience="other"),
        lambda p, r: p.update(beadyard_id="other"),
        lambda p, r: p.update(epoch=r["authority"]["epoch"]),
        lambda p, r: p.update(epoch=r["authority"]["epoch"] + 1),
        lambda p, r: p.update(epoch=True),
        lambda p, r: p.pop("config_revision"),
        lambda p, r: p.update(extra="x"),
        lambda p, r: r.update(state="draining"),
        lambda p, r: r.update(state="retired"),
        lambda p, r: r.update(state="pending"),
        lambda p, r: r.update(emergency={"reason": "x"}),
    ],
)
def test_lineage_refuses_foreign_newer_or_inactive(lineage, mutate):
    _world, minted, record = lineage
    mutate(minted, record)
    assert not guard.same_incumbent_after_rotation(minted, "factory", record)


def test_lineage_accepts_a_lease_minted_before_the_grant_was_bound_to_a_beadyard(lineage):
    _world, minted, record = lineage
    del minted["beadyard_id"]
    assert guard.same_incumbent_after_rotation(minted, "factory", record)


def test_lineage_does_not_serve_another_frame(lineage):
    _world, minted, record = lineage
    assert not guard.same_incumbent_after_rotation(minted, "other-frame", record)


def _conformant(world):
    world.beat["conformance"] = {"profile": "reviewed", "status": "conformant", "checks": []}
    world.beat["state_seen"] = "active"
    world.beat["leaseDurationSeconds"] = 900
    world.beat = HeartbeatLease.model_validate(world.beat).model_dump(
        mode="json", exclude_none=True
    )


def _arm_receiver(world, record):
    """Make the receiver's route, request and beat agree with the rotated record."""
    epoch = record["authority"]["epoch"]
    world.state["frames"]["factory"]["active"] = record
    world.record = record
    world.request["epoch"] = epoch
    world.request["config_revision"] = record["authority"]["config_revision"]
    world.beat["epoch"] = epoch
    world.beat["config_revision"] = record["authority"]["config_revision"]
    world.route = (*world.route[:4], epoch, inbox_table("frame", epoch), *world.route[6:])
    _conformant(world)
    world.receiver.clock = lambda: NOW - 700  # inside the receipt floor's 900s lease


def test_receiver_renews_the_lease_minted_many_rotations_ago(lineage):
    world, minted, record = lineage
    _arm_receiver(world, record)
    world.prior["authority"] = minted
    assert world.receiver.accept_hive_lease("frame", "request")
    assert world.committed


def test_receiver_refuses_renewal_for_a_foreign_lease_authority(lineage):
    world, minted, record = lineage
    _arm_receiver(world, record)
    world.prior["authority"] = {**minted, "key_fingerprint": "SHA256:other"}
    with pytest.raises(ReceiverError, match="exact incumbent"):
        world.receiver.accept_hive_lease("frame", "request")
    assert not world.committed
