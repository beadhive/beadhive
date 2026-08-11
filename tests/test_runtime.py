"""The Runtime seam (bh-c6dk.1) — `work.runtime` config + the `Runtime` protocol.

Covers the seam only: this bead does not implement the `local` or `temporal` tiers
(bh-c6dk.5 / bh-c6dk.4), so those two assert a loud `NotImplementedError` naming the bead that
will land them, not working schedulers. `claude` is documented, not developed (ADR Decision 2)
— its `ClaudeRuntime` anchor raises too, on purpose (see runtime.py's module docstring).
"""

from __future__ import annotations

import pytest

from beadhive import config, config_schema, config_validate, runtime

# ---- config: work.runtime section --------------------------------------------


def test_work_runtime_defaults_to_local():
    assert config.work_runtime({}, None) == "local"
    assert config.work_runtime({"work": {"runtime": "local"}}, None) == "local"


def test_work_runtime_reads_configured_value():
    assert config.work_runtime({"work": {"runtime": "claude"}}, None) == "claude"
    assert config.work_runtime({"work": {"runtime": "temporal"}}, None) == "temporal"


def test_work_runtime_layers_per_hive_over_global():
    cfg = {"work": {"runtime": "local"}}
    entry = {"work": {"runtime": "temporal"}}
    assert config.work_runtime(cfg, entry) == "temporal"


def test_work_runtime_falls_back_to_local_for_unknown_value():
    # Tolerant-getter shape (matches `work_landing`/`review_gate`): a hand-edited bad value
    # never crashes an unrelated `bh` invocation that only reads config. Real rejection is the
    # schema/write-path checks below (bh-aidze) and `get_runtime`'s own ValueError.
    assert config.work_runtime({"work": {"runtime": "bogus"}}, None) == "local"


# ---- schema: work.runtime is a real, validated Literal field -----------------


def test_literal_choices_finds_work_runtime():
    assert config_schema.literal_choices("work.runtime") == ("claude", "local", "temporal")


def test_field_default_work_runtime_is_local():
    assert config_schema.field_default("work.runtime") == "local"


def test_literal_violations_catches_bad_work_runtime():
    cfg = {"work": {"runtime": "bogus"}}
    violations = config.literal_violations(cfg)
    assert len(violations) == 1
    v = violations[0]
    assert v["key"] == "work.runtime"
    assert v["value"] == "bogus"
    assert v["choices"] == ("claude", "local", "temporal")
    assert v["default"] == "local"


def test_literal_violations_clean_work_runtime_is_empty():
    assert config.literal_violations({"work": {"runtime": "temporal"}}) == []


# ---- write path: `bh config set` refuses an invalid work.runtime -------------


