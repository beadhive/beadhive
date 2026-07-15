"""ws config get/set/unset — dotted-path mutation over the round-trip CommentedMap.

Covers coercion (bool/int/str + --json), validation (otel.protocol enum, *.enabled bool,
unknown-section warn), auto-vivification, and — the load-bearing acceptance — round-trip
preservation of comments + flow-style managed_repos when set/unset rewrite config.yaml.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config
from beadhive.cli import app

FIXTURE = Path(__file__).parent / "fixture_config.yaml"


@pytest.fixture
def cfg_path(tmp_path, monkeypatch) -> Path:
    """A temp copy of the fixture config wired in via $WS_CONFIG so set/unset hit a real file."""
    p = tmp_path / "config.yaml"
    shutil.copy(FIXTURE, p)
    monkeypatch.setenv("WS_CONFIG", str(p))
    return p


# ---- coercion ---------------------------------------------------------------


def test_coerce_bool_int_str_default():
    assert config.coerce_value("true") is True
    assert config.coerce_value("False") is False  # case-insensitive
    assert config.coerce_value("42") == 42
    assert config.coerce_value("-7") == -7
    assert config.coerce_value("http/protobuf") == "http/protobuf"  # string default
    assert config.coerce_value("1.5") == "1.5"  # float is not int → stays string


def test_coerce_json_escape_hatch():
    assert config.coerce_value('["a", "b"]', as_json=True) == ["a", "b"]
    assert config.coerce_value('{"k": 1}', as_json=True) == {"k": 1}
    assert config.coerce_value("1.5", as_json=True) == 1.5


# ---- validation -------------------------------------------------------------


def test_otel_protocol_enum_rejected(cfg_path):
    res = config.set_value("otel.protocol", "carrier-pigeon")
    assert res["ok"] is False
    assert any(p["level"] == "error" for p in res["problems"])
    # nothing written: the bad value never lands
    assert config.get_value("otel.protocol")["ok"] is False


def test_otel_protocol_enum_accepted(cfg_path):
    res = config.set_value("otel.protocol", "http/protobuf")
    assert res["ok"] is True
    assert config.get_value("otel.protocol")["value"] == "http/protobuf"


def test_enabled_must_be_bool(cfg_path):
    res = config.set_value("otel.enabled", "yes")  # "yes" coerces to str, not bool
    assert res["ok"] is False
    assert any("boolean" in p["message"] for p in res["problems"])

    ok = config.set_value("otel.enabled", "true")
    assert ok["ok"] is True
    assert config.get_value("otel.enabled")["value"] is True


def test_unknown_top_level_section_warns_not_rejects(cfg_path):
    res = config.set_value("mycustom.flag", "1")
    assert res["ok"] is True  # warn, not reject
    assert any(p["level"] == "warning" for p in res["problems"])
    assert config.get_value("mycustom.flag")["value"] == 1


def test_passthrough_section_is_known_section_no_warning(cfg_path):
    res = config.set_value("passthrough.bd_enabled", "true")
    assert res["ok"] is True
    # Verify no unknown section warning is emitted
    unknown_warnings = [
        p for p in res["problems"]
        if p["level"] == "warning" and "unknown config section" in p["message"]
    ]
    assert len(unknown_warnings) == 0
    assert config.get_value("passthrough.bd_enabled")["value"] is True


def test_passthrough_git_enabled_known_section_no_warning(cfg_path):
    res = config.set_value("passthrough.git_enabled", "false")
    assert res["ok"] is True
    # Verify no unknown section warning is emitted
    unknown_warnings = [
        p for p in res["problems"]
        if p["level"] == "warning" and "unknown config section" in p["message"]
    ]
    assert len(unknown_warnings) == 0
    assert config.get_value("passthrough.git_enabled")["value"] is False


# ---- auto-vivification + descend conflicts ----------------------------------


def test_set_auto_vivifies_intermediate_maps(cfg_path):
    res = config.set_value("otel.genai.model", "claude-opus")
    assert res["ok"] is True
    assert config.get_value("otel.genai.model")["value"] == "claude-opus"


def test_set_into_scalar_is_an_error(cfg_path):
    res = config.set_value("delimiter.nested", "x")  # delimiter is a scalar ":"
    assert res["ok"] is False
    assert any("scalar" in p["message"] for p in res["problems"])


def test_empty_key_raises():
    with pytest.raises(ValueError):
        config.set_value("", "x", cfg={})


# ---- old/new + unset semantics ----------------------------------------------


def test_set_returns_old_and_new(cfg_path):
    first = config.set_value("otel.endpoint", "http://a:4317")
    assert first["old"] is None and first["new"] == "http://a:4317"
    second = config.set_value("otel.endpoint", "http://b:4317")
    assert second["old"] == "http://a:4317" and second["new"] == "http://b:4317"


def test_unset_removes_key_and_reports_old(cfg_path):
    config.set_value("otel.enabled", "true")
    res = config.unset_value("otel.enabled")
    assert res["ok"] is True and res["old"] is True and res["new"] is None
    assert config.get_value("otel.enabled")["ok"] is False


def test_unset_missing_key_is_not_ok(cfg_path):
    res = config.unset_value("otel.nope")
    assert res["ok"] is False


# ---- round-trip preservation (the acceptance criterion) ---------------------


def test_set_preserves_comments_and_flow_style(cfg_path):
    config.set_value("otel.enabled", "true")
    text = cfg_path.read_text()
    # leading comment survives
    assert "round-trip must preserve this comment" in text
    # flow-style managed_repos entries stay on one line each (inline {} maps)
    assert '{"provider": "github", "org": "agentguides"' in text
    assert "policy = required" in text or "policy: required = org-native" in text
    # the new value landed
    assert "enabled: true" in text


def test_unset_preserves_comments_and_flow_style(cfg_path):
    config.unset_value("dolt")
    text = cfg_path.read_text()
    assert "round-trip must preserve this comment" in text
    assert '{"provider": "github", "org": "agentguides"' in text
    assert "dolt:" not in text


# ---- CLI surface ------------------------------------------------------------


def test_cli_set_get_unset_roundtrip(cfg_path):
    runner = CliRunner()

    r = runner.invoke(app, ["config", "set", "otel.enabled", "true"])
    assert r.exit_code == 0

    r = runner.invoke(app, ["config", "get", "otel.enabled"])
    assert r.exit_code == 0 and r.stdout.strip() == "true"

    r = runner.invoke(app, ["config", "unset", "otel.enabled"])
    assert r.exit_code == 0

    r = runner.invoke(app, ["config", "get", "otel.enabled"])
    assert r.exit_code == 1  # gone


def test_cli_get_missing_exits_nonzero(cfg_path):
    r = CliRunner().invoke(app, ["config", "get", "otel.missing"])
    assert r.exit_code == 1


def test_cli_set_json_list(cfg_path):
    runner = CliRunner()
    r = runner.invoke(app, ["config", "set", "exclude.repos", '["a/b", "c/d"]', "--json"])
    assert r.exit_code == 0
    assert config.get_value("exclude.repos")["value"] == ["a/b", "c/d"]


def test_cli_set_bad_enum_exits_nonzero(cfg_path):
    r = CliRunner().invoke(app, ["config", "set", "otel.protocol", "bogus"])
    assert r.exit_code == 1


# ---- §2.1 control-plane backstop: controller is read-only over the HQ registry (bead .36) ----


def test_save_denies_controller_hq_write(cfg_path, monkeypatch):
    """The persistence choke point blocks a controller session (WS_DEV=ctrl/…) from mutating the
    Head Office registry; a non-controller control seat persists fine."""
    import typer

    # $BH_DEV outranks $WS_DEV in identity resolution, so an operator-exported BH_DEV
    # (e.g. during `bh work submit`'s clean-checkout validation) would otherwise shadow
    # the WS_DEV seat this test sets. Clear the identity env so the seat is deterministic.
    monkeypatch.delenv("BH_DEV", raising=False)
    monkeypatch.delenv("WS_CREW", raising=False)

    cfg = config.load()
    monkeypatch.setenv("WS_DEV", "ctrl/gauge")
    with pytest.raises(typer.Exit):
        config.save(cfg)
    monkeypatch.setenv("WS_DEV", "cust/care")  # custodian writes rig config
    config.save(cfg)  # no raise
