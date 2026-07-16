"""`rig init --observaloop` self-checks — the ws.observaloop adapter is faked (no live MCP
server / docker), so we assert the wiring + graceful degradation in isolation:

- the bh-shipped Grafana dashboard asset is valid JSON and references the REAL bh.* metric +
  attribute names emitted by beadhive/otel.py (so it can't drift into invented names);
- the happy path ensures+ups the per-rig profile, reshapes the profile collector with the
  CLI-metrics preset, then applies the dashboard via the adapter;
- the collector preset apply is independent of the visualizer (still applies when Grafana is off)
  and best-effort (a falsy apply warns + continues; rig init survives);
- each absence (observaloop unavailable, visualizer unreachable, no profile name) degrades to a
  warn-and-skip — never a raise;
- otel.enabled false warns but still proceeds (observaloop needs otel, but the profile is still
  ensured so a later flip just works);
- the `--observaloop` flag is threaded into rig.init, and an exploding installer never aborts
  rig init (best-effort fence).
"""

from __future__ import annotations

import json

from beadhive import config, hive

# ---- fake adapter -----------------------------------------------------------


class _FakeObservaloop:
    """Records calls to the bh.observaloop seam so tests can assert the wiring without a live
    MCP server. ``available`` / ``status`` drive the gating branches."""

    def __init__(self, available=True, status=None, preset_result="ok"):
        self._available = available
        self._status = status
        self._preset_result = preset_result
        self.calls: list[tuple] = []

    def is_available(self, cfg=None):
        self.calls.append(("is_available",))
        return self._available

    def ensure_profile(self, name, cfg=None):
        self.calls.append(("ensure_profile", name))
        return {"name": name}

    def up(self, name, cfg=None):
        self.calls.append(("up", name))
        return {"name": name, "up": True}

    def visualizer_status(self, cfg=None):
        self.calls.append(("visualizer_status",))
        return self._status

    def apply_collector_preset(self, profile, preset, cfg=None):
        # record the profile + the preset's pipeline order so tests can assert the real preset
        # (strip → promote → accumulate) reached the adapter.
        order = tuple(preset["metrics_pipeline_processors"])
        self.calls.append(("apply_collector_preset", profile, order))
        return self._preset_result

    def apply_dashboards(self, dashboard, cfg=None):
        self.calls.append(("apply_dashboards", dashboard.get("uid")))
        return {"uid": dashboard.get("uid"), "url": "http://localhost:3000/d/bh-telemetry"}

    def _tools(self):
        return [c[0] for c in self.calls]


def _patch(monkeypatch, fake):
    # _install_observaloop does `from . import observaloop`; patch the real module's attrs.
    from beadhive import observaloop

    for attr in (
        "is_available",
        "ensure_profile",
        "up",
        "apply_collector_preset",
        "visualizer_status",
        "apply_dashboards",
    ):
        monkeypatch.setattr(observaloop, attr, getattr(fake, attr))


_REACHABLE = {"visualizer": "grafana", "reachable": True}
_UNREACHABLE = {"visualizer": "grafana", "reachable": False}
_OTEL_ON = {"otel": {"enabled": True}}


# ---- dashboard asset --------------------------------------------------------


def test_dashboard_asset_valid_json_and_references_real_ws_names():
    raw = config.observaloop_dashboard_asset().read_text()
    model = json.loads(raw)  # must parse — a real Grafana dashboard model
    assert model["uid"] == "bh-telemetry"
    assert model["panels"]  # has panels

    # The Prometheus-normalized forms of the real OTLP instruments in beadhive/otel.py must all
    # appear in the queries (so the dashboard can't drift into invented metric names).
    for metric in (
        "bh_cli_invocations_total",
        "bh_cli_duration_seconds_bucket",
        "bh_mcp_tool_invocations_total",
        "bh_mcp_tool_duration_seconds_bucket",
        "bh_errors_total",
    ):
        assert metric in raw, metric
    # the resource/attribute labels used for the per-worktree breakdown + RED splits
    for label in ("bh_worktree", "observaloop_profile", "bh_hive", "bh_cli_command", "bh_mcp_tool"):
        assert label in raw, label
    # the bh.cli trace-nesting root is queried over Tempo
    assert 'name =~ \\"bh.cli.*\\"' in raw or "bh.cli.*" in raw