def test_set_value_refuses_out_of_range_work_runtime(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("work:\n  runtime: local\n")
    monkeypatch.setenv("BH_CONFIG", str(p))

    res = config.set_value("work.runtime", "docker")
    assert res["ok"] is False
    assert any(pr["level"] == "error" for pr in res["problems"])
    assert any(
        "work.runtime" in pr["message"] and "claude" in pr["message"] for pr in res["problems"]
    )
    # nothing written: the bad value never lands
    assert config.get_value("work.runtime", scope=config.SCOPE_HOST)["value"] == "local"


def test_set_value_accepts_in_range_work_runtime(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("work:\n  runtime: local\n")
    monkeypatch.setenv("BH_CONFIG", str(p))

    res = config.set_value("work.runtime", "temporal")
    assert res["ok"] is True
    assert config.get_value("work.runtime", scope=config.SCOPE_HOST)["value"] == "temporal"


def test_cli_config_set_bad_work_runtime_exits_nonzero(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from beadhive.cli import app

    p = tmp_path / "config.yaml"
    p.write_text("work:\n  runtime: local\n")
    monkeypatch.setenv("BH_CONFIG", str(p))

    r = CliRunner().invoke(app, ["config", "set", "work.runtime", "docker"])
    assert r.exit_code == 1
    assert "work.runtime" in r.output


# ---- load path: `bh config validate` rejects an invalid work.runtime ---------


def test_validate_config_rejects_bad_work_runtime():
    cfg = {
        "schema_version": config_schema.SCHEMA_VERSION,
        "providers": ["github"],
        "managed_repos": [],
        "work": {"runtime": "docker"},
    }
    problems = config_validate.validate_config(cfg)
    assert any(
        p["level"] == "error" and "work" in p["message"] and "runtime" in p["message"]
        for p in problems
    )


def test_validate_config_accepts_each_valid_work_runtime():
    for value in ("claude", "local", "temporal"):
        cfg = {
            "schema_version": config_schema.SCHEMA_VERSION,
            "providers": ["github"],
            "managed_repos": [],
            "work": {"runtime": value},
        }
        problems = config_validate.validate_config(cfg)
        assert not any("runtime" in p["message"] for p in problems), (value, problems)


# ---- Runtime protocol conformance ---------------------------------------------


class _FakeRuntime:
    """A minimal double implementing every `Runtime` operation — the conformance positive
    case. Return values are throwaway; only the shape (method names + `name`) is asserted."""

    name = "fake"

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        return runtime.RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        return runtime.RoleOutcome(status="running")

    def on_gate_resolved(self, gate_id):
        return None


class _MissingObserve:
    """Structurally short one method — the conformance negative case."""

    name = "incomplete"

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        return runtime.RoleHandle(bead_id=bead_id, session_id=session_id)

    def on_gate_resolved(self, gate_id):
        return None


def test_fake_runtime_conforms_to_the_protocol():
    assert isinstance(_FakeRuntime(), runtime.Runtime)


def test_incomplete_runtime_does_not_conform():
    assert not isinstance(_MissingObserve(), runtime.Runtime)


def test_claude_runtime_conforms_to_the_protocol():
    assert isinstance(runtime.ClaudeRuntime(), runtime.Runtime)


def test_runtime_protocol_names_no_tier_specific_types():
    """Acceptance: no tier-specific types leak into the protocol's signature — nothing from
    baml-harness (SeatRun/RoleOutcome), asyncio, or Temporal appears as an annotation."""
    import inspect

    for meth_name in ("schedule", "observe", "on_gate_resolved"):
        sig = inspect.signature(getattr(runtime.Runtime, meth_name))
        for param in sig.parameters.values():
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                continue
            ann_str = str(ann)
            assert "baml" not in ann_str.lower()
            assert "asyncio" not in ann_str.lower()
            assert "temporal" not in ann_str.lower()


# ---- get_runtime() -------------------------------------------------------------


def test_get_runtime_returns_claude_runtime():
    got = runtime.get_runtime({"work": {"runtime": "claude"}})
    assert isinstance(got, runtime.ClaudeRuntime)
    assert got.name == "claude"


def test_get_runtime_defaults_to_local_and_returns_the_local_tier():
    """`local` is the default and, since bh-c6dk.5, a REAL running tier rather than a loud gap.
    Asserted through the protocol (`isinstance(..., Runtime)`) rather than the concrete class,
    because the seam's promise is the protocol — that is what a caller may rely on."""
    got = runtime.get_runtime({})
    assert got.name == "local"
    assert isinstance(got, runtime.Runtime)


def test_get_runtime_temporal_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="bh-c6dk.4"):
        runtime.get_runtime({"work": {"runtime": "temporal"}})


def test_get_runtime_falls_back_to_local_when_config_missing(monkeypatch):
    def raise_not_found():
        raise FileNotFoundError("no config yet")

    monkeypatch.setattr(config, "load", raise_not_found)
    # No config yet (pre-`bh config init`) still yields the default tier — the fallback must not
    # re-enter the loader from inside the `work.dispatch.*` accessors and re-raise.
    assert runtime.get_runtime().name == "local"


# ---- ClaudeRuntime: documented, not developed — every method refuses to run --


def test_claude_runtime_schedule_raises_with_doc_pointer():
    with pytest.raises(NotImplementedError, match="docs/WORK.md"):
        runtime.ClaudeRuntime().schedule(
            "bh-1", "developer", workspace="/tmp", instructions="/tmp/i", session_id="s"
        )


def test_claude_runtime_observe_raises():
    with pytest.raises(NotImplementedError):
        runtime.ClaudeRuntime().observe(runtime.RoleHandle(bead_id="bh-1", session_id="s"))


def test_claude_runtime_on_gate_resolved_raises():
    with pytest.raises(NotImplementedError):
        runtime.ClaudeRuntime().on_gate_resolved("gate-1")
