"""ws.observaloop — the gated, best-effort fastmcp MCP-client seam to observaloop. The runnable
acceptance check for bead.

observaloop ships as a Claude Code plugin whose automation is reachable only over the
``observaloop-mcp`` stdio MCP server, and ``fastmcp`` is an *optional* extra — neither is needed
here because every "reachable" assertion fakes the single async ``_call_tool`` seam (or the
launch-command resolver), exactly as ``test_otel`` fakes ``_load_otel``. That proves the wrappers
drive the right tools + the protocol-matched endpoint logic without a live server, while the
absent-path asserts the graceful no-op + one-time hint. A live test against a real
``observaloop-mcp`` is import/availability-gated at the bottom.
"""

from __future__ import annotations

import pytest

from ws import config, observaloop


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Isolate each test: clear the one-time-hint guard and scrub the profile-override env so
    resolution depends only on what the test injects."""
    observaloop._hint_shown = False
    monkeypatch.delenv("WS_OBSERVALOOP_PROFILE", raising=False)
    yield
    observaloop._hint_shown = False


# ---- command resolution ------------------------------------------------------


def test_resolve_command_prefers_config_override_string(monkeypatch):
    """A string ``observaloop.command`` override is shlex-split into argv (wins over discovery)."""
    monkeypatch.setattr(config, "observaloop_cfg", lambda cfg=None: {"command": "obs-mcp --stdio"})
    assert observaloop._resolve_command() == ["obs-mcp", "--stdio"]


def test_resolve_command_prefers_config_override_list(monkeypatch):
    """A list override is used verbatim (stringified), without touching plugin discovery."""
    monkeypatch.setattr(
        config, "observaloop_cfg", lambda cfg=None: {"command": ["uv", "run", "observaloop-mcp"]}
    )
    # Point discovery at nothing to prove the override short-circuits it.
    monkeypatch.setattr(observaloop, "_newest_plugin_dir", lambda: None)
    assert observaloop._resolve_command() == ["uv", "run", "observaloop-mcp"]


def test_resolve_command_discovers_newest_plugin(monkeypatch, tmp_path):
    """With no override, discovery globs the plugin cache, picks the HIGHEST version, and builds the
    ``uv run --directory <install> observaloop-mcp`` argv."""
    base = tmp_path / "observaloop" / "observaloop"
    for ver in ("0.1.0", "0.1.2", "0.2.1", "0.2.0"):
        (base / ver).mkdir(parents=True)
    monkeypatch.setattr(config, "observaloop_cfg", lambda cfg=None: {})
    monkeypatch.setattr(observaloop, "_PLUGIN_BASE", str(base))

    cmd = observaloop._resolve_command()

    assert cmd == ["uv", "run", "--directory", str(base / "0.2.1"), "observaloop-mcp"]


def test_resolve_command_none_when_nothing_resolves(monkeypatch, tmp_path):
    """No override + no plugin install → ``None`` (the single 'unavailable' signal)."""
    monkeypatch.setattr(config, "observaloop_cfg", lambda cfg=None: {})
    monkeypatch.setattr(observaloop, "_PLUGIN_BASE", str(tmp_path / "absent"))
    assert observaloop._resolve_command() is None


def test_version_key_orders_numerically(monkeypatch):
    """0.2.10 sorts above 0.2.9 (numeric segments, not lexical)."""
    assert observaloop._version_key("0.2.10") > observaloop._version_key("0.2.9")


# ---- graceful absence (no command resolves) ----------------------------------


def _make_unresolvable(monkeypatch):
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: None)


def test_is_available_false_with_one_time_hint(monkeypatch):
    """Unresolvable server → ``is_available`` is False and emits the install hint exactly once."""
    _make_unresolvable(monkeypatch)
    warnings: list[tuple] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr("ws.log.get_logger", lambda *_a, **_k: _Logger())

    assert observaloop.is_available() is False
    assert observaloop.is_available() is False  # second call must not re-warn
    assert [e for e, _ in warnings] == ["observaloop_install_hint"]
    assert observaloop._INSTALL_HINT in warnings[0][1]["hint"]


# ---- three correctly-attributed unavailability hints -------


def _capture_warnings(monkeypatch):
    """Route ``ws.log`` warnings into a list of ``(event, kwargs)`` for hint assertions."""
    warnings: list[tuple] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr("ws.log.get_logger", lambda *_a, **_k: _Logger())
    return warnings


def test_is_available_command_unresolved_emits_plugin_hint(monkeypatch):
    """(b) No command resolves → the observaloop-plugin hint, and the fastmcp check never runs (the
    plugin-absent case short-circuits before the extra check)."""
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: None)
    monkeypatch.setattr(
        observaloop, "_fastmcp_importable", lambda: pytest.fail("must not probe fastmcp")
    )
    warnings = _capture_warnings(monkeypatch)

    assert observaloop.is_available() is False
    assert [e for e, _ in warnings] == ["observaloop_install_hint"]
    assert warnings[0][1]["hint"] == observaloop._INSTALL_HINT


def test_is_available_fastmcp_missing_emits_mcp_extra_hint(monkeypatch):
    """(a) Command resolves but fastmcp is unimportable → the broken-install hint (fastmcp is a
    core dep, NOT observaloop-not-found), warn-once, and the server is never probed."""
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: ["uv", "run", "obs-mcp"])
    monkeypatch.setattr(observaloop, "_fastmcp_importable", lambda: False)
    probed: list = []

    async def _ping(_command):
        probed.append(_command)
        return True

    monkeypatch.setattr(observaloop, "_ping", _ping)
    warnings = _capture_warnings(monkeypatch)

    assert observaloop.is_available() is False
    assert observaloop.is_available() is False  # warn-once across calls
    assert [e for e, _ in warnings] == ["observaloop_install_hint"]
    hint = warnings[0][1]["hint"]
    assert hint == observaloop._MCP_EXTRA_HINT
    assert hint != observaloop._INSTALL_HINT  # not misdiagnosed as a missing plugin
    assert "mcp" in hint
    assert probed == []  # skips the probe that would ImportError on fastmcp


def test_is_available_unreachable_emits_reachability_hint(monkeypatch):
    """(c) Command + fastmcp present but the ping fails → the reachability hint, distinct from the
    plugin/extra hints; never raises out of is_available."""
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: ["obs-mcp"])
    monkeypatch.setattr(observaloop, "_fastmcp_importable", lambda: True)

    async def _ping(_command):
        raise RuntimeError("docker not running")

    monkeypatch.setattr(observaloop, "_ping", _ping)
    warnings = _capture_warnings(monkeypatch)

    assert observaloop.is_available() is False
    assert observaloop.is_available() is False  # warn-once
    assert [e for e, _ in warnings] == ["observaloop_install_hint"]
    hint = warnings[0][1]["hint"]
    assert hint == observaloop._UNREACHABLE_HINT
    assert hint != observaloop._INSTALL_HINT
    assert hint != observaloop._MCP_EXTRA_HINT


def test_is_available_true_when_resolved_extra_and_ping_ok(monkeypatch):
    """Happy path: command resolves, fastmcp importable, ping answers → True with no hint."""
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: ["obs-mcp"])
    monkeypatch.setattr(observaloop, "_fastmcp_importable", lambda: True)

    async def _ping(_command):
        return True

    monkeypatch.setattr(observaloop, "_ping", _ping)
    warnings = _capture_warnings(monkeypatch)

    assert observaloop.is_available() is True
    assert warnings == []


def test_three_unavailability_hints_are_distinct():
    """The plugin / mcp-extra / reachability hints are three separate constants."""
    hints = {observaloop._INSTALL_HINT, observaloop._MCP_EXTRA_HINT, observaloop._UNREACHABLE_HINT}
    assert len(hints) == 3


def test_fastmcp_importable_false_when_spec_missing(monkeypatch):
    """A missing fastmcp spec → False, without importing it (find_spec locates, not executes)."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    assert observaloop._fastmcp_importable() is False


