"""Integration: `hive.adopt_existing` on a FRESH host, against real `bd` and `dolt` (bh-m2irb).

The bh-6csoa scenario end to end. A hive already exists elsewhere: its beads (`nvhack-*`) ride
its git remote's `refs/dolt/data`, and its zero-footprint clone carries no `.beads/` at all. A
fresh host, whose shared server already holds bd's generic `beads` database for some OTHER
store, then:

  1. `bh hive add` registers it — and with adopt on gets the store's own prefix and project id
     from the remote's data ref, not `ac-widget` derived from the repo name (bh-ajgot);
  2. `bh hive onboard` materialises it with NO `--clone-url` (the fleet config already names
     it) and lands the clone in the hive's OWN shared-server database, readable straight away —
     no manual `bd init`, no second `bd bootstrap`, the generic `beads` database untouched;
  3. a second materialisation (the database already hydrated on this host, as `bh sync`'s hub
     cache leaves it) attaches to that database instead of cloning a second copy.

Each test runs against its World's own isolated shared server (BEADS_SHARED_SERVER_DIR + a
free port, reaped by the `world` fixture), never the operator's. Marked `integration`.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from beadhive import config, hive, registry, store_identity, store_locator
from harness.beads import bd, bd_json, create, init_embedded, skip_if_no_bd
from harness.world import git

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    skip_if_no_bd,
    pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt not installed"),
]

_TIMEOUT = 120
_HIVE = "github/acme/widget"


def _source_hive(world):
    """The hive as it exists on ANOTHER host: an embedded `nvhack` store with one bead, pushed
    to the bare origin's `refs/dolt/data`. Returns (origin, project_id, bead_id)."""
    origin = world.remotes / "widget.git"
    origin.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=origin)
    src = world.tmp / "elsewhere" / "widget"
    src.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=src)
    git("config", "user.email", "t@t.dev", cwd=src)
    git("config", "user.name", "T", cwd=src)
    (src / "README.md").write_text("widget\n")
    git("add", ".", cwd=src)
    git("commit", "-q", "-m", "init", cwd=src)
    git("remote", "add", "origin", str(origin), cwd=src)
    git("push", "-q", "-u", "origin", "main", cwd=src)
    init_embedded(src, "nvhack")
    bead = create(src, "existing work")
    bd("dolt", "push", cwd=src, capture=True, timeout=_TIMEOUT)
    assert git("ls-remote", str(origin), "refs/dolt/data").stdout.strip()
    project_id = store_locator.project_id(src)
    assert project_id
    return origin, project_id, bead


def _occupy_generic_database(world):
    """Another store already holds bd's generic `beads` database on this host's shared server —
    the collision bh-6csoa hit. Minting it also starts the isolated shared server."""
    other = world.tmp / "other-store"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    from beadhive.run import run

    run(
        ["bd", "init", "--prefix", "beads", "--shared-server", "--quiet", "--skip-hooks"],
        cwd=str(other),
        check=True,
        capture=True,
        env={**os.environ, "BD_NON_INTERACTIVE": "1"},
        timeout=_TIMEOUT,
    )
    assert store_locator.dolt_database(other, "") == "beads"
    assert (store_locator.server_store_dir() / "beads").is_dir()
    return other


def _fresh_host(world, origin, *, adopt=True):
    """This host's config: adopt on (or off), and the git-workspace lock naming the hive's
    clone URL — the fleet configuration a registered hive's URL resolves from."""
    world.cfg_path.write_text(
        "providers: [github]\nmanaged_repos: []\n"
        + ("hive:\n  adopt_existing: true\n" if adopt else "")
    )
    (world.ws_root / "workspace-lock.toml").write_text(
        f'[[repo]]\npath = "{_HIVE}"\nurl = "{origin}"\nbranch = "main"\n'
    )


def _databases() -> list[str]:
    root = store_locator.server_store_dir()
    names = (p.name for p in root.iterdir())
    return sorted(name for name in names if store_locator.has_direct_server_database(name))


def _entry():
    return registry.find_entry(config.load(), "github", "acme", "widget")


