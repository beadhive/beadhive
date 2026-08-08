"""Out-of-range `Literal[...]` config values (bh-aidze) — `dolt.backend: shared-server`
accepted, persisted, and echoed back by `bh config show` as if in effect, even though it is
NOT a member of `DoltConfig.backend`'s `Literal["colima", "docker", "podman", "none"]`.

Covers both halves named in the bead:
  - WRITE path: `bh config set dolt.backend shared-server` is refused (`config._validate`).
  - LOAD path: a hand-edited config.yaml carrying the same bad value is caught by
    `config.literal_violations` / `config.warn_literal_violations_if_needed` (the write-path
    guard never runs for a value that arrived by hand-editing the file).
  - RENDER path: `bh config show` (doctor.py) marks the value INVALID and shows the effective
    default alongside it, rather than printing it plainly as if it were in effect.
  - SWEEP: other `Literal`-typed fields (not just `dolt.backend`) share the same exposure and
    get the same fix for free (one schema walk, not a `dolt.backend`-only special case).
"""

from __future__ import annotations

from structlog.testing import capture_logs

from beadhive import config, config_schema, doctor, log

# ---- schema introspection (config_schema.literal_choices / field_default) ----


def test_literal_choices_finds_dolt_backend():
    assert config_schema.literal_choices("dolt.backend") == ("colima", "docker", "podman", "none")


def test_field_default_dolt_backend_is_docker():
    assert config_schema.field_default("dolt.backend") == "docker"


def test_literal_choices_none_for_a_non_literal_field():
    assert config_schema.literal_choices("otel.endpoint") is None


def test_literal_choices_none_for_an_unknown_key():
    assert config_schema.literal_choices("dolt.nonsense") is None
    assert config_schema.literal_choices("totally.unrelated.nonsense") is None


def test_literal_choices_does_not_walk_into_collection_members():
    # managed_repos[].kind IS a Literal, but it's dynamically-keyed per list entry — not a
    # fixed dotted key `_field_info` should resolve.
    assert config_schema.literal_choices("managed_repos.kind") is None


# ---- sweep: other Literal fields share the exposure ---------------------------


def test_sweep_multiple_top_level_literal_fields_are_covered():
    """Not just `dolt.backend` — the bead's acceptance bar is a sweep across the schema. A
    handful of representative Literal-typed leaves at different nesting depths."""
    assert config_schema.literal_choices("log.format") == ("auto", "rich", "json")
    assert config_schema.literal_choices("log.level") == (
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    )
    assert config_schema.literal_choices("harness") == ("claude", "opencode")
    assert config_schema.literal_choices("work.dispatch.mode") == ("fanout", "collapsed", "auto")
    assert config_schema.field_default("log.format") == "auto"
    assert config_schema.field_default("work.dispatch.mode") == "fanout"


# ---- config.literal_violations (the load-path detector) -----------------------


def test_literal_violations_catches_hand_written_shared_server():
    """The bead's exact regression case: a hand-written config carrying
    `dolt.backend: shared-server` — never went through `set_value` at all — is caught on load,
    and the effective value it reports is the schema default `docker`."""
    cfg = {"dolt": {"backend": "shared-server"}}
    violations = config.literal_violations(cfg)
    assert len(violations) == 1
    v = violations[0]
    assert v["key"] == "dolt.backend"
    assert v["value"] == "shared-server"
    assert v["choices"] == ("colima", "docker", "podman", "none")
    assert v["default"] == "docker"


def test_literal_violations_clean_config_is_empty():
    cfg = {"dolt": {"backend": "docker"}, "log": {"format": "json"}}
    assert config.literal_violations(cfg) == []


def test_literal_violations_reports_every_bad_literal_leaf_not_just_dolt():
    cfg = {"dolt": {"backend": "shared-server"}, "log": {"format": "yaml"}}
    keys = {v["key"] for v in config.literal_violations(cfg)}
    assert keys == {"dolt.backend", "log.format"}


# ---- config.warn_literal_violations_if_needed (the CLI-seam nudge) ------------


def _captured_warnings():
    """structlog's own capture, not `caplog`: `log.configure()` clears the root handlers on
    first use (which would drop pytest's capture handler mid-test), so a stdlib-level assertion
    would only pass depending on whether some earlier test already configured logging (flaky
    under `pytest -n auto`, where each xdist worker is its own process). The warm-up call forces
    that one-time configure BEFORE the capture context opens — same pattern as
    test_config_fleet_merge.py's `_captured_warnings`."""
    log.get_logger("warmup")
    return capture_logs()