def test_fastmcp_importable_true_when_spec_present(monkeypatch):
    """A present fastmcp spec → True."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    assert observaloop._fastmcp_importable() is True


@pytest.mark.parametrize(
    "call",
    [
        lambda: observaloop.ensure_profile("rig"),
        lambda: observaloop.up("rig"),
        lambda: observaloop.down("rig"),
        lambda: observaloop.endpoint_for("rig", "grpc"),
        lambda: observaloop.apply_dashboards({"title": "x"}),
        lambda: observaloop.import_dashboards("rig", "/repo"),
        lambda: observaloop.apply_collector_preset("rig", {"processors": {}}),
    ],
)
def test_wrappers_noop_to_none_when_absent(monkeypatch, call):
    """Every wrapper returns the ``None`` sentinel — never raises, never blocks — when absent."""
    _make_unresolvable(monkeypatch)
    assert call() is None


def test_wrappers_never_raise_when_call_fails(monkeypatch):
    """A resolved-but-failing server (the async call raises) degrades to a warning + ``None``."""
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: ["obs-mcp"])

    async def _boom(*_a, **_k):
        raise RuntimeError("docker not running")

    monkeypatch.setattr(observaloop, "_call_tool", _boom)
    assert observaloop.up("rig") is None
    assert observaloop.ensure_profile("rig") is None


# ---- reachable: wrappers drive the right tools (fake MCP client) -------------


class _FakeClient:
    """Records each ``(tool, args)`` call and replays canned per-tool return data."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []


def _fake_dispatch(monkeypatch, responses: dict):
    """Make ``_call_tool`` a fake async seam returning ``responses[tool]`` and recording calls, with
    a resolvable command so ``_invoke`` reaches it."""
    recorder = _FakeClient(responses)
    monkeypatch.setattr(observaloop, "_resolve_command", lambda cfg=None: ["obs-mcp"])

    async def _call(command, tool, args):
        recorder.calls.append((tool, args))
        return responses.get(tool, {})

    monkeypatch.setattr(observaloop, "_call_tool", _call)
    return recorder


def test_ensure_profile_up_down_drive_tools(monkeypatch):
    """ensure_profile/up/down call profile_create/up/down with the name + return the data."""
    rec = _fake_dispatch(
        monkeypatch,
        {
            "profile_create": {"name": "rig", "otlp_grpc_port": 4319},
            "profile_up": {"started": True},
            "profile_down": {"stopped": True},
        },
    )

    assert observaloop.ensure_profile("rig") == {"name": "rig", "otlp_grpc_port": 4319}
    assert observaloop.up("rig") == {"started": True}
    assert observaloop.down("rig") == {"stopped": True}
    assert rec.calls == [
        ("profile_create", {"name": "rig"}),
        ("profile_up", {"name": "rig"}),
        ("profile_down", {"name": "rig"}),
    ]


def test_endpoint_for_grpc_uses_grpc_port(monkeypatch):
    """protocol=grpc → the manifest's otlp_grpc_port, scheme-less localhost form."""
    _fake_dispatch(
        monkeypatch,
        {"profile_status": {"manifest": {"otlp_grpc_port": 4319, "otlp_http_port": 4320}}},
    )
    assert observaloop.endpoint_for("rig", config.OTEL_PROTOCOL_GRPC) == "localhost:4319"


def test_endpoint_for_http_uses_http_port(monkeypatch):
    """protocol=http/protobuf → the manifest's otlp_http_port, http:// form."""
    _fake_dispatch(
        monkeypatch,
        {"profile_status": {"manifest": {"otlp_grpc_port": 4319, "otlp_http_port": 4320}}},
    )
    assert observaloop.endpoint_for("rig", config.OTEL_PROTOCOL_HTTP) == "http://localhost:4320"


def test_endpoint_for_none_when_port_missing(monkeypatch):
    """A profile whose manifest lacks the requested port → ``None`` (never a half endpoint)."""
    _fake_dispatch(monkeypatch, {"profile_status": {"manifest": {"otlp_http_port": 4320}}})
    assert observaloop.endpoint_for("rig", config.OTEL_PROTOCOL_GRPC) is None


def test_apply_and_import_dashboards_pass_through(monkeypatch):
    """The Phase-C dashboard wrappers pass their payloads straight to the grafana/import tools."""
    rec = _fake_dispatch(
        monkeypatch,
        {"grafana_apply_dashboard": {"uid": "abc"}, "profile_import": {"imported": []}},
    )
    assert observaloop.apply_dashboards({"title": "d"}) == {"uid": "abc"}
    assert observaloop.import_dashboards("rig", "/repo") == {"imported": []}
    assert rec.calls == [
        ("grafana_apply_dashboard", {"dashboard": {"title": "d"}}),
        ("profile_import", {"name": "rig", "repo_dir": "/repo"}),
    ]


# ---- apply_collector_preset (metrics reshape; merge-and-set) ----------------


_PRESET = {
    "processors": {
        "resource/strip_instance": {
            "attributes": [{"key": "service.instance.id", "action": "delete"}]
        },
        "transform/promote_ws_attrs": {"metric_statements": [{"context": "datapoint"}]},
        "deltatocumulative": {},
    },
    "metrics_pipeline_processors": [
        "resource/profile",
        "resource/strip_instance",
        "transform/promote_ws_attrs",
        "deltatocumulative",
        "batch",
    ],
}


