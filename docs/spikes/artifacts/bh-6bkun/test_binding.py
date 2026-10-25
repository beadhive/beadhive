"""Spike bh-6bkun: binding a bead store to the tracker-github adapter plugin.

Offline (conftest forbids sockets, sleeps and subprocesses). The config tests run the REAL
``validate_config`` and the REAL config models; the prototype only subclasses them to add the
one ``adapter`` key. The composition tests run the REAL kernel discovery and
``bind_application_port`` through the bh-rktrh prototypes.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from pathlib import Path

import pytest
from binding_proto import (
    KNOWN_ADAPTERS,
    BindingError,
    ProtoBeadhiveConfig,
    ProtoStoreDescriptor,
    TrackerGithubConfig,
    adapter_problems,
    binding_check,
    derive_repo,
    resolve_store_adapter,
    validate_with_proto,
)
from compose_proto import TrackerAdapterProvider
from config_proto import FORBIDDEN_KEY
from ruamel.yaml import YAML
from stub_adapters import build_adapter
from tracker_contracts import CommentWriter, Operation, TrackerAdapter

from beadhive import config
from beadhive.config_validate import validate_config
from beadhive.kernel.plugins import BuiltInManifestSource
from beadhive.modules.config.contracts import (
    SCHEMA_VERSION,
    BeadhiveConfig,
    StoreDescriptor,
)
from beadhive_plugins.contracts import ManifestDocument, ManifestProvenance

HERE = Path(__file__).parent
REPO = HERE.parents[3]
MANIFEST = json.loads((HERE / "plugin.json").read_text())
SHAPED = "ghp_" + "A1b2C3d4E5" * 4

# ---- corpus of EXISTING configs ---------------------------------------------------------------


def _example() -> dict:
    return dict(YAML().load(Path(config.template("config.example.yaml")).read_text()))


def _fixture() -> dict:
    return dict(YAML().load((REPO / "tests/fixture_config.yaml").read_text()))


def _docs_stores_block() -> list:
    """The ``managed_repos`` example in docs/CONFIGURATION.md (the one with github-issues)."""
    text = (REPO / "docs/CONFIGURATION.md").read_text()
    tail = text.split("### `managed_repos[*].stores`", 1)[1]
    block = re.search(r"```yaml\n(.*?)```", tail, re.S).group(1)
    return YAML().load(block)["managed_repos"]


def _hive(**stores) -> dict:
    entry = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}
    if stores:
        entry["stores"] = stores
    return entry


PUBLIC = {
    "prefix": "widpub",
    "path": "/stores/widget-public",
    "remote": "git-origin",
    "source": "github-issues",
}


def _doc(*hives, **top) -> dict:
    return {"schema_version": SCHEMA_VERSION, "managed_repos": list(hives), **top}


def _corpus() -> dict[str, dict]:
    docs_hives = [dict(h) for h in _docs_stores_block()]
    return {
        "config.example.yaml": _example(),
        "tests/fixture_config.yaml (no schema_version: stays an error)": _fixture(),
        "legacy hive without stores": _doc(_hive()),
        "internal-only stores": _doc(_hive(internal={"remote": "widget-private"})),
        "today: stores.public.source github-issues": _doc(
            _hive(internal={"remote": "widget-private"}, public=PUBLIC)
        ),
        "docs/CONFIGURATION.md stores example": {
            "schema_version": SCHEMA_VERSION,
            "managed_repos": docs_hives,
        },
        "public store without source": _doc(
            _hive(
                internal={"remote": "widget-private"},
                public={k: v for k, v in PUBLIC.items() if k != "source"},
            )
        ),
        "fork + host sections": _doc(
            {**_hive(), "kind": "fork", "upstream": "up/widget"},
            hitch={"enabled": False},
            orca={"enabled": False},
        ),
        "invalid today: public sync push-pull": _doc(
            _hive(internal={"remote": "widget-private"}, public={**PUBLIC, "sync": "push-pull"})
        ),
        "invalid today: internal source": _doc(_hive(internal={"source": "github-issues"})),
        "invalid today: unknown key": _doc(_hive(), plugins_x={"a": 1}),
    }


CORPUS = _corpus()


# ---- 1. an existing hive config validates unchanged -------------------------------------------


@pytest.mark.parametrize("name", list(CORPUS))
def test_existing_config_validates_identically_under_the_real_and_prototype_models(name):
    """Same problems, same messages, same order: the new key changes nothing for a document
    that does not use it."""
    cfg = CORPUS[name]
    assert validate_with_proto(copy.deepcopy(cfg)) == validate_config(copy.deepcopy(cfg))


@pytest.mark.parametrize(
    "name",
    [
        "config.example.yaml",
        "legacy hive without stores",
        "internal-only stores",
        "today: stores.public.source github-issues",
        "docs/CONFIGURATION.md stores example",
        "public store without source",
        "fork + host sections",
    ],
)
def test_the_valid_existing_configs_are_clean_and_dump_identically(name):
    cfg = CORPUS[name]
    assert validate_config(copy.deepcopy(cfg)) == []
    real = BeadhiveConfig.model_validate(copy.deepcopy(cfg)).model_dump(exclude_none=True)
    proto = ProtoBeadhiveConfig.model_validate(copy.deepcopy(cfg)).model_dump(exclude_none=True)
    assert proto == real


def test_the_corpus_really_contains_todays_github_issues_source():
    sources = [
        (h.get("stores") or {}).get("public", {}).get("source")
        for doc in CORPUS.values()
        for h in doc.get("managed_repos", [])
        if isinstance(h, dict)
    ]
    assert "github-issues" in sources
    assert KNOWN_ADAPTERS["tracker-github"] == "github-issues"


def test_the_prototype_adds_exactly_one_store_field():
    assert set(ProtoStoreDescriptor.model_fields) - set(StoreDescriptor.model_fields) == {"adapter"}
    assert ProtoStoreDescriptor().adapter is None  # absent = unbound = today


_STORE_AXES = {
    "authority": [None, "local", "remote"],
    "sync": [None, "push-pull", "pull-only", "none"],
    "source": [None, "github-issues"],
    "dispatch": [None, True],
}


def test_every_authority_sync_source_dispatch_combination_is_unchanged():
    """Exhaustive over the existing public-store axes (48 documents): where the real model
    accepts or rejects, the prototype does too, with the same message."""
    checked = 0
    for combo in itertools.product(*_STORE_AXES.values()):
        public = dict(
            PUBLIC, **{k: v for k, v in zip(_STORE_AXES, combo, strict=True) if v is not None}
        )
        cfg = _doc(_hive(internal={"remote": "widget-private"}, public=public))
        assert validate_with_proto(copy.deepcopy(cfg)) == validate_config(copy.deepcopy(cfg)), combo
        checked += 1
    assert checked == 3 * 4 * 2 * 2


def test_recorded_gap_real_models_reject_the_new_keys():
    """Today's schema is ``extra="forbid"``: the binding needs the schema bead. Expected to
    flip when it lands."""
    cfg = _doc(
        _hive(internal={"remote": "widget-private"}, public={**PUBLIC, "adapter": "tracker-github"})
    )
    cfg["plugins"] = {"tracker-github": {"enabled": True}}
    messages = [p["message"] for p in validate_config(cfg)]
    assert messages == [
        "unknown config key `managed_repos.0.stores.public.adapter` — not part of schema v1.",
        "unknown config key `plugins` — not part of schema v1.",
    ]


# ---- 2. the config shape ----------------------------------------------------------------------

BOUND = _hive(internal={"remote": "widget-private"}, public={**PUBLIC, "adapter": "tracker-github"})


def test_the_documented_binding_shape_validates_and_defaults_off():
    cfg = _doc(
        BOUND,
        plugins={"tracker-github": {"enabled": True, "read": {"issues": {"enabled": True}}}},
    )
    assert validate_with_proto(cfg) == []
    parsed = ProtoBeadhiveConfig.model_validate(cfg)
    assert parsed.managed_repos[0].stores.public.adapter == "tracker-github"
    assert parsed.managed_repos[0].stores.internal.adapter is None
    defaults = TrackerGithubConfig()
    assert defaults.enabled is False and not defaults.read.issues.enabled
    assert not defaults.write.comment.enabled and not defaults.write.labels.enabled


def test_a_binding_alone_enables_nothing():
    """The fleet-owned store key is intent only: with no plugin config at all the binding
    resolves to a disabled plugin, never to a port."""
    with pytest.raises(BindingError) as caught:
        _resolve(BOUND, plugin_configs={})
    assert caught.value.rule == "adapter-disabled"


def test_there_is_no_repo_key_on_the_plugin_namespace():
    problems = validate_with_proto(
        _doc(BOUND, plugins={"tracker-github": {"repo": "octo/widgets"}})
    )
    assert len(problems) == 1 and "repo" in problems[0]["message"]


def test_no_new_key_can_hold_a_secret_a_command_or_an_approval():
    new_names = {"adapter"}
    plugin_names = {name for name, _ in iter_paths(TrackerGithubConfig)}
    for name in new_names | plugin_names:
        if name in {"sources", "credential"}:
            continue  # names of where to look, never a value (rktrh ALLOWED_PATHS)
        assert not FORBIDDEN_KEY.search(name), name


def iter_paths(model, prefix=""):
    for name, field in model.model_fields.items():
        yield f"{prefix}{name}", field
        annotation = field.annotation
        if hasattr(annotation, "model_fields"):
            yield from iter_paths(annotation, f"{prefix}{name}.")


def test_ownership_of_the_binding_is_fleet_and_the_plugin_namespace_is_unclassified_today():
    from beadhive.modules.config.application.partition import FLEET, partition_of

    assert partition_of("managed_repos.0.stores.public.adapter") == FLEET
    assert partition_of("plugins.tracker-github.enabled") is None  # rktrh C2: must become host


# ---- 3. T1 rules (config load), exact messages -----------------------------------------------


def _problems(**public_or_internal) -> list[str]:
    store = public_or_internal.pop("public", None)
    internal = public_or_internal.pop("internal", {"remote": "widget-private"})
    stores = {"internal": internal}
    if store is not None:
        stores["public"] = store
    return [p["message"] for p in validate_with_proto(_doc(_hive(**stores)))]


def test_rule_unknown_adapter():
    assert _problems(public={**PUBLIC, "adapter": "tracker-gitlab"}) == [
        "`managed_repos.0.stores`: Value error, stores.public: unknown tracker adapter "
        "'tracker-gitlab' — known adapters: tracker-github"
    ]


def test_rule_adapter_needs_a_source():
    public = {k: v for k, v in PUBLIC.items() if k != "source"}
    assert _problems(public={**public, "adapter": "tracker-github"}) == [
        "`managed_repos.0.stores`: Value error, stores.public: adapter 'tracker-github' needs "
        "`source: github-issues` — a bound store must name the tracker it mirrors"
    ]


def test_rule_adapter_only_on_a_public_store():
    assert _problems(internal={"remote": "widget-private", "adapter": "tracker-github"}) == [
        "`managed_repos.0.stores`: Value error, stores.internal: adapter is only valid on a "
        "public store"
    ]


def test_rule_adapter_must_serve_the_declared_source():
    """Unreachable through the schema while ``source`` has one member; this is the rule a second
    source activates."""
    assert adapter_problems("public", "tracker-github", "jira-issues") == [
        "stores.public: adapter 'tracker-github' serves source 'github-issues', not 'jira-issues'"
    ]
    two = {**KNOWN_ADAPTERS, "tracker-jira": "jira-issues"}
    assert adapter_problems("public", "tracker-jira", "jira-issues", two) == []


@pytest.mark.parametrize("value", [SHAPED, "Tracker_GitHub", "", "a" * 65, "x y"])
def test_a_malformed_adapter_value_is_rejected_without_being_echoed(value):
    problems = _problems(public={**PUBLIC, "adapter": value})
    assert problems == [
        "`managed_repos.0.stores.public.adapter`: Value error, adapter must be a plugin id "
        "(lowercase letters, digits and hyphens, at most 64 characters)"
    ]
    assert value not in "".join(problems) or value == ""


def test_the_known_adapter_table_matches_the_builtin_manifest_shape():
    """Drift guard (rktrh P3 analogue): every row names a plugin id whose manifest provides
    tracker.adapter and whose namespace is plugins.<id>."""
    assert set(KNOWN_ADAPTERS) == {MANIFEST["plugin_id"]}
    assert MANIFEST["configuration"]["namespace"] == f"plugins.{MANIFEST['plugin_id']}"
    assert [c["id"] for c in MANIFEST["capabilities"]["provides"]] == ["tracker.adapter"]


# ---- 4. T3 rules (composition root), firing against the real kernel --------------------------


def _source(manifest: dict) -> BuiltInManifestSource:
    return BuiltInManifestSource(
        (
            ManifestDocument(
                ManifestProvenance("built-in", f"{manifest['plugin_id']}.json"),
                json.dumps(manifest).encode(),
            ),
        )
    )


def _plugin(**flags) -> dict:
    cfg: dict = {"enabled": True, "read": {"issues": {"enabled": True}}}
    for dotted, value in flags.items():
        node = cfg
        *parents, leaf = dotted.split("__")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return cfg


def _resolve(entry, *, plugin_configs, requested=frozenset(), manifests=None, kernel=None):
    sources = [_source(m) for m in (manifests if manifests is not None else [MANIFEST])]
    return resolve_store_adapter(
        entry,
        plugin_configs=plugin_configs,
        sources=sources,
        providers=[TrackerAdapterProvider("tracker-github", build_adapter)],
        token_source=lambda: None,
        requested=frozenset(requested),
        kernel=kernel,
    )


def _error(entry, **kwargs) -> BindingError:
    with pytest.raises(BindingError) as caught:
        _resolve(entry, **kwargs)
    return caught.value


def test_enabled_plugin_binds_a_reader_for_this_hives_repository():
    bound = _resolve(
        BOUND, plugin_configs={"tracker-github": _plugin()}, requested={Operation.ISSUES_READ}
    )
    assert bound.repo == "acme/widget" and bound.adapter_id == "tracker-github"
    assert isinstance(bound.port, TrackerAdapter) and not isinstance(bound.port, CommentWriter)
    assert bound.port.describe().project.locator == "acme/widget"
    assert bound.enabled == {Operation.ISSUES_READ}


def test_rule_store_without_a_binding_is_today():
    legacy = _hive()
    unbound = _hive(internal={"remote": "widget-private"}, public=PUBLIC)
    for entry in (legacy, unbound):
        assert _error(entry, plugin_configs={"tracker-github": _plugin()}).rule == "not-bound"
    assert _error(unbound, plugin_configs={"tracker-github": _plugin()}).message == (
        "github/acme/widget: stores.public binds no tracker adapter — declare "
        "`stores.public.adapter` to opt in"
    )


def test_rule_adapter_not_installed():
    error = _error(BOUND, plugin_configs={"tracker-github": _plugin()}, manifests=[])
    assert error.rule == "adapter-not-installed"
    assert error.message == (
        "github/acme/widget: stores.public binds tracker adapter 'tracker-github' but no "
        "discovered plugin has that id — install `beadhive[github]`"
    )


@pytest.mark.parametrize(
    "configs, kernel",
    [
        ({}, None),
        ({"tracker-github": {"enabled": False}}, None),
        ({"tracker-github": {"enabled": "true"}}, None),
        ({"tracker-github": _plugin()}, {"enabled": {"tracker-github": False}}),
    ],
    ids=["absent", "false", "non-boolean", "kernel-disabled"],
)
def test_rule_plugin_disabled(configs, kernel):
    error = _error(BOUND, plugin_configs=configs, kernel=kernel)
    assert error.rule == "adapter-disabled"
    assert error.message == (
        "github/acme/widget: stores.public binds tracker adapter 'tracker-github' but the "
        "plugin is disabled — set plugins.tracker-github.enabled: true on this host (a "
        "binding never enables a plugin)"
    )


def test_rule_write_requested_without_enablement():
    error = _error(
        BOUND,
        plugin_configs={"tracker-github": _plugin()},
        requested={Operation.COMMENT_CREATE},
    )
    assert error.rule == "operation-not-enabled"
    assert error.message == (
        "github/acme/widget: write comment.create was requested for stores.public but is not "
        "enabled — set plugins.tracker-github.write.comment.enabled: true (enabling a write is "
        "necessary, never sufficient: each write still needs an operator-approved single-use "
        "grant)"
    )


def test_rule_read_requested_without_enablement_has_no_grant_note():
    error = _error(
        BOUND,
        plugin_configs={"tracker-github": _plugin()},
        requested={Operation.COMMENTS_READ},
    )
    assert error.message == (
        "github/acme/widget: capability comments.read was requested for stores.public but is "
        "not enabled — set plugins.tracker-github.read.comments.enabled: true"
    )


def test_enabling_a_write_is_independent_of_reads_and_adds_only_its_writer():
    configs = {"tracker-github": _plugin(write__comment__enabled=True)}
    bound = _resolve(BOUND, plugin_configs=configs, requested={Operation.COMMENT_CREATE})
    assert isinstance(bound.port, CommentWriter)
    assert bound.enabled == {Operation.ISSUES_READ, Operation.COMMENT_CREATE}


def test_rule_identity_comes_from_the_hive_triplet_or_fails():
    gitlab = {**BOUND, "provider": "gitlab"}
    error = _error(gitlab, plugin_configs={"tracker-github": _plugin()})
    assert error.rule == "identity"
    assert error.message == (
        "gitlab/acme/widget: cannot derive a GitHub repository — the hive provider is "
        "'gitlab', not 'github'"
    )
    assert _error({**BOUND, "repo": ""}, plugin_configs={"tracker-github": _plugin()}).message == (
        "github/acme/: the hive identity names no owner/repository"
    )
    assert derive_repo(BOUND) == "acme/widget"


def test_rule_another_plugin_owning_the_capability():
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
    # tracker-github is installed but disabled; tracker-other is the only enabled provider.
    sources = [_source(MANIFEST), _source(other)]
    with pytest.raises(BindingError) as caught:
        resolve_store_adapter(
            BOUND,
            plugin_configs={
                "tracker-github": {"enabled": False},
                "tracker-other": {"enabled": True},
            },
            sources=sources,
            providers=[
                TrackerAdapterProvider("tracker-github", build_adapter),
                TrackerAdapterProvider("tracker-other", build_adapter),
            ],
            token_source=lambda: None,
        )
    assert caught.value.rule == "adapter-not-owner"
    assert "'tracker-other' is the selected owner of tracker.adapter@1" in caught.value.message


def test_two_enabled_providers_conflict_until_an_owner_is_selected():
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
    kwargs = dict(
        plugin_configs={"tracker-github": _plugin(), "tracker-other": {"enabled": True}},
        sources=[_source(MANIFEST), _source(other)],
        providers=[
            TrackerAdapterProvider("tracker-github", build_adapter),
            TrackerAdapterProvider("tracker-other", build_adapter),
        ],
        token_source=lambda: None,
    )
    with pytest.raises(BindingError) as caught:
        resolve_store_adapter(BOUND, **kwargs)
    assert caught.value.rule == "adapter-conflict"
    bound = resolve_store_adapter(
        BOUND, **kwargs, kernel={"capability_owners": {"tracker.adapter@1": "tracker-github"}}
    )
    assert bound.adapter_id == "tracker-github"


def test_each_hive_gets_its_own_repository_from_one_plugin_namespace():
    """One host-owned plugin config serves many hives; the repo is the store binding's."""
    other = {**BOUND, "org": "umbrella", "repo": "gadget", "prefix": "gad"}
    configs = {"tracker-github": _plugin()}
    assert _resolve(BOUND, plugin_configs=configs).port.describe().project.locator == "acme/widget"
    assert (
        _resolve(other, plugin_configs=configs).port.describe().project.locator == "umbrella/gadget"
    )


