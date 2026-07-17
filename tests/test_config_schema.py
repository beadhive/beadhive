"""BeadhiveConfig (config_schema.py) — the pydantic-settings schema layer.

Covers: SCHEMA_VERSION / schema_version defaults, validating the shipped
config.example.yaml, extra="forbid" rejecting unknown top-level + nested keys, BH_ env
overrides (including the deprecated-name-free nested delimiter form), a partial nested
override merging with a section's defaults rather than wiping its sibling fields
(nested_model_default_partial_update), and (bh-5cgm.4) schema introspection: the
`bh config schema` dump (`iter_schema_fields`/`known_keys`) and the did-you-mean helper
(`suggest_key`) built on top of it.

This is a schema/validation-layer test module — it does NOT exercise the ~40 existing
config.py getters or the ruamel round-trip read/write path (see test_config_dotted.py for
did-you-mean wired into `config get`/`set`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from beadhive import config
from beadhive.config_schema import SCHEMA_VERSION, BeadhiveConfig, iter_schema_fields, suggest_key

_EXAMPLE_YAML = Path(config.template("config.example.yaml"))


def _load_example() -> dict:
    y = YAML()
    return dict(y.load(_EXAMPLE_YAML.read_text()))


# ---- schema_version / SCHEMA_VERSION -----------------------------------------


def test_schema_version_constant_is_1():
    assert SCHEMA_VERSION == 1


def test_fresh_model_carries_schema_version_1():
    assert BeadhiveConfig().schema_version == 1


def test_example_yaml_and_config_init_output_carry_schema_version_1():
    """config_init copies config.example.yaml verbatim (src/beadhive/cli.py config_init), so
    stamping the template is sufficient for both the shipped example and a freshly-scaffolded
    config to carry schema_version: 1."""
    data = _load_example()
    assert data["schema_version"] == 1


# ---- validates the shipped config.example.yaml -------------------------------


def test_beadhive_config_validates_shipped_example_with_no_errors():
    data = _load_example()
    cfg = BeadhiveConfig(**data)
    assert cfg.schema_version == 1
    assert cfg.providers == ["github", "gitlab", "gitea"]
    assert cfg.dolt.backend == "docker"
    assert cfg.worktrees.bead_branch == "bead/{kind}/{id}"
    assert cfg.managed_repos == []


# ---- extra="forbid" -----------------------------------------------------------


def test_unknown_top_level_key_rejected():
    with pytest.raises(ValidationError):
        BeadhiveConfig(this_key_does_not_exist=True)


def test_unknown_nested_key_rejected():
    with pytest.raises(ValidationError):
        BeadhiveConfig(dolt={"backend": "docker", "this_key_does_not_exist": True})


def test_known_top_level_and_nested_keys_accepted():
    cfg = BeadhiveConfig(dolt={"backend": "podman"}, work={"max_commits": 5})
    assert cfg.dolt.backend == "podman"
    assert cfg.work.max_commits == 5


# ---- BH_ env overrides (env_prefix + env_nested_delimiter) --------------------


def test_env_override_beats_file_value(monkeypatch):
    monkeypatch.setenv("BH_DOLT__BACKEND", "podman")
    cfg = BeadhiveConfig(**{"dolt": {"backend": "docker"}})
    assert cfg.dolt.backend == "podman"


def test_env_override_top_level_scalar(monkeypatch):
    monkeypatch.setenv("BH_DELIMITER", "/")
    cfg = BeadhiveConfig(**{"delimiter": ":"})
    assert cfg.delimiter == "/"


def test_env_override_deeply_nested_field(monkeypatch):
    monkeypatch.setenv("BH_WORK__DISPATCH__MODE", "collapsed")
    cfg = BeadhiveConfig()
    assert cfg.work.dispatch.mode == "collapsed"


# ---- nested_model_default_partial_update --------------------------------------


def test_partial_nested_env_override_merges_with_sibling_defaults(monkeypatch):
    """Setting only BH_WORK__MAX_COMMITS must not wipe WorkConfig's other fields — they
    keep their defaults rather than the whole `work` section failing/blanking out."""
    monkeypatch.setenv("BH_WORK__MAX_COMMITS", "3")
    cfg = BeadhiveConfig()
    assert cfg.work.max_commits == 3
    assert cfg.work.validate_cmd == "just check"
    assert cfg.work.review_gate == "human"
    assert cfg.work.landing == "local"


def test_partial_nested_kwarg_override_merges_with_sibling_defaults():
    """Same guarantee via direct kwargs (init-source partial update, not just env)."""
    cfg = BeadhiveConfig(work={"review_gate": "gh:pr"})
    assert cfg.work.review_gate == "gh:pr"
    assert cfg.work.validate_cmd == "just check"
    assert cfg.work.max_commits == 10


def test_partial_override_does_not_wipe_dispatch_sub_section():
    """A partial override of one work.* field leaves the nested dispatch sub-model at its
    own defaults, not missing/blank."""
    cfg = BeadhiveConfig(work={"max_commits": 7})
    assert cfg.work.max_commits == 7
    assert cfg.work.dispatch.mode == "fanout"
    assert cfg.work.dispatch.max_depth == 2


# ---- schema introspection (bh-5cgm.4: `bh config schema`) ---------------------


def test_iter_schema_fields_covers_known_keys_with_descriptions():
    """`iter_schema_fields` walks BeadhiveConfig (incl. nested sub-models) rather than a
    hand-maintained list — worktrees.ephemeral / otel.protocol from the acceptance criteria
    show up with a type, default, and non-empty description."""
    by_path = {f.path: f for f in iter_schema_fields()}

    ephemeral = by_path["worktrees.ephemeral"]
    assert ephemeral.type == "bool"
    assert ephemeral.default == "true"
    assert "temp dir" in ephemeral.description

    protocol = by_path["otel.protocol"]
    assert "grpc" in protocol.type and "http/protobuf" in protocol.type
    assert protocol.default == '"grpc"'
    assert "OTLP" in protocol.description


def test_iter_schema_fields_recurses_into_nested_sub_models():
    """A doubly-nested field (otel.genai.model) is reachable — the walk isn't one level deep."""
    by_path = {f.path: f for f in iter_schema_fields()}
    assert "otel.genai.model" in by_path
    assert by_path["otel.genai.model"].type == "str"