def _sent_config(rec) -> dict:
    """Parse the YAML-string ``config`` arg passed to ``collector_set_config`` back into a dict.

    ``collector_set_config(config: str, profile)`` takes a YAML string (not a dict), so the adapter
    serializes the merged config before sending; tests re-parse it to assert on the resulting
    pipeline shape."""
    from ruamel.yaml import YAML

    set_args = rec.calls[1][1]
    return YAML(typ="safe").load(set_args["config"])


def _profile_collector() -> dict:
    """A minimal pre-reshape profile collector: metrics pipeline with only resource/profile+batch,
    plus an intact traces pipeline to prove it's left untouched."""
    return {
        "processors": {"resource/profile": {}, "batch": {}},
        "exporters": {"otlp/lgtm": {}},
        "service": {
            "pipelines": {
                "metrics": {
                    "receivers": ["otlp"],
                    "processors": ["resource/profile", "batch"],
                    "exporters": ["otlp/lgtm"],
                },
                "traces": {
                    "receivers": ["otlp"],
                    "processors": ["batch"],
                    "exporters": ["otlp/lgtm"],
                },
            }
        },
    }


def test_apply_collector_preset_merges_and_sets(monkeypatch):
    """get → merge (three processors added, metrics order replaced) → set; traces left intact."""
    rec = _fake_dispatch(
        monkeypatch,
        {
            "collector_get_config": _profile_collector(),
            "collector_set_config": {"applied": True},
        },
    )

    assert observaloop.apply_collector_preset("rig", _PRESET) == {"applied": True}

    get_tool, get_args = rec.calls[0]
    assert (get_tool, get_args) == ("collector_get_config", {"profile": "rig"})

    set_tool, set_args = rec.calls[1]
    assert set_tool == "collector_set_config"
    assert set_args["profile"] == "rig"
    # config crosses the seam as a YAML string (collector_set_config(config: str, …)), not a dict
    assert isinstance(set_args["config"], str)
    sent = _sent_config(rec)
    # three new processors merged in alongside the existing ones
    assert set(sent["processors"]) == {
        "resource/profile",
        "batch",
        "resource/strip_instance",
        "transform/promote_ws_attrs",
        "deltatocumulative",
    }
    # metrics pipeline reordered; receivers/exporters preserved
    assert sent["service"]["pipelines"]["metrics"]["processors"] == [
        "resource/profile",
        "resource/strip_instance",
        "transform/promote_ws_attrs",
        "deltatocumulative",
        "batch",
    ]
    assert sent["service"]["pipelines"]["metrics"]["exporters"] == ["otlp/lgtm"]
    # traces pipeline untouched
    assert sent["service"]["pipelines"]["traces"]["processors"] == ["batch"]


def test_apply_collector_preset_does_not_mutate_fetched_config(monkeypatch):
    """The merge builds a fresh config — the dict from collector_get_config is never mutated."""
    original = _profile_collector()
    _fake_dispatch(
        monkeypatch,
        {"collector_get_config": original, "collector_set_config": {"applied": True}},
    )

    observaloop.apply_collector_preset("rig", _PRESET)

    metrics = original["service"]["pipelines"]["metrics"]
    assert metrics["processors"] == ["resource/profile", "batch"]
    assert "resource/strip_instance" not in original["processors"]


def test_apply_collector_preset_noop_when_get_returns_no_config(monkeypatch):
    """A get that yields no usable config dict → no set is attempted (never a half-apply)."""
    rec = _fake_dispatch(monkeypatch, {"collector_get_config": None})

    assert observaloop.apply_collector_preset("rig", _PRESET) is None
    assert [tool for tool, _ in rec.calls] == ["collector_get_config"]


def test_apply_collector_preset_unwraps_config_key(monkeypatch):
    """A get result wrapped under a ``config`` key is unwrapped before merging."""
    rec = _fake_dispatch(
        monkeypatch,
        {
            "collector_get_config": {"config": _profile_collector()},
            "collector_set_config": {"applied": True},
        },
    )

    observaloop.apply_collector_preset("rig", _PRESET)

    sent = _sent_config(rec)
    assert "resource/strip_instance" in sent["processors"]


