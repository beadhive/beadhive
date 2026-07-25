"""config.load() — the fleet base + host override deep-merge (bh-e0y8.5).

Every `bh` invocation reads through `config.load()`, so the bar here is conservative and
loudly-wrong-over-quietly-wrong. Covers the AC's five cases:

- clean merge (fleet base + host layer, nested sections merged, result still schema-valid)
- an ALLOWLISTED fleet key a host may override (the mechanism, injected — the real
  `FLEET_HOST_OVERRIDE_ALLOWLIST` is deliberately empty today, so relying on a live entry
  would pin nothing; the same approach `tests/test_config_partition.py` already takes)
- a NON-allowlisted fleet key the host tries to override → rejected, naming the key
- absent fleet.yaml → host-only, plus the operator-facing warning
- absent host config.yaml → fleet-only (and both absent → the historical FileNotFoundError)

...plus the write-path guarantee that makes the merge safe: `load_host()` never merges, so a
read-modify-write can't bake fleet truth into a host's own file.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from beadhive import config, config_partition, log
from beadhive.config_validate import validate_config

# A fleet base carrying only FLEET-partition keys, and a host layer carrying only HOST ones —
# the split `config_partition` defines. Together they make one schema-valid config.
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
  max_commits: 10
  dispatch:
    mode: fanout
"""

# `work:` is deliberately LAST (and `work.dispatch` last within it) so a test can append an
# extra key at the right indent to build a host config that overreaches — appending a second
# `work:` block instead would be a duplicate YAML key, which ruamel rejects outright.
HOST_YAML = """\
otel:
  enabled: true
  endpoint: http://localhost:4317
worktrees:
  path: ~/.beadhive/worktrees
hq:
  remote: acme/beadhive-hq
work:
  identity:
    name: dev/ada
  dispatch:
    max_beads_per_session: 3
"""


@pytest.fixture
def bh_home(tmp_path, monkeypatch):
    """An isolated BH_HOME with an HQ store dir, overriding conftest's sandbox so this test
    owns BOTH files the merge reads. Write into it with `_write_fleet` / `_write_host`."""
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


def _captured_warnings():
    """structlog's own capture, not `caplog`: `log.configure()` clears the root handlers on
    first use (which would drop pytest's capture handler mid-test), so a stdlib-level assertion
    would only pass depending on whether some earlier test already configured logging. The
    warm-up call forces that one-time configure BEFORE the capture context opens."""
    log.get_logger("warmup")
    return capture_logs()


# ---- clean merge --------------------------------------------------------------


