"""The Runtime seam (bh-c6dk.1) — `work.runtime` config + the `Runtime` protocol.

Covers the seam only: this bead does not implement the `local` or `temporal` tiers
(bh-c6dk.5 / bh-c6dk.4), so those two assert a loud `NotImplementedError` naming the bead that
will land them, not working schedulers. `claude` is documented, not developed (ADR Decision 2)
— its `ClaudeRuntime` anchor raises too, on purpose (see runtime.py's module docstring).
"""

from __future__ import annotations

import pytest

from beadhive import config, config_schema, config_validate, runtime
from beadhive.modules.config.domain import runtime_names

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


def test_work_runtime_getter_returns_the_name_unvalidated():
    # The getter stays registry-free; rejection is the schema's job at load and `get_runtime`'s
    # at resolution (below).
    assert config.work_runtime({"work": {"runtime": "bogus"}}, None) == "bogus"
    assert config.work_runtime({"work": {"runtime": " "}}, None) == "local"


# ---- schema: work.runtime is a real, validated Literal field -----------------


def test_work_runtime_is_a_registry_validated_string_not_a_closed_literal():
    assert config_schema.literal_choices("work.runtime") is None


def test_field_default_work_runtime_is_local():
    assert config_schema.field_default("work.runtime") == "local"


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

    def on_gate_resolved(self, resolution):
        return None


class _MissingObserve:
    """Structurally short one method — the conformance negative case."""

    name = "incomplete"

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        return runtime.RoleHandle(bead_id=bead_id, session_id=session_id)

    def on_gate_resolved(self, resolution):
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
        runtime.ClaudeRuntime().on_gate_resolved(
            runtime.GateResolution(gate_id="g", bead_id="b", verdict="approved")
        )


# ---- the registry (bh-ubmts) ----------------------------------------------------------------


@pytest.fixture
def clean_registry():
    saved = dict(runtime._RUNTIMES)
    yield
    runtime._RUNTIMES.clear()
    runtime._RUNTIMES.update(saved)


def test_builtins_are_registered():
    assert {"claude", "local", "temporal"} <= set(runtime.available_runtimes())
    assert runtime.available_runtimes() == sorted(runtime.available_runtimes())


def test_registered_runtime_resolves_from_work_runtime(clean_registry):
    marker = _FakeRuntime()
    runtime.register_runtime("gascity", lambda cfg, entry: marker)
    assert runtime.get_runtime({"work": {"runtime": "gascity"}}) is marker


def test_factory_receives_cfg_and_entry(clean_registry):
    got = []
    runtime.register_runtime("spy", lambda cfg, entry: got.append((cfg, entry)) or _FakeRuntime())
    cfg = {"work": {"runtime": "spy"}}
    runtime.get_runtime(cfg)
    assert got == [(cfg, None)]


def test_duplicate_name_is_refused_unless_replace(clean_registry):
    runtime.register_runtime("dup", lambda cfg, entry: _FakeRuntime())
    with pytest.raises(ValueError, match="already registered"):
        runtime.register_runtime("dup", lambda cfg, entry: _FakeRuntime())
    with pytest.raises(ValueError, match="already registered"):
        runtime.register_runtime("local", lambda cfg, entry: _FakeRuntime())
    other = _FakeRuntime()
    runtime.register_runtime("dup", lambda cfg, entry: other, replace=True)
    assert runtime.get_runtime({"work": {"runtime": "dup"}}) is other


def test_empty_name_is_refused(clean_registry):
    with pytest.raises(ValueError):
        runtime.register_runtime("", lambda cfg, entry: _FakeRuntime())


def test_unknown_name_fails_closed_listing_available_names():
    with pytest.raises(
        ValueError, match=r"unknown runtime 'nope'; registered: claude, local, temporal"
    ):
        runtime.get_runtime({"work": {"runtime": "nope"}}, entry_points=lambda **kw: [])