# ---- 5. T2 rows: hive ready / doctor ----------------------------------------------------------


def _check(entry, plugin_configs, **kwargs):
    return binding_check(
        entry,
        plugin_configs=plugin_configs,
        sources=[_source(MANIFEST)],
        providers=[TrackerAdapterProvider("tracker-github", build_adapter)],
        token_source=lambda: None,
        **kwargs,
    )


def test_hive_ready_says_nothing_for_an_unbound_hive():
    assert _check(_hive(), {}) is None
    assert (
        _check(_hive(internal={"remote": "x"}, public=PUBLIC), {"tracker-github": _plugin()})
        is None
    )


def test_hive_ready_never_fails_a_binding_whose_plugin_is_off():
    check = _check(BOUND, {})
    assert (check.state, check.severity) == ("adapter-disabled", "info")


def test_hive_ready_reports_a_ready_binding_with_operations_and_repo():
    check = _check(BOUND, {"tracker-github": _plugin()})
    assert (check.state, check.severity) == ("ready", "info")
    assert check.detail == (
        "github/acme/widget: tracker-github -> acme/widget; operations: issues.read"
    )


def test_hive_ready_makes_a_missing_package_an_error_only_once_the_plugin_is_enabled():
    sources = dict(
        sources=[],
        providers=[TrackerAdapterProvider("tracker-github", build_adapter)],
        token_source=lambda: None,
    )
    off = binding_check(BOUND, plugin_configs={}, **sources)
    on = binding_check(BOUND, plugin_configs={"tracker-github": _plugin()}, **sources)
    assert (off.state, off.severity) == ("adapter-not-installed", "info")
    assert (on.state, on.severity) == ("adapter-not-installed", "error")


