"""Spike bh-rktrh: the ``tracker.adapter`` plugin category, exercised against the REAL kernel.

Every test runs offline (conftest forbids sockets, sleeps and subprocesses). Nothing under
``src/`` or ``packages/`` imports any of this.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import compose_proto
import pytest
from compose_proto import TRACKER_ADAPTER, TRACKER_ADAPTER_PORT, TrackerAdapterProvider
from config_proto import ALLOWED_PATHS, FORBIDDEN_KEY, PROPOSED_DEFAULTS, all_paths, flatten
from credentials_proto import REDACTED, ResolvedToken, build_token_source, scrub
from jsonschema import Draft202012Validator
from status_proto import build_status, credential_facts, render_json, render_text
from stub_adapters import ReadOnlyStub, build_adapter
from tracker_contracts import CommentWriter, LabelWriter, Operation, TrackerAdapter

from beadhive.kernel.plugins import BuiltInManifestSource, discover_plugins
from beadhive.modules.config.application.plugin_fragments import compose_plugin_config
from beadhive.modules.config.application.resolution import SourceLayer
from beadhive_plugins.contracts import (
    CapabilityBindingError,
    DiagnosticCode,
    ManifestDocument,
    ManifestProvenance,
)

HERE = Path(__file__).parent
REPO = HERE.parents[3]
MANIFEST = json.loads((HERE / "plugin.json").read_text())
SHAPED = "ghp_" + "A1b2C3d4E5" * 4  # matches the token-shape pattern
UNSHAPED = "plain-secret-with-no-known-prefix"  # only exact-value replacement can catch it


def _source(manifest: dict, name: str) -> BuiltInManifestSource:
    return BuiltInManifestSource(
        (
            ManifestDocument(
                ManifestProvenance("built-in", f"{name}.json"),
                json.dumps(manifest).encode(),
            ),
        )
    )


def _other_provider_manifest() -> dict:
    other = copy.deepcopy(MANIFEST)
    other["plugin_id"] = "tracker-other"
    other["configuration"] = {
        "legacy_namespaces": [],
        "namespace": "plugins.tracker-other",
        "schema_artifact": None,
    }
    other["presentation"] = {
        "cli": [{"capability_id": "tracker.adapter", "command": "plugin tracker-other status"}]
    }
    return other


def _providers(*ids: str) -> tuple[TrackerAdapterProvider, ...]:
    return tuple(TrackerAdapterProvider(pid, build_adapter) for pid in ids)


def _on(**overrides) -> dict:
    cfg = copy.deepcopy(PROPOSED_DEFAULTS)
    cfg.update(enabled=True, repo="octo/widgets")
    for dotted, value in overrides.items():
        node = cfg
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    return cfg


def _no_token():
    return None


def _compose(configs, *, ids=("tracker-github",), manifests=None, kernel=None, token=_no_token):
    sources = [_source(m, m["plugin_id"]) for m in (manifests or [MANIFEST])]
    return compose_proto.compose_tracker_adapter(
        configs, sources=sources, providers=_providers(*ids), token_source=token, kernel=kernel
    )


# ---- 1. the category is a plain kernel capability ------------------------------------------


def test_manifest_is_valid_and_owns_the_capability():
    result = discover_plugins(
        [_source(MANIFEST, "tracker-github")],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables={"gh": "2.60.0"},
    )
    assert result.errors == ()
    assert result.owner_of(TRACKER_ADAPTER) == "tracker-github"
    assert result.plugins[0].manifest.configuration_namespace == "plugins.tracker-github"


def test_manifest_matches_the_published_wire_schema():
    schema_path = max((REPO / "docs/schemas/wire").glob("v*/plugin-manifest-v1.schema.json"))
    Draft202012Validator(json.loads(schema_path.read_text())).validate(MANIFEST)


def test_missing_optional_gh_warns_but_does_not_block_selection():
    result = discover_plugins(
        [_source(MANIFEST, "tracker-github")],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables={},
    )
    assert result.owner_of(TRACKER_ADAPTER) == "tracker-github"
    assert [d.code for d in result.diagnostics] == [DiagnosticCode.OPTIONAL_EXECUTABLE_MISSING]
    assert result.diagnostics[0].severity.value == "warning"


def test_manifest_credential_sources_are_an_unordered_set():
    """The kernel SORTS declared sources, so manifest order cannot carry precedence."""
    result = discover_plugins(
        [_source(MANIFEST, "tracker-github")],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    names = [s.name for s in result.plugins[0].manifest.credentials[0].sources]
    declared = [s["name"] for s in MANIFEST["security"]["credentials"][0]["sources"]]
    assert names == sorted(declared) == ["GH_TOKEN", "GITHUB_TOKEN"]
    # bh's chain is GITHUB_TOKEN first; precedence therefore lives in credential.sources + root.


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["security"]["credentials"][0]["sources"][0].update(value="x"),
        lambda m: m["security"]["credentials"][0].update(token="x"),
        lambda m: m["security"]["credentials"][0]["sources"][0].update(kind="command"),
        lambda m: m["security"]["executables"][0].update(argv=["auth", "token"]),
    ],
    ids=["value-member", "token-member", "command-kind", "argv-member"],
)
def test_manifest_fails_closed_on_values_and_commands(mutate):
    bad = copy.deepcopy(MANIFEST)
    mutate(bad)
    result = discover_plugins(
        [_source(bad, "tracker-github")],
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
    )
    assert [d.code for d in result.errors] == [DiagnosticCode.INVALID_MANIFEST]
    assert result.owner_of(TRACKER_ADAPTER) is None


def test_declared_capability_equals_the_bound_capability():
    assert [c["id"] for c in MANIFEST["capabilities"]["provides"]] == [
        TRACKER_ADAPTER.capability_id
    ]
    assert MANIFEST["capabilities"]["provides"][0]["api_version"] == TRACKER_ADAPTER.api_version


# ---- 2. off by default; read and write separate; structural absence of writers --------------


def test_defaults_bind_nothing_and_every_feature_flag_is_off():
    composition = _compose({"tracker-github": copy.deepcopy(PROPOSED_DEFAULTS)})
    assert composition.state == "disabled" and composition.port is None
    with pytest.raises(CapabilityBindingError, match="no selected provider"):
        compose_proto.bind_application_port(TRACKER_ADAPTER_PORT, composition.discovery, ())
    flags = {p: v for p, v in flatten(PROPOSED_DEFAULTS).items() if p.endswith("enabled")}
    assert flags and not any(flags.values())


@pytest.mark.parametrize(
    "config", [{}, {"enabled": "true"}, {"enabled": 1}], ids=["absent", "str", "int"]
)
def test_only_an_exact_true_enables_the_plugin(config):
    """An absent key is OFF, and a non-boolean never counts as ON (config validation rejects it
    earlier; this is the second line of defence)."""
    composition = _compose({"tracker-github": config})
    assert composition.state == "disabled" and composition.port is None


def test_kernel_disablement_still_wins_over_the_plugin_flag():
    composition = _compose({"tracker-github": _on()}, kernel={"enabled": {"tracker-github": False}})
    assert composition.state == "disabled" and composition.port is None


def test_enabled_plugin_with_no_operation_binds_a_reader_with_no_writer_methods():
    composition = _compose({"tracker-github": _on()})
    assert composition.state == "ready" and composition.owner == "tracker-github"
    port = composition.port
    assert isinstance(port, TrackerAdapter)
    assert not isinstance(port, CommentWriter) and not isinstance(port, LabelWriter)
    assert all(not s.enabled for s in port.probe_capabilities().states.values())


@pytest.mark.parametrize(
    ("flags", "comment", "labels"),
    [
        ({}, False, False),
        ({"write__comment__enabled": True}, True, False),
        ({"write__labels__enabled": True}, False, True),
    ],
)
def test_each_write_flag_adds_exactly_its_own_writer(flags, comment, labels):
    # nested path helper: write__comment__enabled -> cfg["write"]["comment"]["enabled"]
    cfg = _on()
    for dotted, value in flags.items():
        a, b, c = dotted.split("__")
        cfg[a][b][c] = value
    port = _compose({"tracker-github": cfg}).port
    assert isinstance(port, CommentWriter) is comment
    assert isinstance(port, LabelWriter) is labels


def test_read_flags_enable_reads_without_enabling_writes():
    port = _compose({"tracker-github": _on(read__issues={"enabled": True})}).port
    states = port.probe_capabilities().states
    assert states[Operation.ISSUES_READ].enabled
    assert not states[Operation.COMMENTS_READ].enabled
    assert (
        not states[Operation.COMMENT_CREATE].enabled and not states[Operation.LABELS_CHANGE].enabled
    )


# ---- ownership conflicts reuse the kernel's fail-closed rule ---------------------------------


def test_two_enabled_providers_conflict_until_an_owner_is_selected():
    configs = {"tracker-github": _on(), "tracker-other": _on()}
    manifests = [MANIFEST, _other_provider_manifest()]
    ids = ("tracker-github", "tracker-other")
    conflict = _compose(configs, ids=ids, manifests=manifests)
    assert conflict.state == "conflict" and conflict.port is None
    assert [d.code for d in conflict.diagnostics] == [DiagnosticCode.DUPLICATE_CAPABILITY]
    chosen = _compose(
        configs,
        ids=ids,
        manifests=manifests,
        kernel={"capability_owners": {"tracker.adapter@1": "tracker-other"}},
    )
    assert chosen.state == "ready" and chosen.owner == "tracker-other"


def test_a_disabled_second_provider_causes_no_conflict():
    composition = _compose(
        {"tracker-github": _on(), "tracker-other": copy.deepcopy(PROPOSED_DEFAULTS)},
        ids=("tracker-github", "tracker-other"),
        manifests=[MANIFEST, _other_provider_manifest()],
    )
    assert composition.state == "ready" and composition.owner == "tracker-github"


# ---- 3. config: no secret-shaped or approval-shaped key; token chain -------------------------


def test_no_proposed_key_can_hold_a_secret_a_command_or_an_approval():
    for path in all_paths(PROPOSED_DEFAULTS):
        if path in ALLOWED_PATHS:
            continue
        for segment in path.split("."):
            assert not FORBIDDEN_KEY.search(segment), path


def test_token_source_follows_configured_names_and_treats_blank_as_unset():
    calls = []

    def gh():
        calls.append("gh")
        return "from-gh"

    env = {"GITHUB_TOKEN": "  ", "GH_TOKEN": "from-env"}
    order = ("GITHUB_TOKEN", "GH_TOKEN")
    first = build_token_source(["env", "gh"], environ=env, env_order=order, gh_auth_token=gh)()
    assert first.source == "env:GH_TOKEN" and calls == []
    second = build_token_source(["gh", "env"], environ=env, env_order=order, gh_auth_token=gh)()
    assert second.source == "gh-auth-token" and calls == ["gh"]
    assert build_token_source(["env"], environ={}, env_order=order, gh_auth_token=gh)() is None
    with pytest.raises(ValueError, match="unknown source"):
        build_token_source(["env", "keychain"], environ={}, env_order=order, gh_auth_token=gh)


# ---- 4. status and doctor: identity, capability, and no secret -------------------------------


def _doc(token_value: str, *, probe: bool, leaky_reason: bool = False, scrub_with=None):
    token = ResolvedToken(token_value, "env:GITHUB_TOKEN")
    cfg = _on(read__issues={"enabled": True})

    class Leaky(ReadOnlyStub):
        def probe_capabilities(self):
            report = super().probe_capabilities()
            if leaky_reason:
                for op, state in list(report.states.items()):
                    report.states[op] = type(state)(
                        state.operation, True, state.enabled, state.probe, f"echo {token_value}"
                    )
            return report

    composition = compose_proto.compose_tracker_adapter(
        {"tracker-github": cfg},
        sources=[_source(MANIFEST, "tracker-github")],
        providers=(
            TrackerAdapterProvider(
                "tracker-github", lambda s, ts: Leaky(s["repo"], ts, build_adapter(s, ts)._enabled)
            ),
        ),
        token_source=lambda: token,
    )
    declared = [(s["kind"], s["name"]) for s in MANIFEST["security"]["credentials"][0]["sources"]]
    environ = {"GITHUB_TOKEN": token_value}
    report = composition.port.probe_capabilities() if probe else None
    doc = build_status(
        composition,
        plugin_id="tracker-github",
        settings=cfg,
        credential=credential_facts(declared, environ, gh_on_path=False),
        report=report,
        secrets=(token_value,) if scrub_with is None else scrub_with,
        error=f"upstream said: bad credentials for {token_value}" if probe else None,
    )
    return token, doc


@pytest.mark.parametrize("value", [SHAPED, UNSHAPED], ids=["shaped", "unshaped"])
@pytest.mark.parametrize("probe", [False, True], ids=["status", "doctor-probe"])
def test_no_output_form_ever_contains_the_token(value, probe):
    token, doc = _doc(value, probe=probe, leaky_reason=True)
    outputs = [render_text(doc), render_json(doc), repr(token), str(token), f"{token}", repr(doc)]
    for out in outputs:
        assert value not in out
    if probe:
        assert "env:GITHUB_TOKEN" in render_text(doc) and REDACTED in render_text(doc)


def test_negative_control_the_leak_assertion_can_fail():
    """Without the exact-value scrub an unshaped secret DOES reach the output (so the test above
    is not vacuous); the shape pattern alone still catches a shaped one."""
    _, leaked = _doc(UNSHAPED, probe=True, leaky_reason=True, scrub_with=())
    assert UNSHAPED in render_text(leaked)
    _, caught = _doc(SHAPED, probe=True, leaky_reason=True, scrub_with=())
    assert SHAPED not in render_text(caught)


def test_scrub_replaces_longest_secret_first():
    assert scrub("token-long", ["tok", "token-long"]) == REDACTED


def test_status_text_is_stable_for_the_disabled_default():
    composition = _compose({"tracker-github": copy.deepcopy(PROPOSED_DEFAULTS)})
    declared = [(s["kind"], s["name"]) for s in MANIFEST["security"]["credentials"][0]["sources"]]
    doc = build_status(
        composition,
        plugin_id="tracker-github",
        settings=PROPOSED_DEFAULTS,
        credential=credential_facts(declared, {}, gh_on_path=False),
    )
    assert render_text(doc).splitlines() == [
        "tracker-github  disabled",
        "  capability   tracker.adapter@1  (owner: none selected)",
        "  repo         (unset)",
        "  credential   env:GH_TOKEN unset, env:GITHUB_TOKEN unset; gh not found",
        "  identity     not probed (bh plugin tracker-github doctor --probe)",
        "  operations   issues.read     disabled  probe=not-probed",
        "               comments.read   disabled  probe=not-probed",
        "               comment.create  disabled  probe=not-probed  "
        "[operator-approved single-use grant per use]",
        "               labels.change   disabled  probe=not-probed  "
        "[operator-approved single-use grant per use]",
    ]


# ---- recorded gaps in TODAY's config layer (these assert the gap, and flip when it is closed) --


def test_recorded_gap_typed_config_rejects_plugins_and_plugin_kernel():
    from beadhive.config_validate import validate_config

    problems = validate_config(
        {
            "schema_version": 1,
            "plugins": {"tracker-github": {"enabled": False}},
            "plugin_kernel": {},
        }
    )
    messages = [p["message"] for p in problems]
    assert any("unknown config key `plugins`" in m for m in messages)
    assert any("unknown config key `plugin_kernel`" in m for m in messages)


def test_recorded_gap_fragment_registry_is_closed_and_ownership_is_unclassified():
    from beadhive import config_partition

    composed = compose_plugin_config(
        {"plugins": {"tracker-github": {"enabled": True}}},
        [],
        available_plugin_ids=["tracker-github"],
        source_layer=SourceLayer.DEFAULT,
    )
    assert [(d.code, d.plugin_id) for d in composed.diagnostics] == [
        ("unknown_plugin_config", "tracker-github")
    ]
    assert "tracker-github" in composed.retained and "tracker-github" not in composed.active
    assert config_partition.partition_of("plugins.tracker-github.enabled") is None
    assert config_partition.partition_of("hitch.enabled") == "host"