def test_config_validate_checks_the_registry_at_use(clean_registry):
    def problems(name):
        return config_validate.validate_config(
            {
                "schema_version": config_schema.SCHEMA_VERSION,
                "providers": ["github"],
                "managed_repos": [],
                "work": {"runtime": name},
            }
        )

    assert any("unknown runtime 'gascity'" in p["message"] for p in problems("gascity"))
    runtime.register_runtime("gascity", lambda cfg, entry: _FakeRuntime())
    assert not any("runtime" in p["message"] for p in problems("gascity"))


# ---- syntax at load, strict at use (bh-igvro) -------------------------------------------------

_GASCITY_ENTRY = (
    "  - provider: github\n    org: o\n    repo: r\n    prefix: r\n    kind: external\n"
    "    work:\n      runtime: gascity\n"
)


def _write_cfg(tmp_path, monkeypatch, body):
    p = tmp_path / "config.yaml"
    p.write_text(f"schema_version: {config_schema.SCHEMA_VERSION}\nproviders: [github]\n{body}")
    monkeypatch.setenv("BH_CONFIG", str(p))
    return p


def test_file_backed_load_accepts_unregistered_well_formed_host_runtime(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, "managed_repos: []\nwork:\n  runtime: gascity\n")
    assert config.work_runtime(config.load(), None) == "gascity"


def test_file_backed_load_accepts_unregistered_well_formed_per_hive_runtime(tmp_path, monkeypatch):
    _write_cfg(tmp_path, monkeypatch, f"managed_repos:\n{_GASCITY_ENTRY}")
    cfg = config.load()
    assert config.work_runtime(cfg, cfg["managed_repos"][0]) == "gascity"


@pytest.mark.parametrize("scope", ["host", "fleet"])
def test_raw_document_gate_accepts_unregistered_runtime_at_both_scopes(scope):
    from beadhive import hq_document_validation as hdv

    doc = {
        "schema_version": config_schema.SCHEMA_VERSION,
        "work": {"runtime": "gascity"},
        "managed_repos": [
            {
                "provider": "github",
                "org": "o",
                "repo": "r",
                "prefix": "r",
                "kind": "external",
                "work": {"runtime": "gascity"},
            }
        ],
    }
    hdv.validate_settings_mapping(doc, scope=scope)
    for bad in ("", "has space", "-lead"):
        doc["work"]["runtime"] = bad
        with pytest.raises(hdv.DocumentValidationError):
            hdv.validate_settings_mapping(doc, scope=scope)


@pytest.mark.parametrize("bad", ["", " ", "has space", "-lead", "a/b"])
def test_empty_or_malformed_runtime_still_fails_at_load(tmp_path, monkeypatch, bad):
    _write_cfg(tmp_path, monkeypatch, f"managed_repos: []\nwork:\n  runtime: {bad!r}\n")
    with pytest.raises(Exception):  # noqa: B017 - any load rejection; the type is the loader's
        config.load()


def test_validate_config_refuses_unregistered_runtime_at_use_with_registered_names():
    cfg = {
        "schema_version": config_schema.SCHEMA_VERSION,
        "providers": ["github"],
        "managed_repos": [],
        "work": {"runtime": "gascity"},
    }
    msgs = [p["message"] for p in config_validate.validate_config(cfg) if p["level"] == "error"]
    assert any(
        "unknown runtime 'gascity'" in m and "registered: " in m and "local" in m for m in msgs
    )


def test_get_runtime_raises_registered_names_message_at_use():
    with pytest.raises(ValueError, match=r"unknown runtime 'gascity'; registered: .*local"):
        runtime.get_runtime({"work": {"runtime": "gascity"}}, entry_points=lambda **kw: [])


# ---- `beadhive.runtimes` entry points, loaded lazily by name (bh-a68gk) -----------------------


class _EP:
    """A stand-in entry point recording whether it was imported."""

    def __init__(self, name, obj=None, *, error=None, dist="gc-pkg"):
        self.name = name
        self._obj, self._error = obj, error
        self.loads = 0
        self.dist = type("D", (), {"name": dist})()

    def load(self):
        self.loads += 1
        if self._error is not None:
            raise self._error
        return self._obj


def _eps(*eps):
    return lambda group=None: list(eps) if group == "beadhive.runtimes" else []


