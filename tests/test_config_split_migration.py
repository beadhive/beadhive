"""config_split_migration.py — the one-time flat config.yaml -> fleet.yaml + host config.yaml
split (bh-e0y8.7).

Builds directly on the fleet/host partition (bh-e0y8.3's `config_partition.partition_of`) and
the fleet/host merge (bh-e0y8.5's `config.py` load_fleet/load_host/_deep_merge) rather than
reimplementing either. Covers the AC:

- a flat config splits into fleet.yaml + a reduced host config.yaml, per the partition
- re-running on an already-split install is a no-op
- `--dry-run` prints the exact split and writes nothing
- the original file is backed up (`.bak`) before anything is overwritten
- the split ROUND-TRIPS a representative real-world config: `_deep_merge(fleet, host)`
  reproduces the original flat config exactly
- an unclassified key (`partition_of` -> None, e.g. the un-schema'd `beads` section) is
  preserved on the host side rather than dropped
"""

from __future__ import annotations

import pytest

from beadhive import config, config_partition, config_split_migration

# A representative real-world flat config.yaml: a mix of FLEET keys (delimiter, orgs,
# dimensions, work.validate_cmd, work.dispatch.mode, managed_repos), HOST keys (otel,
# worktrees.path, hq.remote, work.identity, work.dispatch.max_beads_per_session, dolt), and
# one UNCLASSIFIED section (`beads` — not part of config_schema.BeadhiveConfig at all, per
# `test_host_only_and_unclassified_keys_are_never_rejected` in test_config_fleet_merge.py).
FLAT_YAML = """\
schema_version: 1
delimiter: ':'
providers: [github]
managed_repos: []
exclude:
  orgs: []
  repos: []
orgs:
  acme: {code: ac, policy: required}
dimensions:
  component:
    description: Intra-project functional area. Open set.
worktrees:
  ephemeral: true
  path: ~/.beadhive/worktrees
work:
  validate_cmd: just check
  review_gate: human
  identity:
    name: dev/ada
  dispatch:
    mode: fanout
    max_beads_per_session: 3
otel:
  enabled: true
  endpoint: http://localhost:4317
hq:
  remote: acme/beadhive-hq
dolt:
  backend: colima
beads:
  engine: bd
"""


@pytest.fixture
def bh_home(tmp_path, monkeypatch):
    """An isolated BH_HOME with an HQ store dir (mirrors test_config_fleet_merge.py's
    fixture) and a flat, pre-split config.yaml already written."""
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    monkeypatch.setenv("BH_HOME", str(home))
    for var in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ"):
        monkeypatch.delenv(var, raising=False)
    (home / "config.yaml").write_text(FLAT_YAML)
    return home


def _backup_path(home):
    return home / "config.yaml.bak"


# ---- split_leaves (pure) -----------------------------------------------------------


def test_split_leaves_puts_fleet_keys_in_the_fleet_portion():
    flat = config._yaml.load(FLAT_YAML)

    fleet_portion, _host_portion = config_split_migration.split_leaves(flat)

    assert fleet_portion["delimiter"] == ":"
    assert fleet_portion["orgs"]["acme"]["code"] == "ac"
    assert fleet_portion["work"]["validate_cmd"] == "just check"
    assert fleet_portion["work"]["dispatch"]["mode"] == "fanout"
    assert "identity" not in fleet_portion.get("work", {})
    assert "max_beads_per_session" not in fleet_portion.get("work", {}).get("dispatch", {})


def test_split_leaves_puts_host_keys_in_the_host_portion():
    flat = config._yaml.load(FLAT_YAML)

    _fleet_portion, host_portion = config_split_migration.split_leaves(flat)

    assert host_portion["otel"]["endpoint"] == "http://localhost:4317"
    assert host_portion["worktrees"]["path"] == "~/.beadhive/worktrees"
    assert host_portion["hq"]["remote"] == "acme/beadhive-hq"
    assert host_portion["work"]["identity"]["name"] == "dev/ada"
    assert host_portion["work"]["dispatch"]["max_beads_per_session"] == 3
    assert "validate_cmd" not in host_portion.get("work", {})


def test_split_leaves_keeps_an_unclassified_key_on_the_host_side():
    """`beads.engine` is not part of config_schema at all — partition_of returns None for it.
    Unclassified is not a licence to drop the value (mirrors fleet_override_violations'
    existing rule): it must land somewhere, and HOST (never silently promoted to fleet-wide
    truth) is the safe side."""
    flat = config._yaml.load(FLAT_YAML)
    assert config_partition.partition_of("beads.engine") is None  # pin the premise

    fleet_portion, host_portion = config_split_migration.split_leaves(flat)

    assert host_portion["beads"]["engine"] == "bd"
    assert "beads" not in fleet_portion