def test_no_message_repeats_a_credential_shaped_value():
    """A token pasted where a name belongs (adapter, provider) never comes back in a message."""
    configs = {"tracker-github": _plugin()}
    assert (
        SHAPED
        not in _error(BOUND, plugin_configs=configs, requested={Operation.COMMENT_CREATE}).message
    )
    assert SHAPED not in "".join(_problems(public={**PUBLIC, "adapter": SHAPED}))


def test_recorded_finding_a_bad_store_value_is_echoed_by_the_real_stores_model():
    """``store_target.configured_stores`` formats ``str(ValidationError)`` (store_target.py:185).
    pydantic repeats the INPUT value, so a token pasted into ``source`` is echoed today. The
    prototype models hide the input, which is what the schema bead should do for the stores
    models; ``adapter`` makes the paste more likely."""
    from binding_proto import ProtoManagedRepoStores
    from pydantic import ValidationError

    from beadhive.modules.config.contracts import ManagedRepoStores

    bad = {"internal": {}, "public": {**PUBLIC, "source": SHAPED, "adapter": SHAPED}}
    with pytest.raises(ValidationError) as real:
        ManagedRepoStores.model_validate(bad)
    assert SHAPED in str(real.value)  # the finding (negative control)
    with pytest.raises(ValidationError) as proto:
        ProtoManagedRepoStores.model_validate(bad)
    assert SHAPED not in str(proto.value)


def test_recorded_finding_a_pre_change_host_cannot_resolve_any_store_of_a_bound_hive():
    """Rollout hazard: the real ``store_target`` validates the stores block with
    ``extra="forbid"``, so a host that receives ``adapter`` before it runs the new schema fails
    to resolve even the INTERNAL store of that hive (``resolve_store`` starts from
    ``configured_stores``, which is the pure step exercised here)."""
    from beadhive import store_target

    entry = {**BOUND, "stores": {**BOUND["stores"]}}
    with pytest.raises(store_target.StoreConfigInvalid, match="adapter"):
        store_target.configured_stores(entry)