def test_entry_point_factory_resolves_and_only_that_entry_point_is_imported(clean_registry):
    marker = _FakeRuntime()
    wanted = _EP("gascity", lambda cfg, entry: marker)
    other = _EP("other", lambda cfg, entry: None)
    got = runtime.get_runtime({"work": {"runtime": "gascity"}}, entry_points=_eps(other, wanted))
    assert got is marker
    assert wanted.loads == 1
    assert other.loads == 0


def test_entry_point_register_callable_is_supported(clean_registry):
    marker = _FakeRuntime()
    ep = _EP("gascity", lambda: runtime.register_runtime("gascity", lambda cfg, entry: marker))
    assert runtime.get_runtime({"work": {"runtime": "gascity"}}, entry_points=_eps(ep)) is marker


def test_config_load_then_get_runtime_resolves_entry_point(tmp_path, monkeypatch, clean_registry):
    _write_cfg(tmp_path, monkeypatch, "managed_repos: []\nwork:\n  runtime: gascity\n")
    marker = _FakeRuntime()
    ep = _EP("gascity", lambda cfg, entry: marker)
    cfg = config.load()
    assert runtime.get_runtime(cfg, None, entry_points=_eps(ep)) is marker


def test_per_hive_runtime_selects_that_hives_runtime(clean_registry):
    host_rt, hive_rt = _FakeRuntime(), _FakeRuntime()
    runtime.register_runtime("hostrt", lambda cfg, entry: host_rt)
    runtime.register_runtime("hivert", lambda cfg, entry: hive_rt)
    cfg = {"work": {"runtime": "hostrt"}}
    entry = {"work": {"runtime": "hivert"}}
    assert runtime.get_runtime(cfg, entry) is hive_rt
    assert runtime.get_runtime(cfg, {"work": {}}) is host_rt


def test_missing_package_message_names_runtime_and_group(clean_registry):
    with pytest.raises(ValueError) as ei:
        runtime.get_runtime({"work": {"runtime": "gascity"}}, entry_points=_eps())
    assert "'gascity'" in str(ei.value) and "beadhive.runtimes" in str(ei.value)
    assert "install the package" in str(ei.value)


def test_import_error_is_distinct(clean_registry):
    ep = _EP("gascity", error=ImportError("no module gc"))
    with pytest.raises(runtime_names.RuntimeEntryPointError) as ei:
        runtime.get_runtime({"work": {"runtime": "gascity"}}, entry_points=_eps(ep))
    assert ei.value.kind == "import_error"
    assert "'gascity'" in str(ei.value) and "beadhive.runtimes" in str(ei.value)
    assert "failed to import" in str(ei.value) and "gc-pkg" in str(ei.value)


def test_name_mismatch_is_distinct(clean_registry):
    ep = _EP("gascity", lambda: runtime.register_runtime("elsewhere", lambda c, e: None))
    with pytest.raises(runtime_names.RuntimeEntryPointError) as ei:
        runtime.get_runtime({"work": {"runtime": "gascity"}}, entry_points=_eps(ep))
    assert ei.value.kind == "name_mismatch"
    assert "did not register a runtime named 'gascity'" in str(ei.value)
    assert "beadhive.runtimes" in str(ei.value)


def test_three_failure_modes_have_distinct_messages(clean_registry):
    msgs = set()
    for ep in (
        None,
        _EP("gascity", error=ImportError("x")),
        _EP("gascity", lambda: None),
    ):
        eps = _eps(*([ep] if ep else []))
        with pytest.raises(ValueError) as ei:
            runtime.validate_runtime_name("gascity", entry_points=eps)
        msgs.add(str(ei.value).split(":")[0] + str(ei.value)[-40:])
    assert len(msgs) == 3


def test_validate_runtime_name_loads_entry_point_on_miss(clean_registry):
    ep = _EP("gascity", lambda cfg, entry: _FakeRuntime())
    assert runtime.validate_runtime_name("gascity", entry_points=_eps(ep)) == "gascity"
    # builtins validate without touching entry points
    assert runtime.validate_runtime_name("local", entry_points=_eps()) == "local"