def test_warn_literal_violations_names_key_value_and_allowed_set():
    config.config_path().write_text(
        f"schema_version: {config_schema.SCHEMA_VERSION}\n"
        "providers: [github]\n"
        "managed_repos: []\n"
        "dolt:\n"
        "  backend: shared-server\n"
    )

    with _captured_warnings() as captured:
        config.warn_literal_violations_if_needed()

    warnings = [e for e in captured if e["event"] == "config_literal_value_invalid"]
    assert len(warnings) == 1
    w = warnings[0]
    assert w["key"] == "dolt.backend"
    assert w["value"] == "shared-server"
    assert "docker" in w["allowed"] and "colima" in w["allowed"]
    assert w["effective"] == "docker"
    assert "dolt.backend" in w["hint"] and "shared-server" in w["hint"] and "docker" in w["hint"]


def test_warn_literal_violations_clean_config_is_silent():
    config.config_path().write_text(
        f"schema_version: {config_schema.SCHEMA_VERSION}\n"
        "providers: [github]\n"
        "managed_repos: []\n"
        "dolt:\n"
        "  backend: docker\n"
    )

    with _captured_warnings() as captured:
        config.warn_literal_violations_if_needed()

    assert not [e for e in captured if e["event"] == "config_literal_value_invalid"]


def test_warn_literal_violations_skips_when_config_absent():
    config.config_path().unlink(missing_ok=True)
    config.warn_literal_violations_if_needed()  # must not raise


# ---- write path: `bh config set` refuses the same bad value -------------------


def test_set_value_refuses_out_of_range_dolt_backend(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("dolt:\n  backend: docker\n")
    monkeypatch.setenv("BH_CONFIG", str(p))

    res = config.set_value("dolt.backend", "shared-server")
    assert res["ok"] is False
    assert any(pr["level"] == "error" for pr in res["problems"])
    assert any(
        "dolt.backend" in pr["message"] and "colima" in pr["message"] for pr in res["problems"]
    )

    # nothing written: the bad value never lands
    assert config.get_value("dolt.backend", scope=config.SCOPE_HOST)["value"] == "docker"


def test_set_value_accepts_in_range_dolt_backend(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("dolt:\n  backend: docker\n")
    monkeypatch.setenv("BH_CONFIG", str(p))

    res = config.set_value("dolt.backend", "podman")
    assert res["ok"] is True
    assert config.get_value("dolt.backend", scope=config.SCOPE_HOST)["value"] == "podman"


def test_cli_config_set_bad_dolt_backend_exits_nonzero(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from beadhive.cli import app

    p = tmp_path / "config.yaml"
    p.write_text("dolt:\n  backend: docker\n")
    monkeypatch.setenv("BH_CONFIG", str(p))

    r = CliRunner().invoke(app, ["config", "set", "dolt.backend", "shared-server"])
    assert r.exit_code == 1
    assert "dolt.backend" in r.output


# ---- render path: `bh config show` / `bh doctor` mark it INVALID --------------


def test_render_literal_value_marks_invalid_with_effective_default(capsys):
    text = doctor._render_literal_value("dolt.backend", {"dolt": {"backend": "shared-server"}})
    assert "shared-server" in text
    assert "INVALID" in text
    assert "docker" in text


def test_render_literal_value_plain_when_valid():
    text = doctor._render_literal_value("dolt.backend", {"dolt": {"backend": "podman"}})
    assert text == "podman"
    assert "INVALID" not in text


def test_section_dolt_does_not_present_invalid_value_as_in_effect(capsys):
    doctor._section_dolt({"dolt": {"backend": "shared-server"}})
    out = capsys.readouterr().out
    assert "shared-server" in out
    assert "INVALID" in out
    assert "docker" in out  # the effective value is visible alongside the declared one


def test_section_config_problems_lists_every_violation(capsys):
    doctor._section_config_problems(
        {"dolt": {"backend": "shared-server"}, "log": {"format": "yaml"}}
    )
    out = capsys.readouterr().out
    assert "# Config problems (2)" in out
    assert "dolt.backend" in out and "shared-server" in out
    assert "log.format" in out and "yaml" in out


def test_section_config_problems_silent_when_clean(capsys):
    doctor._section_config_problems({"dolt": {"backend": "docker"}})
    assert capsys.readouterr().out == ""


def test_doctor_warnings_include_literal_violation(tmp_path):
    cfg = {"dolt": {"backend": "shared-server"}, "managed_repos": []}
    warns = doctor._data_warnings(cfg, tmp_path, [], set(), set(), set(), set())
    assert any("dolt.backend" in w and "shared-server" in w and "docker" in w for w in warns)