def test_iter_schema_fields_does_not_expand_dynamically_keyed_collections():
    """list[Model]/dict[str, Model] fields (e.g. worktrees.init, orgs) describe themselves as
    one collection row — they are not flattened, since their members are user-named, not
    fixed config keys."""
    paths = {f.path for f in iter_schema_fields()}
    assert "worktrees.init" in paths
    assert not any(p.startswith("worktrees.init.") for p in paths)
    assert "orgs" in paths
    assert not any(p.startswith("orgs.") for p in paths)


def test_schema_dump_cli_lists_known_keys_with_descriptions():
    """`bh config schema` (the CLI surface) renders the same rows, human-readable."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    r = CliRunner().invoke(app, ["config", "schema"])
    assert r.exit_code == 0
    assert "worktrees.ephemeral" in r.stdout
    assert "otel.protocol" in r.stdout
    assert "OTLP transport" in r.stdout


def test_schema_dump_cli_json_round_trips():
    import json

    from typer.testing import CliRunner

    from beadhive.cli import app

    r = CliRunner().invoke(app, ["config", "schema", "--json"])
    assert r.exit_code == 0
    rows = json.loads(r.stdout)
    by_path = {row["path"]: row for row in rows}
    assert by_path["otel.protocol"]["type"].count("|") == 1
    assert by_path["worktrees.ephemeral"]["default"] == "true"


# ---- did-you-mean (bh-5cgm.4: suggest_key) -------------------------------------


def test_suggest_key_finds_close_typo():
    assert suggest_key("otel.protcol") == "otel.protocol"
    assert suggest_key("otel.enalbed") == "otel.enabled"


def test_suggest_key_no_match_for_hopelessly_wrong_key():
    """A key with no real resemblance to anything in the schema gets no suggestion —
    never a false positive."""
    assert suggest_key("totally.unrelated.nonsense") is None


def test_suggest_key_no_match_for_a_merely_unset_but_unrelated_key():
    """A key that shares a section prefix with real keys but isn't itself close to any of
    them (e.g. a genuinely-just-unset key) doesn't get a misleading suggestion either."""
    assert suggest_key("otel.nope") is None


def test_suggest_key_returns_none_for_an_exact_match():
    """An exact known key never "suggests itself"."""
    assert suggest_key("otel.protocol") is None