def test_apply_collector_preset_sends_yaml_string_not_dict(monkeypatch):
    """Regression for: collector_set_config(config: str, profile) requires a YAML
    STRING — the live server rejects a dict (``Input should be a valid string``), silently no-op'ing
    the apply. Assert the value passed to the set tool is a ``str`` carrying the reshaped
    pipeline."""
    rec = _fake_dispatch(
        monkeypatch,
        {
            "collector_get_config": _profile_collector(),
            "collector_set_config": {"applied": True},
        },
    )

    observaloop.apply_collector_preset("rig", _PRESET)

    set_args = rec.calls[1][1]
    assert isinstance(set_args["config"], str)
    assert not isinstance(set_args["config"], dict)
    # the string is real, applied YAML — the reshaped metrics pipeline round-trips back out
    sent = _sent_config(rec)
    assert sent["service"]["pipelines"]["metrics"]["processors"] == [
        "resource/profile",
        "resource/strip_instance",
        "transform/promote_ws_attrs",
        "deltatocumulative",
        "batch",
    ]


def test_apply_collector_preset_parses_yaml_string_get(monkeypatch):
    """The live ``collector_get_config`` returns the config as a YAML STRING under ``config``; it is
    parsed to a dict before the merge (parse-on-get), mirroring dump-on-set."""
    import io

    from ruamel.yaml import YAML

    buf = io.StringIO()
    YAML(typ="safe").dump(_profile_collector(), buf)
    rec = _fake_dispatch(
        monkeypatch,
        {
            "collector_get_config": {"profile": "rig", "config": buf.getvalue()},
            "collector_set_config": {"applied": True},
        },
    )

    assert observaloop.apply_collector_preset("rig", _PRESET) == {"applied": True}

    sent = _sent_config(rec)
    assert "resource/strip_instance" in sent["processors"]
    assert sent["service"]["pipelines"]["metrics"]["processors"][1] == "resource/strip_instance"


def test_import_safe_without_fastmcp():
    """``import ws.observaloop`` must succeed even if the optional fastmcp extra is absent — the
    module touches fastmcp only lazily inside the client builder."""
    import importlib

    importlib.reload(observaloop)  # re-exec module body; must not import fastmcp


# ---- subprocess noise suppression (_build_client) ----------------------------


def test_build_client_redirects_stderr_to_devnull(monkeypatch):
    """_build_client passes ``log_file=Path(os.devnull)`` so the spawned subprocess's stderr
    (FastMCP startup banner + gRPC fork-fd warning) is silenced on the user's terminal."""
    import os
    from pathlib import Path

    captured: list[dict] = []

    class _FakeTransport:
        def __init__(self, command, args, env=None, log_file=None, **kwargs):
            captured.append({"env": env, "log_file": log_file})

    class _FakeClient:
        def __init__(self, transport):
            pass

    import fastmcp
    import fastmcp.client.transports as _fm_transports

    monkeypatch.setattr(_fm_transports, "StdioTransport", _FakeTransport)
    monkeypatch.setattr(fastmcp, "Client", _FakeClient)

    observaloop._build_client(["uv", "run", "observaloop-mcp"])

    assert len(captured) == 1
    assert captured[0]["log_file"] == Path(os.devnull)


def test_build_client_sets_grpc_quiet_env(monkeypatch):
    """_build_client passes ``GRPC_VERBOSITY=NONE`` (and ``GRPC_TRACE=""``) in the subprocess env
    so the gRPC C-core fork-fd warning is suppressed in the spawned observaloop-mcp process."""
    captured: list[dict] = []

    class _FakeTransport:
        def __init__(self, command, args, env=None, log_file=None, **kwargs):
            captured.append({"env": env})

    class _FakeClient:
        def __init__(self, transport):
            pass

    import fastmcp
    import fastmcp.client.transports as _fm_transports

    monkeypatch.setattr(_fm_transports, "StdioTransport", _FakeTransport)
    monkeypatch.setattr(fastmcp, "Client", _FakeClient)

    observaloop._build_client(["uv", "run", "observaloop-mcp"])

    env = captured[0]["env"] or {}
    assert env.get("GRPC_VERBOSITY") == "NONE"
    assert "GRPC_TRACE" in env


# ---- live integration (skipped unless a real observaloop-mcp is reachable) ---


@pytest.mark.integration
def test_live_profile_roundtrip():
    """Drive a real observaloop-mcp end to end. Skipped unless fastmcp is installed AND a server is
    actually reachable — so the default suite never depends on observaloop/docker being present."""
    pytest.importorskip("fastmcp")
    if not observaloop.is_available():
        pytest.skip("observaloop-mcp not resolvable/reachable in this environment")
    name = "ws-x43-live-test"
    assert observaloop.ensure_profile(name) is not None
    endpoint = observaloop.endpoint_for(name, config.OTEL_PROTOCOL_HTTP)
    assert endpoint is None or endpoint.startswith("http://localhost:")
