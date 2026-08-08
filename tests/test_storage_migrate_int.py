"""Integration: a REAL round trip for the storage-migration verb (bh-areg.4) against a REAL
bd binary — embedded -> shared server, not a mock of the Engine protocol.

Proves the two things a mock can't: (1) `bd init --shared-server --reinit-local` really does
leave `.beads/metadata.json`'s `dolt_mode` at "embedded" (the exact drift constraint 1 exists
to close, measured here rather than merely asserted in a docstring); (2) `migrate_hive` closes
that drift and the issue data survives the mode change intact (same count, same content), with
`backup.enabled` explicitly turned back on (constraint 2) and a second run a clean no-op.

ALSO proves bh-oa225's own measurement: the shape EVERY hive on the real fleet is actually in —
a LIVE embedded store whose git `origin` already carries `refs/dolt/data` (because pushing bead
state creates it, the fleet's own durability practice) — which `_reinit_shared_server` alone
always refuses against. `migrate_hive` must select `bd bootstrap` instead here, and the bootstrap
call itself is measured (not assumed) to silently DISCARD anything in the live embedded store
that was never pushed; the existing post-mechanism backup/restore step is what brings it back.

Runs against an ISOLATED shared-server instance (its own `BEADS_SHARED_SERVER_DIR` + a free TCP
port), never the operator's real `~/.beads/shared-server/` — the same scratch discipline every
other real-bd test in this suite follows (`bh-u562.1`/`bh-00cq`).

Marked `integration` (slower — spins up a real Dolt sql-server) + self-skips without a `bd`
binary on PATH.

GAP, noted honestly per this bead's acceptance: this proves the mechanism against a small,
isolated store. It does NOT exercise a live 22-hive fleet or the operator's real 1.6 GB HQ —
infeasible inside a dev worktree. `migrate_fleet`'s ordering/resumability/isolation logic is
covered separately (fast, deterministic) in `test_storage_migrate.py`.
"""

from __future__ import annotations

import json

import pytest

from beadhive import storage_migrate
from harness.beads import skip_if_no_bd
from harness.world import (
    free_port,
    git,  # noqa: F401 - re-exported for parity with sibling int tests
    reap_dolt_server,
)

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_TIMEOUT = 60


def _init_embedded(path, prefix):
    from beadhive.run import run

    path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True, capture=True)
    run(
        ["bd", "init", "--prefix", prefix, "--non-interactive"],
        cwd=str(path),
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )


def _init_embedded_with_pushed_remote(world, path, prefix):
    """The shape bh-oa225 measured EVERY hive on the real fleet to actually be in: a LIVE
    embedded store whose git `origin` already carries `refs/dolt/data` — because pushing bead
    state creates it (`bd dolt push`, the durability practice the fleet already follows), not
    because the hive is otherwise special. `_init_embedded` alone (no remote at all) is the
    easy case `_reinit_shared_server` already handles; this is the one it refuses."""
    from beadhive.run import run

    _init_embedded(path, prefix)
    git("config", "user.email", world.human.email, cwd=path)
    git("config", "user.name", world.human.name, cwd=path)
    (path / "README.md").write_text("# hive\n")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "chore: init", cwd=path)

    remote = world.remotes / f"{prefix}.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=world.remotes)
    git("remote", "add", "origin", str(remote), cwd=path)
    git("push", "-q", "origin", "main", cwd=path)

    run(
        ["bd", "-C", str(path), "dolt", "remote", "add", "origin", f"git+file://{remote}"],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    run(
        ["bd", "-C", str(path), "dolt", "push"],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    return remote


def _create(path, title) -> str:
    from beadhive.run import run

    res = run(["bd", "-C", str(path), "q", title], check=True, capture=True)
    return (res.stdout or "").strip().splitlines()[-1].strip()


def _titles(path) -> set[str]:
    from beadhive.run import run

    res = run(["bd", "-C", str(path), "list", "--json"], check=True, capture=True)
    return {row["title"] for row in json.loads(res.stdout or "[]")}


def _config_get(path, key):
    from beadhive.run import run

    res = run(["bd", "-C", str(path), "config", "get", key, "--json"], check=True, capture=True)
    return json.loads(res.stdout or "{}")


@pytest.fixture
def isolated_shared_server(tmp_path, monkeypatch):
    """This test's OWN shared-server instance, at its own data dir and a free port — never the
    operator's real `~/.beads/shared-server/` — reaped when the test ends however it ends.

    The teardown used to be a per-test `finally` running `bd -C <hive> dolt stop`, and it NEVER
    WORKED (bh-cbou). bd resolves the server from `.beads/metadata.json`'s `dolt_mode`, and the
    first test here exists precisely to prove `--reinit-local` leaves that stale at "embedded",
    so bd answered `'bd dolt stop' is not supported in embedded mode (no Dolt server)` and
    exited 1 while the server ran on. The call was `check=False`, so the refusal was silent and
    a server accumulated on every suite run — 16 of them by the time it was measured. Reaping by
    the pidfile in THIS fixture's own dir does not consult bd's view of the world at all."""
    server_dir = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(free_port()))
    yield
    reap_dolt_server(server_dir)


def test_bd_reinit_local_shared_server_leaves_metadata_stale_by_itself(
    world, isolated_shared_server
):
    """The exact drift constraint 1 exists to close, measured against a real bd binary: `bd
    init --shared-server --reinit-local` activates the shared server for this project WITHOUT
    updating `dolt_mode` in metadata.json — bd's own `warnSharedServerEmbeddedMismatch` warns
    about exactly this and recommends persisting it by hand, but does not do so itself."""
    hive_dir = world.ws_root / "github" / "acme" / "drift"
    _init_embedded(hive_dir, "drft")
    _create(hive_dir, "an issue")
    res = storage_migrate._reinit_shared_server(hive_dir, "drft", "drft", "test")
    assert res.returncode == 0, res.stderr

    metadata = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata["dolt_mode"] == "embedded"  # the drift, unfixed — proves the hazard


def test_embedded_to_shared_server_real_round_trip(world, isolated_shared_server):
    hive_dir = world.ws_root / "github" / "acme" / "widget"
    _init_embedded(hive_dir, "wgt")
    _create(hive_dir, "real issue one")
    _create(hive_dir, "real issue two")
    live_titles = _titles(hive_dir)
    assert live_titles == {"real issue one", "real issue two"}

    metadata_before = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata_before["dolt_mode"] == "embedded"

    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "wgt",
        "kind": "personal",
    }
    cfg = {"managed_repos": [entry]}

    result = storage_migrate.migrate_hive(entry, cfg, dry_run=False, actor="test")

    assert result.status == "migrated", (result.detail, result.backup_plan)
    assert result.pre_issue_count == 2
    assert result.post_issue_count == 2
    assert result.dolt_mode == "server"

    # constraint 1: dolt_mode really persisted into metadata.json itself.
    metadata_after = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata_after["dolt_mode"] == "server"

    # the actual round trip: content came back, not just a green status.
    assert _titles(hive_dir) == live_titles

    # constraint 2: backup.enabled explicitly turned back on (defaults OFF in shared-server
    # mode even though it was ON in embedded, per `bd backup --help`).
    assert _config_get(hive_dir, "backup.enabled").get("value") is True

    # re-running is a clean no-op, not an error.
    result2 = storage_migrate.migrate_hive(entry, cfg, dry_run=False, actor="test")
    assert result2.status == "already-migrated"
    assert _titles(hive_dir) == live_titles  # unchanged by the no-op re-run


def test_dry_run_against_a_real_embedded_store_changes_nothing(world, isolated_shared_server):
    hive_dir = world.ws_root / "github" / "acme" / "dryrun"
    _init_embedded(hive_dir, "dry1")
    _create(hive_dir, "an issue")
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "dryrun",
        "prefix": "dry1",
        "kind": "personal",
    }
    cfg = {"managed_repos": [entry]}

    result = storage_migrate.migrate_hive(entry, cfg, dry_run=True)

    assert result.status == "would-migrate"
    assert result.mechanism == "reinit"  # no remote configured at all — nothing to bootstrap from
    assert result.size_bytes > 0
    assert result.target_path.endswith("dolt/dry1")
    metadata = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata["dolt_mode"] == "embedded"  # untouched


