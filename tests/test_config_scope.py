"""`bh config get|set|unset --scope fleet|host`, and `bh config show` provenance (bh-e0y8.6).

Builds directly on bh-e0y8.5's fleet/host split (`config.py`'s `load_fleet`/`load_host`/
`fleet_path`/`_reject_fleet_overrides`) and bh-e0y8.3's partition data
(`config_partition.partition_of`) rather than reimplementing either:

- `--scope host` reads/writes the host's own `config.yaml` (`load_host`/`save`) — the existing
  default `set`/`unset` behavior, now explicit and selectable.
- `--scope fleet` reads/writes the HQ working copy's `fleet.yaml` (`load_fleet`/`save_fleet`) —
  local-only, no commit/push (that's `bh hq push`'s job).
- a `--scope host` set of a non-allowlisted FLEET key is refused with the exact message
  `config.load()` raises for the same key (`_reject_fleet_overrides`, reused verbatim).
- `config.key_provenance()` (the data `bh config show`'s new "# Provenance" section renders)
  labels each leaf key fleet-only / host-only / override, at the same leaf granularity
  `fleet_override_violations` walks.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from beadhive import config, config_partition
from beadhive.cli import app

# A minimal, schema-valid fleet base + host layer — same shape test_config_fleet_merge.py uses,
# trimmed to just what this file's tests need.
FLEET_YAML = """\
schema_version: 1
delimiter: ':'
providers: [github]
managed_repos: []
exclude:
  orgs: []
  repos: []
worktrees:
  ephemeral: true
work:
  validate_cmd: just check
  review_gate: human
"""

HOST_YAML = """\
otel:
  enabled: true
work:
  identity:
    name: dev/ada
"""


@pytest.fixture
def bh_home(tmp_path, monkeypatch):
    """An isolated BH_HOME with an HQ store dir, so this test owns both the fleet.yaml and
    config.yaml files scope routing reads/writes. Mirrors test_config_fleet_merge.py's fixture."""
    home = tmp_path / "bh-home"
    (home / "hq").mkdir(parents=True)
    monkeypatch.setenv("BH_HOME", str(home))
    for var in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ"):
        monkeypatch.delenv(var, raising=False)
    return home


def _write_fleet(home, text: str) -> None:
    (home / "hq" / "fleet.yaml").write_text(text)


def _write_host(home, text: str) -> None:
    (home / "config.yaml").write_text(text)


# ---- get --scope --------------------------------------------------------------