def test_clean_merge_carries_both_sides(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    cfg = config.load()

    assert cfg["delimiter"] == ":"  # fleet-only key survives
    assert cfg["providers"] == ["github"]
    assert cfg["otel"]["endpoint"] == "http://localhost:4317"  # host-only key survives
    assert cfg["hq"]["remote"] == "acme/beadhive-hq"


def test_clean_merge_is_deep_not_section_replacing(bh_home):
    """The whole point of a DEEP merge: a host setting one field of a section must not blow
    away the fleet's sibling fields in that same section (or sub-section)."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    cfg = config.load()

    assert cfg["work"]["validate_cmd"] == "just check"  # fleet
    assert cfg["work"]["identity"]["name"] == "dev/ada"  # host
    assert cfg["work"]["dispatch"]["mode"] == "fanout"  # fleet, one level deeper
    assert cfg["work"]["dispatch"]["max_beads_per_session"] == 3  # host, same sub-section
    assert cfg["worktrees"]["ephemeral"] is True  # fleet
    assert cfg["worktrees"]["path"] == "~/.beadhive/worktrees"  # host


def test_merged_result_validates_against_the_existing_schema(bh_home):
    """AC: the merged result validates against `config_schema.py` — with no schema change."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    problems = validate_config(config.load())

    assert [p for p in problems if p["level"] == "error"] == []


def test_merge_never_mutates_either_source():
    """A merged view that wrote back through its inputs would corrupt whichever file a later
    `save()` targets — the merged map has to be a fresh object graph."""
    fleet = {"work": {"validate_cmd": "just check", "dispatch": {"mode": "fanout"}}}
    host = {"work": {"dispatch": {"max_beads_per_session": 3}}}

    merged = config._deep_merge(fleet, host)
    merged["work"]["validate_cmd"] = "mutated"
    merged["work"]["dispatch"]["mode"] = "collapsed"

    assert fleet == {"work": {"validate_cmd": "just check", "dispatch": {"mode": "fanout"}}}
    assert host == {"work": {"dispatch": {"max_beads_per_session": 3}}}


def test_merge_replaces_lists_wholesale_rather_than_appending():
    merged = config._deep_merge({"providers": ["github", "gitlab"]}, {"providers": ["gitea"]})

    assert merged["providers"] == ["gitea"]


def test_getters_read_through_the_merged_view(bh_home):
    """The existing getters (incl. bh-e0y8.1's `hq_remote`) keep working over the merged map."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    cfg = config.load()

    assert config.validate_cmd(cfg, None) == "just check"  # fleet-side default
    assert config.hq_remote(cfg) == "acme/beadhive-hq"  # host-side value
    assert config.otel_enabled(cfg) is True
    assert config.dispatch_max_beads_per_session(cfg, None) == 3


# ---- allowlisted override ------------------------------------------------------


def test_allowlisted_fleet_key_may_be_overridden_by_the_host(bh_home, monkeypatch):
    """The allowlist mechanism, exercised with an injected entry: `FLEET_HOST_OVERRIDE_ALLOWLIST`
    is empty today (no fleet key needs a per-host escape hatch yet), so a test relying on a real
    entry would pin nothing. An allowlisted key: host wins, no rejection."""
    monkeypatch.setattr(
        config_partition, "FLEET_HOST_OVERRIDE_ALLOWLIST", frozenset({"work.validate_cmd"})
    )
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just fast\n")

    cfg = config.load()

    assert cfg["work"]["validate_cmd"] == "just fast"
    assert cfg["work"]["review_gate"] == "human"  # untouched fleet sibling


def test_allowlist_covers_keys_nested_under_an_allowlisted_prefix(bh_home, monkeypatch):
    monkeypatch.setattr(
        config_partition, "FLEET_HOST_OVERRIDE_ALLOWLIST", frozenset({"work.dispatch"})
    )
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "    mode: collapsed\n")

    assert config.load()["work"]["dispatch"]["mode"] == "collapsed"


def test_allowlisting_one_key_does_not_allowlist_its_siblings(bh_home, monkeypatch):
    monkeypatch.setattr(
        config_partition, "FLEET_HOST_OVERRIDE_ALLOWLIST", frozenset({"work.validate_cmd"})
    )
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  review_gate: timer\n")

    with pytest.raises(config.ConfigError) as exc:
        config.load()

    assert "work.review_gate" in str(exc.value)


# ---- rejected override ----------------------------------------------------------


def test_non_allowlisted_fleet_key_is_rejected_naming_the_key(bh_home):
    """AC: rejected with a clear message NAMING the key — not silently ignored, not silently
    applied. `work.validate_cmd` is fleet truth and the real allowlist is empty."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just fast\n")

    with pytest.raises(config.ConfigError) as exc:
        config.load()

    assert "work.validate_cmd" in str(exc.value)