def test_split_leaves_round_trips_via_deep_merge():
    """The core correctness property, exercised directly on the pure function: merging the
    two portions back together reproduces the original exactly."""
    flat = config._yaml.load(FLAT_YAML)

    fleet_portion, host_portion = config_split_migration.split_leaves(flat)
    merged = config._deep_merge(fleet_portion, host_portion)

    assert merged == flat


# ---- needs_split (idempotency check) -----------------------------------------------


def test_needs_split_true_for_a_flat_config():
    flat = config._yaml.load(FLAT_YAML)
    assert config_split_migration.needs_split(flat)


def test_needs_split_false_once_reduced_to_host_only():
    flat = config._yaml.load(FLAT_YAML)
    _fleet_portion, host_portion = config_split_migration.split_leaves(flat)
    assert not config_split_migration.needs_split(host_portion)


def test_needs_split_false_for_an_unclassified_only_config():
    """A config with nothing but unclassified keys never needed a fleet key moved out."""
    assert not config_split_migration.needs_split({"beads": {"engine": "bd"}})


# ---- split_flat_config: the real end-to-end migration --------------------------------


def test_split_flat_config_writes_fleet_and_reduces_host(bh_home):
    config_split_migration.split_flat_config()

    fleet = config.load_fleet()
    host = config.load_host()
    assert fleet["delimiter"] == ":"
    assert fleet["work"]["validate_cmd"] == "just check"
    assert "delimiter" not in host
    assert host["otel"]["endpoint"] == "http://localhost:4317"
    assert host["work"]["identity"]["name"] == "dev/ada"


def test_split_flat_config_backs_up_the_original(bh_home):
    original = (bh_home / "config.yaml").read_text()

    config_split_migration.split_flat_config()

    assert _backup_path(bh_home).exists()
    assert _backup_path(bh_home).read_text() == original


def test_split_flat_config_round_trips_the_real_world_config(bh_home):
    """AC: the split round-trips a representative real-world config — merging the two written
    files back together reproduces the original exactly. This is the explicit round-trip
    assertion the bead's acceptance bar requires."""
    original = config._yaml.load(FLAT_YAML)

    config_split_migration.split_flat_config()

    merged = config._deep_merge(config.load_fleet(), config.load_host())
    assert merged == original


def test_split_flat_config_dry_run_writes_nothing(bh_home, capsys):
    original = (bh_home / "config.yaml").read_text()

    config_split_migration.split_flat_config(dry_run=True)

    assert (bh_home / "config.yaml").read_text() == original  # host file untouched
    assert not (bh_home / "hq" / "fleet.yaml").exists()  # fleet.yaml never created
    assert not _backup_path(bh_home).exists()  # no backup taken either


def test_split_flat_config_dry_run_prints_the_exact_split(bh_home, capsys):
    config_split_migration.split_flat_config(dry_run=True)

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "delimiter" in out  # fleet content shown
    assert "otel" in out  # host content shown


def test_split_flat_config_is_idempotent(bh_home, capsys):
    config_split_migration.split_flat_config()
    fleet_after_first = (bh_home / "hq" / "fleet.yaml").read_text()
    host_after_first = (bh_home / "config.yaml").read_text()
    capsys.readouterr()

    config_split_migration.split_flat_config()  # second call: must be a clean no-op

    out = capsys.readouterr().out
    assert "already split" in out
    assert (bh_home / "hq" / "fleet.yaml").read_text() == fleet_after_first
    assert (bh_home / "config.yaml").read_text() == host_after_first


def test_split_flat_config_noop_when_no_config_file(tmp_path, monkeypatch):
    home = tmp_path / "bh-home-empty"
    home.mkdir()
    monkeypatch.setenv("BH_HOME", str(home))
    for var in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ"):
        monkeypatch.delenv(var, raising=False)

    config_split_migration.split_flat_config()  # must not raise

    assert not (home / "config.yaml").exists()
    assert not (home / "hq" / "fleet.yaml").exists()


def test_split_flat_config_merges_onto_an_existing_fleet_base(bh_home):
    """A second host running this migration folds its own fleet keys into whatever fleet.yaml
    already exists (e.g. from a first host's earlier migration) rather than discarding it —
    a key ONLY the existing fleet base sets (not in this host's own flat config) survives."""
    (bh_home / "hq" / "fleet.yaml").write_text("release: {branch: main}\n")

    config_split_migration.split_flat_config()

    fleet = config.load_fleet()
    assert fleet["release"]["branch"] == "main"  # preserved, not clobbered
    assert fleet["delimiter"] == ":"  # this host's own fleet key still lands