# ---- bh-oa225: the shape EVERY hive on the real fleet is actually in ------------------------


def test_dry_run_selects_bootstrap_when_origin_already_has_dolt_data(world, isolated_shared_server):
    """The dry-run must stop lying (bh-oa225 acceptance): previously this always reported
    `would-migrate` for reinit, an operation `bd` itself refuses whenever the remote already
    carries `refs/dolt/data` — which every hive on the real fleet does. It must now select and
    report bootstrap instead, and change nothing."""
    hive_dir = world.ws_root / "github" / "acme" / "pushed-dry"
    _init_embedded_with_pushed_remote(world, hive_dir, "pdry")
    _create(hive_dir, "an issue")
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "pushed-dry",
        "prefix": "pdry",
        "kind": "personal",
    }
    cfg = {"managed_repos": [entry]}

    result = storage_migrate.migrate_hive(entry, cfg, dry_run=True)

    assert result.status == "would-migrate"
    assert result.mechanism == "bootstrap"
    metadata = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata["dolt_mode"] == "embedded"  # untouched


def test_bootstrap_migration_survives_a_live_embedded_store_with_unpushed_changes(
    world, isolated_shared_server
):
    """The full proof bh-oa225 is about, end to end against a real bd binary: a hive whose
    remote already carries `refs/dolt/data` (the shape `_reinit_shared_server` alone always
    refuses against) migrates successfully via `bd bootstrap`, AND an issue created AFTER the
    last `bd dolt push` — the exact case bootstrap alone silently drops, measured directly for
    this bead — survives the migration because the pre-migration native backup is restored on
    top of the freshly-bootstrapped store."""
    hive_dir = world.ws_root / "github" / "acme" / "pushed-live"
    _init_embedded_with_pushed_remote(world, hive_dir, "plv")
    _create(hive_dir, "pushed issue one")
    _create(hive_dir, "pushed issue two")
    from beadhive.run import run

    run(
        ["bd", "-C", str(hive_dir), "dolt", "push"],
        check=True,
        capture=True,
        timeout=_TIMEOUT,
    )
    # Created AFTER the push above — must survive even though bootstrap alone would drop it.
    _create(hive_dir, "unpushed issue three")
    live_titles = _titles(hive_dir)
    assert live_titles == {"pushed issue one", "pushed issue two", "unpushed issue three"}

    metadata_before = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata_before["dolt_mode"] == "embedded"

    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "pushed-live",
        "prefix": "plv",
        "kind": "personal",
    }
    cfg = {"managed_repos": [entry]}

    result = storage_migrate.migrate_hive(entry, cfg, dry_run=False, actor="test")

    assert result.status == "migrated", (result.detail, result.backup_plan)
    assert result.mechanism == "bootstrap"
    assert result.pre_issue_count == 3
    assert result.post_issue_count == 3
    assert result.dolt_mode == "server"

    metadata_after = json.loads((hive_dir / ".beads" / "metadata.json").read_text())
    assert metadata_after["dolt_mode"] == "server"

    # the actual round trip: EVERY issue came back, unpushed one included — not just a green
    # status (bh-00cq's own lesson, restated for the bootstrap mechanism specifically).
    assert _titles(hive_dir) == live_titles

    assert _config_get(hive_dir, "backup.enabled").get("value") is True

    # re-running is a clean no-op, not an error, and stays off the reinit/bootstrap fork
    # entirely (already-migrated hives short-circuit before mechanism selection).
    result2 = storage_migrate.migrate_hive(entry, cfg, dry_run=False, actor="test")
    assert result2.status == "already-migrated"
    assert _titles(hive_dir) == live_titles