def test_get_scope_host_reads_only_the_host_file(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.get_value("otel.enabled", scope=config.SCOPE_HOST)
    assert res == {"ok": True, "problems": [], "value": True}

    # a fleet-only key is invisible in host scope
    assert config.get_value("delimiter", scope=config.SCOPE_HOST)["ok"] is False


def test_get_scope_fleet_reads_only_the_fleet_file(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.get_value("delimiter", scope=config.SCOPE_FLEET)
    assert res == {"ok": True, "problems": [], "value": ":"}

    # a host-only key is invisible in fleet scope
    assert config.get_value("otel.enabled", scope=config.SCOPE_FLEET)["ok"] is False


def test_get_default_scope_is_still_the_merged_view(bh_home):
    """No `--scope` is unchanged behavior: the merged effective config."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    assert config.get_value("delimiter")["value"] == ":"
    assert config.get_value("otel.enabled")["value"] is True


def test_cli_get_scope_fleet_and_host(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)
    runner = CliRunner()

    r = runner.invoke(app, ["config", "get", "delimiter", "--scope", "fleet"])
    assert r.exit_code == 0 and r.stdout.strip() == ":"

    r = runner.invoke(app, ["config", "get", "otel.enabled", "--scope", "host"])
    assert r.exit_code == 0 and r.stdout.strip() == "true"

    # cross-scope lookup fails: a fleet-only key isn't visible under --scope host
    r = runner.invoke(app, ["config", "get", "delimiter", "--scope", "host"])
    assert r.exit_code == 1


def test_cli_get_rejects_unknown_scope(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    r = CliRunner().invoke(app, ["config", "get", "otel.enabled", "--scope", "bogus"])
    assert r.exit_code == 1
    assert "--scope" in r.output


# ---- set --scope --------------------------------------------------------------


def test_set_scope_host_writes_only_the_host_file(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.set_value("otel.protocol", "http/protobuf", scope=config.SCOPE_HOST)
    assert res["ok"] is True

    host_text = (bh_home / "config.yaml").read_text()
    assert "protocol: http/protobuf" in host_text
    fleet_text = (bh_home / "hq" / "fleet.yaml").read_text()
    assert "protocol" not in fleet_text  # never touched the fleet file


def test_set_default_scope_is_still_host(bh_home):
    """AC: `--scope host` writes local config — and that was already `set`'s only behavior
    before this bead, so the DEFAULT (no `--scope`) must still target the host file."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.set_value("otel.protocol", "http/protobuf")
    assert res["ok"] is True
    assert "protocol: http/protobuf" in (bh_home / "config.yaml").read_text()


def test_set_scope_fleet_writes_only_the_fleet_file(bh_home):
    """AC: `--scope fleet` writes into the HQ working copy (fleet.yaml), never the host file."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.set_value("release.strategy", "stable-versioning", scope=config.SCOPE_FLEET)
    assert res["ok"] is True

    fleet_text = (bh_home / "hq" / "fleet.yaml").read_text()
    assert "strategy: stable-versioning" in fleet_text
    host_text = (bh_home / "config.yaml").read_text()
    assert "strategy" not in host_text  # never touched the host file


def test_set_scope_fleet_creates_the_hq_store_dir_if_absent(tmp_path, monkeypatch):
    """`--scope fleet` writes the local fleet.yaml file even before `bh hq init` has scaffolded
    the HQ store dir — it must not require the HQ remote to already exist. Never commits/pushes
    (that's `bh hq push`'s job, out of this bead's scope)."""
    home = tmp_path / "bh-home"
    monkeypatch.setenv("BH_HOME", str(home))
    for var in ("BH_CONFIG", "WS_CONFIG", "BH_HQ", "WS_HQ"):
        monkeypatch.delenv(var, raising=False)
    assert not (home / "hq").exists()

    res = config.set_value("delimiter", "|", scope=config.SCOPE_FLEET)
    assert res["ok"] is True
    assert (home / "hq" / "fleet.yaml").read_text().strip() == "delimiter: '|'"
    assert not (home / "hq" / ".git").exists()  # no HQ git operations — just the file


def test_cli_set_scope_fleet_and_host_roundtrip(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)
    runner = CliRunner()

    r = runner.invoke(app, ["config", "set", "otel.protocol", "grpc", "--scope", "host"])
    assert r.exit_code == 0
    assert config.get_value("otel.protocol", scope=config.SCOPE_HOST)["value"] == "grpc"

    r = runner.invoke(app, ["config", "set", "release.strategy", "cadence", "--scope", "fleet"])
    assert r.exit_code == 0
    assert config.get_value("release.strategy", scope=config.SCOPE_FLEET)["value"] == "cadence"


def test_cli_set_scope_host_can_override_worktrees_ephemeral(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    result = CliRunner().invoke(
        app, ["config", "set", "worktrees.ephemeral", "false", "--scope", "host"]
    )

    assert result.exit_code == 0
    assert config.get_value("worktrees.ephemeral", scope=config.SCOPE_HOST)["value"] is False
    assert config.get_value("worktrees.ephemeral")["value"] is False


# ---- unset --scope -------------------------------------------------------------


def test_unset_scope_host_removes_from_host_file_only(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.unset_value("otel.enabled", scope=config.SCOPE_HOST)
    assert res["ok"] is True and res["old"] is True
    assert "enabled" not in (bh_home / "config.yaml").read_text()
    assert config.get_value("work.validate_cmd", scope=config.SCOPE_FLEET)["ok"] is True


def test_unset_scope_fleet_removes_from_fleet_file_only(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.unset_value("delimiter", scope=config.SCOPE_FLEET)
    assert res["ok"] is True and res["old"] == ":"
    assert "delimiter" not in (bh_home / "hq" / "fleet.yaml").read_text()
    assert config.get_value("otel.enabled", scope=config.SCOPE_HOST)["ok"] is True


def test_unset_default_scope_is_still_host(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.unset_value("otel.enabled")
    assert res["ok"] is True
    assert "enabled" not in (bh_home / "config.yaml").read_text()


def test_cli_unset_scope_fleet(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    r = CliRunner().invoke(app, ["config", "unset", "delimiter", "--scope", "fleet"])
    assert r.exit_code == 0
    assert "delimiter" not in (bh_home / "hq" / "fleet.yaml").read_text()


# ---- refusal path: --scope host setting a non-allowlisted fleet key ------------


def test_set_scope_host_refuses_non_allowlisted_fleet_key(bh_home):
    """AC: refused with the SAME message `config.load()`'s loader-side check produces
    (`_reject_fleet_overrides`) — reused verbatim, not a second copy of the wording."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.set_value("work.validate_cmd", "just fast", scope=config.SCOPE_HOST)

    assert res["ok"] is False
    message = res["problems"][0]["message"]
    assert "work.validate_cmd" in message
    assert str(config.config_path()) in message
    assert str(config.fleet_path()) in message
    assert "FLEET_HOST_OVERRIDE_ALLOWLIST" in message

    # nothing was written
    assert "validate_cmd" not in (bh_home / "config.yaml").read_text()


def test_refusal_message_matches_the_loader_exactly(bh_home):
    """Byte-identical to what `config.load()` raises for the same offending key — the AC's
    'same message the loader uses' bar, checked literally."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just fast\n")

    with pytest.raises(config.ConfigError) as exc:
        config.load()
    loader_message = str(exc.value)

    # reset the host file and take the --scope host set path for the identical key/value
    _write_host(bh_home, HOST_YAML)
    res = config.set_value("work.validate_cmd", "just fast", scope=config.SCOPE_HOST)

    assert res["problems"][0]["message"] == loader_message


def test_set_scope_host_allows_allowlisted_fleet_key(bh_home, monkeypatch):
    """The allowlist escape hatch still works through `--scope host`."""
    monkeypatch.setattr(
        config_partition, "FLEET_HOST_OVERRIDE_ALLOWLIST", frozenset({"work.validate_cmd"})
    )
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.set_value("work.validate_cmd", "just fast", scope=config.SCOPE_HOST)

    assert res["ok"] is True
    assert "validate_cmd: just fast" in (bh_home / "config.yaml").read_text()


def test_set_scope_host_does_not_refuse_a_host_partitioned_key(bh_home):
    """The refusal only fires for a FLEET-partitioned key — a HOST key (e.g. `otel.*`) sets
    normally under `--scope host`."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    res = config.set_value("otel.protocol", "http/protobuf", scope=config.SCOPE_HOST)

    assert res["ok"] is True


def test_set_scope_host_does_not_refuse_when_no_fleet_base_exists(bh_home):
    """Mirrors `load()`'s own degrade: no fleet.yaml means nothing to diverge from, so a host
    config predating fleet adoption keeps working exactly as before."""
    _write_host(bh_home, HOST_YAML)  # no fleet.yaml written

    res = config.set_value("work.validate_cmd", "just fast", scope=config.SCOPE_HOST)

    assert res["ok"] is True


def test_cli_set_scope_host_refusal_exits_nonzero(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    r = CliRunner().invoke(
        app, ["config", "set", "work.validate_cmd", "just fast", "--scope", "host"]
    )
    assert r.exit_code == 1
    assert "work.validate_cmd" in r.output


# ---- provenance (`bh config show`) ---------------------------------------------


def test_key_provenance_labels_fleet_only_host_only_and_override(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just fast\n")  # override of a fleet key

    provenance = config.key_provenance()

    assert provenance["delimiter"] == config.PROVENANCE_FLEET  # fleet-only
    assert provenance["providers"] == config.PROVENANCE_FLEET
    assert provenance["work.identity.name"] == config.PROVENANCE_HOST  # host-only
    assert provenance["otel.enabled"] == config.PROVENANCE_HOST
    assert provenance["work.validate_cmd"] == config.PROVENANCE_OVERRIDE  # both sides set it


def test_key_provenance_is_consistent_with_leaf_granularity(bh_home):
    """Walks the SAME leaves `fleet_override_violations` does — no container/namespace rows,
    only real settable leaf keys (`config_partition`'s granularity, not an ad-hoc scheme)."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    provenance = config.key_provenance()

    assert "work" not in provenance  # namespace row, not a leaf
    assert "exclude" not in provenance
    assert "work.validate_cmd" in provenance
    assert "exclude.orgs" in provenance


def test_key_provenance_degrades_when_host_config_is_absent(bh_home):
    _write_fleet(bh_home, FLEET_YAML)

    provenance = config.key_provenance()

    assert provenance["delimiter"] == config.PROVENANCE_FLEET
    assert "otel.enabled" not in provenance


def test_key_provenance_degrades_when_fleet_yaml_is_absent(bh_home):
    _write_host(bh_home, HOST_YAML)

    provenance = config.key_provenance()

    assert provenance["otel.enabled"] == config.PROVENANCE_HOST
    assert "delimiter" not in provenance


def test_cli_config_show_labels_each_key_with_its_origin(bh_home, monkeypatch):
    # allowlist the override so `doctor.show()`'s own `config.load()` call (which enforces the
    # loader-side rejection for a non-allowlisted override) doesn't itself raise here — that
    # rejection path is exercised separately above; this test is about the show/label surface.
    monkeypatch.setattr(
        config_partition, "FLEET_HOST_OVERRIDE_ALLOWLIST", frozenset({"work.validate_cmd"})
    )
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just fast\n")

    r = CliRunner().invoke(app, ["config", "show"])

    assert r.exit_code == 0
    assert "# Provenance" in r.output
    assert "delimiter" in r.output and config.PROVENANCE_FLEET in r.output
    assert "otel.enabled" in r.output and config.PROVENANCE_HOST in r.output
    assert "work.validate_cmd" in r.output and config.PROVENANCE_OVERRIDE in r.output