def test_dashboard_asset_references_agf_lifecycle_and_worktree_metrics():
    """AGF-lifecycle + worktree-events rows reference real shipped metric/attribute names.

    Mirrors test_dashboard_asset_valid_json_and_references_real_ws_names for the two new rows
    added by: every Prometheus-normalised metric name and attribute label
    must appear in the raw JSON so the panels can't drift from beadhive/otel.py.
    """
    raw = config.observaloop_dashboard_asset().read_text()
    model = json.loads(raw)

    # AGF-lifecycle and worktree-events rows must be present.
    row_titles = [p["title"] for p in model["panels"] if p.get("type") == "row"]
    assert any("AGF lifecycle" in t for t in row_titles), row_titles
    assert any("Worktree events" in t for t in row_titles), row_titles

    # Prometheus-normalised metric names for the new instruments (dots→underscores,
    # counters gain _total, second-unit histograms gain _seconds_bucket).
    for metric in (
        "bh_work_bead_transitions_total",
        "bh_work_merge_duration_seconds_bucket",
        "bh_work_validation_runs_total",
        "bh_worktree_events_total",
    ):
        assert metric in raw, metric

    # Attribute labels that dimension the new panels.
    for label in (
        "bh_bead_transition",
        "bh_merge_kind",
        "bh_validation_result",
        "bh_worktree_op",
        "bh_worktree_outcome",
    ):
        assert label in raw, label

    # Agent-dispatch Tempo panel queries invoke_agent spans.
    assert "invoke_agent" in raw


def test_dashboard_asset_has_commit_flow_row_and_window_var(monkeypatch):
    """hqfy.4: the Commit Flow row + $flow_window var reference the real shipped flow-metric
    names (PromQL-normalised), and the counter panels window via increase(...[$flow_window])."""
    raw = config.observaloop_dashboard_asset().read_text()
    model = json.loads(raw)

    # $flow_window custom var: default 1h, options 5m/15m/1h/1d, robustness preserved on bh_rig.
    tvars = {t["name"]: t for t in model["templating"]["list"]}
    assert "flow_window" in tvars
    fw = tvars["flow_window"]
    assert fw["type"] == "custom" and fw["current"]["value"] == "1h"
    assert {o["value"] for o in fw["options"]} == {"5m", "15m", "1h", "1d"}
    assert tvars["bh_hive"]["query"] == "label_values(bh_hive)"
    assert tvars["bh_hive"]["allValue"] == ".*"

    # Commit Flow row present.
    row_titles = [p["title"] for p in model["panels"] if p.get("type") == "row"]
    assert any("Commit Flow" in t for t in row_titles), row_titles

    # Every commit-flow instrument (PromQL-normalised) is referenced by some panel.
    for metric in (
        "bh_work_merge_outcome_total",
        "bh_work_cycle_time_seconds_bucket",
        "bh_work_cycle_time_active_seconds_bucket",
        "bh_work_stage_coding_seconds_bucket",
        "bh_work_stage_review_wait_seconds_bucket",
        "bh_work_stage_merge_latency_seconds_bucket",
        "bh_work_rework_count_bucket",
        "bh_work_merge_slot_wait_seconds_bucket",
        "bh_work_merge_slot_hold_seconds_bucket",
        "bh_work_validation_duration_seconds_bucket",
        "bh_worktree_op_duration_seconds_bucket",
    ):
        assert metric in raw, metric

    # bh.bead is no longer a metric label anywhere in the dashboard queries.
    assert "bh_bead=" not in raw and "bh_bead}" not in raw and "(bh_bead)" not in raw

    # The re-unitted counter panels window with increase(...[$flow_window]).
    assert "increase(bh_work_bead_transitions_total" in raw
    assert "increase(bh_work_merge_outcome_total" in raw
    assert "[$flow_window]" in raw


# ---- happy path -------------------------------------------------------------


def test_install_applies_dashboard_when_available_and_visualizer_on(monkeypatch, capsys):
    fake = _FakeObservaloop(available=True, status=_REACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "acme-api"})
    # profile ensured + brought up under the derived (sanitized) name, then dashboard applied.
    assert ("ensure_profile", "acme-api") in fake.calls
    assert ("up", "acme-api") in fake.calls
    assert ("apply_dashboards", "bh-telemetry") in fake.calls
    out = capsys.readouterr().out
    assert "profile 'acme-api' ensured" in out
    assert "dashboard applied" in out


def test_profile_name_is_derived_and_sanitized(monkeypatch):
    fake = _FakeObservaloop(available=True, status=_REACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "My_Hive.v2"})
    assert ("ensure_profile", "my-hive-v2") in fake.calls


# ---- CLI-metrics collector preset -------------------------------------------


_PRESET_ORDER = (
    "resource/profile",
    "resource/strip_instance",
    "transform/promote_bh_attrs",
    "deltatocumulative",
    "batch",
)


def test_applies_metrics_preset_to_profile_collector(monkeypatch, capsys):
    # Happy path: after ensure+up, the REAL shipped preset (its strip → promote → accumulate
    # pipeline order) is applied to the rig's profile collector via the adapter.
    fake = _FakeObservaloop(available=True, status=_REACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "acme-api"})
    assert ("apply_collector_preset", "acme-api", _PRESET_ORDER) in fake.calls
    # preset reshape happens after up but before the dashboard apply
    tools = fake._tools()
    assert tools.index("apply_collector_preset") > tools.index("up")
    assert tools.index("apply_collector_preset") < tools.index("apply_dashboards")
    assert "CLI-metrics collector preset applied" in capsys.readouterr().out


