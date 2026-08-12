"""Integration: a REAL round trip for HQ's pre-push backup against a REAL server-mode `bd`
store (bh-areg.1) — not a mock of the ``Engine`` protocol.

This is the bead's headline acceptance, demonstrated rather than merely gated: `bh hq push`
must succeed against a server-mode HQ with a backup that is VERIFIED and that a restore can
ACTUALLY CONSUME. A green `plan.ok` alone would pass against a backup that silently wrote
nothing (bh-kobw's shape) — this test proves the artifact is real by destroying the source and
recovering distinguishable content from a SECOND, independent server-mode store.

Never touches the operator's real ``~/.beadhive`` or any registered hive — every `bd init` here
runs in an isolated ``tmp_path``, matching the ``bh-u562.1``/``bh-00cq`` scratch discipline.
Owned mode (`bd init --server`) stands in for server mode generally: bh-areg.1's own notes
record that owned/shared/external are indistinguishable from this bead's perspective (all
persist ``dolt_mode: "server"``, none have `.beads/embeddeddolt`) — exercising one exercises
the code path all three share.

Marked ``integration`` (slower — spins up real Dolt sql-servers) + self-skips without a ``bd``
binary on PATH, per this repo's marker convention.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beadhive import hq, hq_restore
from harness.beads import skip_if_no_bd
from harness.world import git, reap_dolt_server

# `dolt_server`: the round trip runs TWO owned-mode servers (source and restore target), so it
# holds one of the run-wide slots `conftest._bound_concurrent_dolt_servers` hands out — and is the
# reason that bound counts TESTS rather than processes (bh-wa3ch).
pytestmark = [pytest.mark.integration, pytest.mark.dolt_server, skip_if_no_bd]

_INIT_TIMEOUT = 60


def _init_server_store(path, prefix: str):
    from beadhive.run import run

    path.mkdir(parents=True, exist_ok=True)
    run(
        ["bd", "init", "--server", "--prefix", prefix, "--non-interactive"],
        cwd=str(path),
        check=True,
        capture=True,
        timeout=_INIT_TIMEOUT,
    )


def _create_bead(path, title: str) -> str:
    from beadhive.run import run

    res = run(["bd", "-C", str(path), "q", title], check=True, capture=True)
    return (res.stdout or "").strip().splitlines()[-1].strip()


def _titles(path) -> set[str]:
    from beadhive.run import run

    res = run(["bd", "-C", str(path), "list", "--json"], check=True, capture=True)
    return {row["title"] for row in json.loads(res.stdout or "[]")}


def _reap_owned_server(path):
    """Owned mode DETACHES its server from the spawning CLI process and outlives it (measured,
    bh-u562.1 finding 1) — it does not die with this test. Kill it by the pidfile bd writes, which
    for `bd init --server` is `<store>/.beads/dolt-server.pid` (measured against a real bd, the
    same file name shared mode puts under `BEADS_SHARED_SERVER_DIR`).

    NOT `bd … dolt stop` (bh-5mc8g). That resolves the server through `.beads/metadata.json`'s
    `dolt_mode` and refuses outright in some states, and the call here passed `check=False`, so the
    refusal was swallowed and a real sql-server was left running against a tmp dir pytest then
    deleted — observed orphaned (reparented to PID 1) on the operator's machine, from this exact
    test. `reap_dolt_server` is idempotent, so an early in-body reap plus the fixture finalizer is
    a statfile check the second time round."""
    reap_dolt_server(Path(path) / ".beads")


@pytest.fixture
def owned_stores(tmp_path):
    """The two owned-mode stores this round trip needs, with a finalizer that reaps BOTH servers.

    A fixture rather than a `finally` because the finalizer runs even when the test fails partway
    or the run is interrupted between the two — matching `test_hub_bulk_int.py` /
    `test_onboard_server_mode_int.py`, the two files bh-cbou already migrated."""
    src = tmp_path / "hq-src"
    dst = tmp_path / "hq-dst"
    yield src, dst
    _reap_owned_server(src)
    _reap_owned_server(dst)


def test_server_mode_hq_backup_and_restore_real_round_trip(tmp_path, monkeypatch, owned_stores):
    src, dst = owned_stores
    _init_server_store(src, "hqown")
    _create_bead(src, "real bead one")
    _create_bead(src, "real bead two")
    live_titles = _titles(src)
    assert live_titles == {"real bead one", "real bead two"}

    # `bd init --server` already git-inits and commits its own scaffolding (verified
    # against a real bd binary) — `_take_backup`'s third level (remote-dolt-data-ref) just
    # needs SOME git_url; a fresh local bare remote has nothing pre-existing to protect, so
    # that level trivially verifies without a network call.
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)

    backup_dir = tmp_path / "hq-backups" / "2026-08-03"
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: backup_dir.parent)

    plan = hq._take_backup(src, str(remote), {}, dry_run=False)

    assert plan.ok, [(t.name, t.verified, t.detail) for t in plan.targets]
    names = [t.name for t in plan.targets]
    assert "dolt-native-backup" in names  # the connection-oriented level actually ran
    assert "embeddeddolt-tar" not in names  # never attempted — no embeddeddolt off embedded

    # Now DESTROY the source entirely — the scenario a restore recovers from. Reap its server
    # FIRST: the rmtree below takes the pidfile with it, so a reap deferred to the fixture
    # finalizer would have nothing left to find and the server would outlive the run.
    _reap_owned_server(src)
    import shutil

    shutil.rmtree(src)
    assert not src.exists()

    # A fresh, independent server-mode store — the restore TARGET, standing in for a
    # second host / a rebuilt HQ.
    _init_server_store(dst, "hqown")
    assert _titles(dst) == set()

    monkeypatch.setattr(hq_restore.config, "hq_dir", lambda: dst)
    backups = hq_restore.list_backups({})
    assert backups, "the backup just taken must be discoverable"
    backup = backups[0]
    assert backup.native is not None
    assert backup.tar is None  # never produced off embedded mode

    out = hq_restore.restore({}, backup, level="auto", dry_run=False, confirm=True)

    assert out.ok, out.actions
    assert out.level == "tar"  # the public full-fidelity level name — via `native` here
    assert _titles(dst) == live_titles  # the REAL round trip: content actually came back