def test_remote_probe_reads_the_data_ref_without_a_server(world):
    """`read_remote` is a depth-1 `dolt clone` + one embedded query: no bd, no server, nothing
    left behind in bh's cache."""
    origin, project_id, _ = _source_hive(world)

    scratch = store_identity.scratch_dir(config.cache_dir())
    found = store_identity.read_remote(str(origin), scratch=scratch, timeout=_TIMEOUT)

    assert found is not None
    assert (found.prefix, found.project_id) == ("nvhack", project_id)
    assert found.database == "nvhack"
    assert not any(scratch.iterdir()), "the probe's throwaway clone must be removed"


def test_a_remote_without_a_data_ref_has_no_identity(world):
    origin = world.remotes / "empty.git"
    origin.mkdir(parents=True)
    git("init", "-q", "--bare", "-b", "main", cwd=origin)

    scratch = world.tmp / "scratch"
    assert store_identity.read_remote(str(origin), scratch=scratch, timeout=_TIMEOUT) is None


def test_hive_add_without_adopt_still_derives(world, monkeypatch):
    origin, _, _ = _source_hive(world)
    _fresh_host(world, origin, adopt=False)

    hive.add(_HIVE, kind="personal")

    entry = _entry()
    assert str(entry["prefix"]) == "ac-widget"
    assert "project_id" not in entry


def test_fresh_host_adds_and_onboards_a_registered_hive_into_its_own_database(world, monkeypatch):
    origin, project_id, bead = _source_hive(world)
    other = _occupy_generic_database(world)
    _fresh_host(world, origin)
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: True)

    # 1. registration adopts the store's identity from the remote's data ref (bh-ajgot).
    hive.add(_HIVE, kind="personal")
    entry = _entry()
    assert str(entry["prefix"]) == "nvhack"
    assert str(entry["project_id"]) == project_id

    # 2. materialise with NO --clone-url, zero-footprint (bh-6csoa).
    target = world.ws_root / "github" / "acme" / "widget"
    plan = hive.execute_onboard(_HIVE, furnish=False, hub_sync=False)

    assert plan.successful, [(c.id, c.detail) for c in plan.failures]
    assert plan.cloned and target.is_dir()
    assert plan.prefix == "nvhack"
    clone_check = next(c for c in plan.checks if c.id == "clone-url-present")
    assert "fleet registry" in clone_check.detail
    metadata = json.loads((target / ".beads" / "metadata.json").read_text())
    assert metadata["dolt_mode"] == "server"
    assert metadata["dolt_database"] == "nvhack", "the hive's own database, not bd's generic"
    assert metadata[store_locator.SERVER_DATABASE_KEY] == "nvhack"
    assert (store_locator.server_store_dir() / "nvhack").is_dir()
    # Ready as it stands: no manual bd init / bootstrap — the existing bead is readable and the
    # store answers with its own prefix and identity.
    ids = [row["id"] for row in bd_json("list", cwd=target) or []]
    assert bead in ids
    assert bd_json("config", "list", cwd=target)["issue_prefix"] == "nvhack"
    assert create(target, "written on the fresh host").startswith("nvhack-")
    # The other store's generic database was never touched.
    assert bd_json("config", "list", cwd=other)["issue_prefix"] == "beads"
    assert bead not in [row["id"] for row in bd_json("list", cwd=other) or []]
    # Zero-footprint: nothing tracked, nothing committed.
    assert git("rev-list", "--count", "HEAD", cwd=target).stdout.strip() == "1"
    assert ".beads" not in git("status", "--porcelain", cwd=target).stdout

    # 3. a second materialisation attaches to the database already on this host.
    before = _databases()
    assert {"beads", "nvhack"} <= set(before)
    shutil.rmtree(target)
    again = hive.execute_onboard(_HIVE, furnish=False, hub_sync=False)

    assert again.successful, [(c.id, c.detail) for c in again.failures]
    metadata = json.loads((target / ".beads" / "metadata.json").read_text())
    assert metadata["dolt_database"] == "nvhack"
    assert bead in [row["id"] for row in bd_json("list", cwd=target) or []]
    assert _databases() == before, "attached to the hive's database, not a second copy"
    assert (target / ".beads" / "config.yaml").is_file(), "later `bd config set` needs it"
    assert bd_json("config", "get", "dolt.shared-server", cwd=target).get("value") == "true"