def test_rejection_names_every_offending_key(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  max_commits: 99\ndelimiter: '|'\n")

    with pytest.raises(config.ConfigError) as exc:
        config.load()

    message = str(exc.value)
    assert "work.max_commits" in message
    assert "delimiter" in message


def test_rejection_points_at_both_files(bh_home):
    """The message has to be actionable: which file holds the offending key, and where the key
    belongs instead."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just fast\n")

    with pytest.raises(config.ConfigError) as exc:
        config.load()

    assert str(config.config_path()) in str(exc.value)
    assert str(config.fleet_path()) in str(exc.value)


def test_a_fleet_key_the_host_merely_repeats_is_still_an_override(bh_home):
    """Same VALUE, still rejected: a stale host copy of fleet truth is exactly the silent
    divergence the split exists to prevent — it stops tracking the fleet the moment fleet.yaml
    changes."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "  validate_cmd: just check\n")

    with pytest.raises(config.ConfigError):
        config.load()


def test_host_only_and_unclassified_keys_are_never_rejected(bh_home):
    """`partition_of` → None (a key neither side claims, e.g. the un-schema'd `beads` section)
    is not a licence to reject — only a known FLEET key is."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML + "beads:\n  engine: bd\n")

    assert config.load()["beads"]["engine"] == "bd"


def test_empty_host_section_is_not_an_override(bh_home):
    """`work: {}` sets no value at all — a namespace row is not a leaf, so it must not trip the
    fleet-key check."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, "work: {}\n")

    assert config.load()["work"]["validate_cmd"] == "just check"


def test_fleet_override_violations_lists_keys_without_raising(bh_home):
    """The check is inspectable data, not only an exception."""
    violations = config.fleet_override_violations(
        {"delimiter": "|", "otel": {"enabled": True}, "work": {"identity": {"name": "dev/ada"}}}
    )

    assert violations == ["delimiter"]


# ---- absent fleet.yaml (a host that has not cloned HQ) --------------------------


def test_absent_fleet_yaml_degrades_to_host_only(bh_home):
    _write_host(bh_home, HOST_YAML)

    cfg = config.load()

    assert cfg["otel"]["endpoint"] == "http://localhost:4317"
    assert "delimiter" not in cfg  # nothing invented from a fleet base that isn't there


def test_absent_fleet_yaml_does_not_reject_fleet_keys_in_the_host_config(bh_home):
    """Today's config.yaml legitimately holds fleet keys — with no fleet base to diverge FROM
    there is no override to reject, so an un-migrated host keeps working exactly as before."""
    _write_host(bh_home, FLEET_YAML)

    cfg = config.load()

    assert cfg["work"]["validate_cmd"] == "just check"
    assert cfg["delimiter"] == ":"


def test_blank_fleet_yaml_reads_as_absent(bh_home):
    _write_fleet(bh_home, "")
    _write_host(bh_home, FLEET_YAML)

    assert config.load()["work"]["validate_cmd"] == "just check"


def test_missing_fleet_warns_when_the_host_has_an_hq_store(bh_home):
    _write_host(bh_home, HOST_YAML)

    with _captured_warnings() as captured:
        config.warn_missing_fleet_config_if_needed()

    warnings = [e for e in captured if e["event"] == "fleet_config_missing"]
    assert len(warnings) == 1
    assert warnings[0]["expected"] == str(config.fleet_path())


def test_missing_fleet_is_silent_without_an_hq_store(bh_home):
    """A host that has never run `bh hq init` is not fleet-managed — host-only is its normal
    state, and warning on it would fire on every single `bh` invocation."""
    (bh_home / "hq").rmdir()
    _write_host(bh_home, HOST_YAML)

    with _captured_warnings() as captured:
        config.warn_missing_fleet_config_if_needed()

    assert not [e for e in captured if e["event"] == "fleet_config_missing"]


def test_no_warning_once_fleet_yaml_is_present(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    with _captured_warnings() as captured:
        config.warn_missing_fleet_config_if_needed()

    assert not [e for e in captured if e["event"] == "fleet_config_missing"]


def test_cli_invocation_surfaces_the_missing_fleet_warning(bh_home):
    """The one real call site: `cli._root` runs on every actual `bh <command>` invocation, so
    the nudge reaches the operator exactly once per command."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    _write_host(bh_home, HOST_YAML)

    with _captured_warnings() as captured:
        result = CliRunner().invoke(app, ["config", "path"])

    assert result.exit_code == 0
    assert [e for e in captured if e["event"] == "fleet_config_missing"]


def test_bare_load_never_warns(bh_home):
    """`load()` stays side-effect-free: `log.configure()` itself reads config, so warning from
    inside the load path would recurse. The nudge lives at the CLI seam instead."""
    _write_host(bh_home, HOST_YAML)

    with _captured_warnings() as captured:
        config.load()

    assert not [e for e in captured if e["event"] == "fleet_config_missing"]


# ---- absent host config ----------------------------------------------------------


def test_absent_host_config_degrades_to_fleet_only(bh_home):
    _write_fleet(bh_home, FLEET_YAML)

    cfg = config.load()

    assert cfg["work"]["validate_cmd"] == "just check"
    assert cfg["delimiter"] == ":"


def test_both_absent_still_raises_the_scaffold_hint(bh_home):
    with pytest.raises(FileNotFoundError) as exc:
        config.load()

    assert "config init" in str(exc.value)


def test_load_host_alone_still_raises_when_the_host_config_is_absent(bh_home):
    _write_fleet(bh_home, FLEET_YAML)

    with pytest.raises(FileNotFoundError):
        config.load_host()


# ---- write path: load_host() never merges ----------------------------------------


def test_load_host_returns_the_host_file_verbatim(bh_home):
    """The guarantee that makes the merge safe: every read-modify-write path (`set_value`, the
    registry, `bh hive enable`) loads through `load_host()`, so `save()` can never write the
    fleet's keys into the host's own file."""
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    host = config.load_host()

    assert "delimiter" not in host
    assert "validate_cmd" not in host["work"]
    assert host["otel"]["endpoint"] == "http://localhost:4317"


def test_set_value_writes_only_host_content_back(bh_home):
    _write_fleet(bh_home, FLEET_YAML)
    _write_host(bh_home, HOST_YAML)

    config.set_value("otel.enabled", "false")

    written = (bh_home / "config.yaml").read_text()
    assert "delimiter" not in written  # the fleet base did not leak into the host file
    assert "validate_cmd" not in written
    assert "enabled: false" in written