def test_preset_applied_even_when_visualizer_unreachable(monkeypatch):
    # The collector reshape is independent of Grafana — it still applies when the visualizer is
    # off (only the dashboard is gated on the visualizer).
    fake = _FakeObservaloop(available=True, status=_UNREACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "ws"})
    assert ("apply_collector_preset", "ws", _PRESET_ORDER) in fake.calls
    assert "apply_dashboards" not in fake._tools()


def test_preset_skipped_when_observaloop_unavailable(monkeypatch):
    # The whole installer stops at the availability gate — no preset apply attempted.
    fake = _FakeObservaloop(available=False)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "ws"})
    assert "apply_collector_preset" not in fake._tools()


def test_hive_init_survives_preset_apply_failure(monkeypatch, capsys):
    # A falsy adapter result (collector tool unavailable / set failed) warns and continues — the
    # rest of the installer (dashboard) still runs; rig init never aborts.
    fake = _FakeObservaloop(available=True, status=_REACHABLE, preset_result=None)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "ws"})
    assert ("apply_dashboards", "bh-telemetry") in fake.calls  # later steps still run
    assert "collector preset apply failed" in capsys.readouterr().err


# ---- graceful skips ---------------------------------------------------------


def test_skips_everything_when_observaloop_unavailable(monkeypatch, capsys):
    fake = _FakeObservaloop(available=False)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "ws"})
    assert fake._tools() == ["is_available"]  # nothing past the availability gate
    assert "observaloop unavailable" in capsys.readouterr().err


def test_skips_dashboard_when_visualizer_unreachable(monkeypatch, capsys):
    fake = _FakeObservaloop(available=True, status=_UNREACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "ws"})
    # profile still ensured+up, but the dashboard is skipped (no grafana_* tool reachable)
    assert ("ensure_profile", "ws") in fake.calls
    assert ("up", "ws") in fake.calls
    assert "apply_dashboards" not in fake._tools()
    assert "visualizer not reachable" in capsys.readouterr().err


def test_skips_dashboard_when_visualizer_status_none(monkeypatch):
    # status None (adapter call failed) is treated as not-reachable → no apply
    fake = _FakeObservaloop(available=True, status=None)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {"prefix": "ws"})
    assert "apply_dashboards" not in fake._tools()


def test_returns_early_when_no_profile_name(monkeypatch, capsys):
    fake = _FakeObservaloop(available=True, status=_REACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop(_OTEL_ON, {})  # no prefix → no derivable profile name
    assert fake.calls == []  # not even is_available is reached
    assert "could not derive a profile name" in capsys.readouterr().err


def test_warns_but_proceeds_when_otel_disabled(monkeypatch, capsys):
    # otel off: observaloop needs otel, so warn — but still ensure the profile + apply the
    # dashboard (so a later otel.enabled flip just works).
    fake = _FakeObservaloop(available=True, status=_REACHABLE)
    _patch(monkeypatch, fake)
    hive._install_observaloop({"otel": {"enabled": False}}, {"prefix": "ws"})
    assert ("apply_dashboards", "bh-telemetry") in fake.calls
    assert "otel.enabled is false" in capsys.readouterr().err


# ---- flag wiring + best-effort fence ----------------------------------------


def _stub_hive_init_prereqs(monkeypatch, tmp_path):
    """Stub the heavy rig.init prerequisites (identity / registry / bd init) so we can exercise
    the installer-dispatch tail in isolation."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hive, "workspace_identity", lambda cwd=None: ("github", "acme", "api"))
    monkeypatch.setattr(hive.config, "load", lambda: _OTEL_ON)
    monkeypatch.setattr(hive.registry, "classify", lambda *a, **k: "org-native")
    monkeypatch.setattr(hive.registry, "derive_prefix", lambda *a, **k: ("ac-api", []))
    monkeypatch.setattr(hive.registry, "org_policy", lambda *a, **k: "")
    monkeypatch.setattr(hive.registry, "register", lambda *a, **k: None)
    monkeypatch.setattr(hive, "run", lambda *a, **k: None)


def test_flag_threaded_into_install(monkeypatch, tmp_path):
    _stub_hive_init_prereqs(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(
        hive, "_install_observaloop", lambda cfg, entry: seen.update(cfg=cfg, entry=entry)
    )
    hive.init(observaloop=True)
    assert seen["entry"] == {"prefix": "ac-api"}  # the derived prefix is passed through


def test_no_flag_does_not_install(monkeypatch, tmp_path):
    _stub_hive_init_prereqs(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(hive, "_install_observaloop", lambda *a, **k: called.append(1))
    hive.init(observaloop=False)
    assert called == []


def test_hive_init_succeeds_even_if_installer_explodes(monkeypatch, tmp_path, capsys):
    _stub_hive_init_prereqs(monkeypatch, tmp_path)

    def _boom(cfg, entry):
        raise RuntimeError("docker daemon down")

    monkeypatch.setattr(hive, "_install_observaloop", _boom)
    hive.init(observaloop=True)  # must NOT raise — best-effort fence
    out = capsys.readouterr()
    assert "hive 'ac-api' ready" in out.out
    assert "skipped (docker daemon down)" in out.err
