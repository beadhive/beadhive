"""Per-case authority mode for the hq-service conformance runners (bh-4m061, bh-mk97e).

A corpus case may carry ``"authority_mode": "trusted" | "enforced"`` (absent: the case runs exactly
as before). The mode is selected for THAT case only, through pytest's ``monkeypatch`` (so it is
undone at the case's teardown, also under ``-n``, where each worker runs one case at a time) and
never through process-global state that outlives the case: ``BH_HQ_AUTHORITY_MODE`` is pinned
explicitly (it outranks host config, the deprecated enforce switch and any learned fleet default),
``BH_HOME`` points at a directory that does not exist (so no host fleet-default is learned or
written), and the per-process mode caches are reset on entry. Any subprocess a runner spawns
inherits the pinned environment.

A case with a mode runs the receivers against the REAL ``SqlRuntimeAuthority.verified_state_at`` /
``bound_config_at`` (the default runners stub them with always-verifying fakes, which would hide the
mode). Only the I/O seams stay stubbed: the rollback floor file, the config/runtime head fences,
the tolerated-head read (the config-head tolerance corpus owns that) and the latest-config read,
which comes from the case's config rows. ``config_head_read`` (optional) names the head the latest
config read returns when it differs from the head the signed authority names.
"""

import os
import tempfile
from pathlib import Path

from beadhive import hq_authority_enforce
from beadhive.hq_sql_config import SqlFleetConfigRevisionStore
from beadhive.hq_sql_runtime import SqlRuntimeAuthority
from beadhive.modules.config.domain.ports import FleetConfigSnapshot

MODES = {"trusted": "trusted", "enforced": "signed"}


def pin(case: dict, monkeypatch) -> str | None:
    """Pin the case's authority mode for this case only; no field: change nothing."""
    hq_authority_enforce.reset_cache()
    mode = case.get("authority_mode")
    if mode is None:
        return None
    assert mode in MODES, f"authority_mode must be one of {sorted(MODES)}, got {mode!r}"
    monkeypatch.setenv(hq_authority_enforce.MODE_ENV, MODES[mode])
    monkeypatch.delenv(hq_authority_enforce.ENFORCE_ENV, raising=False)
    monkeypatch.delenv("BH_HQ_OPERATOR_SETTINGS", raising=False)
    nohome = Path(tempfile.gettempdir()) / f"hq-conformance-nohome-{os.getpid()}"
    monkeypatch.setenv("BH_HOME", str(nohome))
    hq_authority_enforce.reset_cache()
    return mode


def real_authority(case: dict, clock):
    """The real runtime authority over the case's rows, with only I/O seams stubbed."""
    row = case["rows"]["hq_authority"][0]
    authority = SqlRuntimeAuthority(
        {
            "runtime": {"user": "observer", "database": "authority", "operation_timeout": 15},
            "reader": {"database": "config"},
            "runtime_backend_identity": row["backend_identity"],
            "runtime_generation": row["generation"],
            "runtime_operator_public_key": case["trust"]["operator_public_key"],
            "runtime_floor_path": "/nonexistent/floor.json",
            "runtime_initial_revision": "head-1",
        },
        clock=clock,
    )
    read_head = case.get("config_head_read", row["config_head"])

    def latest(cursor, deadline=None):
        backend, generation, _sequence, documents, _version = (
            SqlFleetConfigRevisionStore._committed(cursor, read_head)
        )
        now = clock()
        return FleetConfigSnapshot(
            "sql:" + backend, read_head, generation, now - 50, now + 10**6, documents
        )

    authority._check_floor = lambda *a, **k: None
    authority.load_latest_config_at = latest
    authority._tolerated_bound_config = lambda *a, **k: None
    authority.fresh_config_head_fence = lambda *a, **k: None
    authority.fresh_runtime_head_fence = lambda *a, **k: None
    return authority


def install(world, case: dict) -> None:
    """Swap the receiver's stubbed authority for the real one (a no-op without a mode)."""
    if case.get("authority_mode") is not None:
        # authority_mode and config_move are exclusive: the first selects the real authority's
        # mode, the second models a moved config head with its own authority.
        assert "config_move" not in case, "a case sets authority_mode or config_move, not both"
        world.receiver.authority = real_authority(case, world.receiver.clock)


__all__ = ["MODES", "install", "pin", "real_authority"]
