"""Config schema for the tracker-adapter binding (bh-w22e8, plugin ADR W2 and replan item (b)).

docs/design/tracker-adapter-plugin-adr.md sections 2-4: ``managed_repos[].stores.public.adapter``
(fleet-owned intent, rules R1-R5 at config load), ``plugins.tracker-github`` and
``plugin_kernel`` (host-owned authority, every switch off), and stores models that never echo an
input value. The regression corpus and the 48-combination store-axis matrix are ported from spike
bh-6bkun (``docs/spikes/artifacts/bh-6bkun/test_binding.py``) and now run against the REAL
models. Their expected results are ``tests/fixtures/config_store_binding_baseline.json``,
recorded on ``main`` at baee1df8 with the models as they were before this bead.

Offline and clock-free: pure model validation, no discovery, environment, network or plugin
import beyond the static built-in manifest resources.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, ValidationError
from ruamel.yaml import YAML

from beadhive import store_target
from beadhive.config_validate import validate_config
from beadhive.kernel.plugins import builtin_manifest_documents
from beadhive.modules.config.application import partition
from beadhive.modules.config.application.plugin_fragments import (
    _FRAGMENT_MODELS,
    PluginConfigManifest,
    TrustedManifestProvenance,
    builtin_plugin_fragments,
    compose_plugin_config,
)
from beadhive.modules.config.application.resolution import SourceLayer
from beadhive.modules.config.application.schema_artifacts import generate_config_json_schema
from beadhive.modules.config.contracts import (
    KNOWN_TRACKER_ADAPTERS,
    SCHEMA_VERSION,
    BeadhiveConfig,
    ManagedRepoEntry,
    ManagedRepoStores,
    PluginKernelSection,
    PluginsConfig,
    StoreDescriptor,
    TrackerGithubConfig,
    adapter_problems,
    field_default,
    iter_schema_fields,
    known_keys,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "tests/fixtures/config_store_binding_baseline.json").read_text())
#: A credential-shaped value (never a real one) planted where a paste could land.
TOKEN = "ghp_" + "A1b2C3d4E5" * 4
NEW_TOP_LEVEL_KEYS = {"plugins", "plugin_kernel"}


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch):
    # BeadhiveConfig is a pydantic-settings class (env_prefix BH_): keep the host env out of it.
    import os

    for name in [name for name in os.environ if name.startswith("BH_")]:
        monkeypatch.delenv(name)


# ---- the bh-6bkun corpus of EXISTING configs -------------------------------------------------


def _example() -> dict:
    return dict(YAML().load((ROOT / "src/beadhive/templates/config.example.yaml").read_text()))


def _fixture() -> dict:
    return dict(YAML().load((ROOT / "tests/fixture_config.yaml").read_text()))


def _docs_stores_block() -> list:
    """The ``managed_repos`` example in docs/CONFIGURATION.md (the one with github-issues)."""
    text = (ROOT / "docs/CONFIGURATION.md").read_text()
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
    return {
        "config.example.yaml": _example(),
        "tests/fixture_config.yaml": _fixture(),
        "legacy hive without stores": _doc(_hive()),
        "internal-only stores": _doc(_hive(internal={"remote": "widget-private"})),
        "public source github-issues": _doc(
            _hive(internal={"remote": "widget-private"}, public=PUBLIC)
        ),
        "docs/CONFIGURATION.md stores example": {
            "schema_version": SCHEMA_VERSION,
            "managed_repos": [dict(hive) for hive in _docs_stores_block()],
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
        "invalid: public sync push-pull": _doc(
            _hive(internal={"remote": "widget-private"}, public={**PUBLIC, "sync": "push-pull"})
        ),
        "invalid: internal source": _doc(_hive(internal={"source": "github-issues"})),
        "invalid: unknown key": _doc(_hive(), plugins_x={"a": 1}),
    }


CORPUS = _corpus()
_STORE_AXES = {
    "authority": [None, "local", "remote"],
    "sync": [None, "push-pull", "pull-only", "none"],
    "source": [None, "github-issues"],
    "dispatch": [None, True],
}


def _combinations() -> dict[str, dict]:
    out = {}
    for combo in itertools.product(*_STORE_AXES.values()):
        public = dict(PUBLIC, **{k: v for k, v in zip(_STORE_AXES, combo, strict=True) if v})
        out[json.dumps(list(combo))] = _doc(
            _hive(internal={"remote": "widget-private"}, public=public)
        )
    return out


COMBINATIONS = _combinations()


def _expected_problems(name: str) -> list[dict]:
    expected = copy.deepcopy(BASELINE["corpus"][name]["problems"])
    if name == "invalid: unknown key":
        # The one delta, and only in wording: `plugins` is now a real key, so the unknown
        # `plugins_x` gains a did-you-mean (same level, same order, same error).
        expected[0]["message"] += " — did you mean `plugins`?"
    return expected


def _observed(cfg: dict) -> dict:
    problems = validate_config(copy.deepcopy(cfg))
    try:
        dump = BeadhiveConfig.model_validate(copy.deepcopy(cfg)).model_dump(
            mode="json", exclude_none=True
        )
    except ValidationError:
        dump = None
    return {
        "problems": problems,
        "top_level_keys": sorted(dump) if dump is not None else None,
        "managed_repos": dump.get("managed_repos") if dump is not None else None,
    }


def test_the_corpus_and_matrix_match_the_recorded_pre_change_baseline():
    assert set(CORPUS) == set(BASELINE["corpus"]) and len(CORPUS) == 11
    assert set(COMBINATIONS) == set(BASELINE["combinations"]) and len(COMBINATIONS) == 48


@pytest.mark.parametrize("name", list(CORPUS))
def test_an_existing_config_validates_and_dumps_as_before(name):
    """Same problems (level, message, order) and the same dump: the new keys change nothing for
    a document that does not use them."""
    observed = _observed(CORPUS[name])
    recorded = BASELINE["corpus"][name]
    assert observed["problems"] == _expected_problems(name)
    assert observed["top_level_keys"] == recorded["top_level_keys"]
    assert observed["managed_repos"] == recorded["managed_repos"]
    if observed["top_level_keys"] is not None:
        assert not NEW_TOP_LEVEL_KEYS & set(observed["top_level_keys"])


def test_every_authority_sync_source_dispatch_combination_is_unchanged():
    """Exhaustive over the existing public-store axes (48 documents)."""
    for key, cfg in COMBINATIONS.items():
        observed = _observed(cfg)
        recorded = BASELINE["combinations"][key]
        assert observed == recorded, key
        for hive in observed["managed_repos"] or []:
            for store in (hive.get("stores") or {}).values():
                assert "adapter" not in store, key


def test_the_corpus_really_contains_todays_github_issues_source():
    sources = [
        (hive.get("stores") or {}).get("public", {}).get("source")
        for doc in CORPUS.values()
        for hive in doc.get("managed_repos", [])
        if isinstance(hive, dict)
    ]
    assert "github-issues" in sources


def test_the_only_new_store_field_is_adapter_and_it_defaults_to_unbound():
    assert StoreDescriptor().adapter is None
    assert "adapter" in StoreDescriptor.model_fields
    assert {"plugins", "plugin_kernel"} <= set(BeadhiveConfig.model_fields)
    config_default = BeadhiveConfig()
    assert config_default.plugins is None and config_default.plugin_kernel is None


# ---- T1 rules R1 to R5, exact messages (plugin ADR section 4, bh-6bkun section 2) -------------


def _problems(**stores_in) -> list[str]:
    public = stores_in.pop("public", None)
    stores = {"internal": stores_in.pop("internal", {"remote": "widget-private"})}
    if public is not None:
        stores["public"] = public
    return [problem["message"] for problem in validate_config(_doc(_hive(**stores)))]


def test_r1_unknown_adapter():
    assert _problems(public={**PUBLIC, "adapter": "tracker-gitlab"}) == [
        "`managed_repos.0.stores`: Value error, stores.public: unknown tracker adapter "
        "'tracker-gitlab' — known adapters: tracker-github"
    ]


@pytest.mark.parametrize("value", [TOKEN, "Tracker_GitHub", "", "a" * 65, "x y"])
def test_r2_a_malformed_adapter_value_is_rejected_without_being_echoed(value):
    problems = _problems(public={**PUBLIC, "adapter": value})
    assert problems == [
        "`managed_repos.0.stores.public.adapter`: Value error, adapter must be a plugin id "
        "(lowercase letters, digits and hyphens, at most 64 characters)"
    ]
    assert value == "" or value not in "".join(problems)


def test_r3_adapter_needs_a_source():
    public = {k: v for k, v in PUBLIC.items() if k != "source"}
    assert _problems(public={**public, "adapter": "tracker-github"}) == [
        "`managed_repos.0.stores`: Value error, stores.public: adapter 'tracker-github' needs "
        "`source: github-issues` — a bound store must name the tracker it mirrors"
    ]


def test_r4_adapter_only_on_a_public_store():
    assert _problems(internal={"remote": "widget-private", "adapter": "tracker-github"}) == [
        "`managed_repos.0.stores`: Value error, stores.internal: adapter is only valid on a "
        "public store"
    ]


def test_r5_adapter_must_serve_the_declared_source():
    """Unreachable through the schema while ``source`` has one member; the rule a second source
    activates, so it is exercised on the rule function the stores validator calls."""
    assert adapter_problems("public", "tracker-github", "jira-issues") == [
        "stores.public: adapter 'tracker-github' serves source 'github-issues', not 'jira-issues'"
    ]
    two = {**KNOWN_TRACKER_ADAPTERS, "tracker-jira": "jira-issues"}
    assert adapter_problems("public", "tracker-jira", "jira-issues", two) == []
    assert adapter_problems("public", None, None) == []


def test_r5_is_wired_into_the_stores_validator(monkeypatch):
    from beadhive.modules.config import contracts

    monkeypatch.setattr(
        contracts, "adapter_problems", lambda *a, **k: ["stores.public: sentinel from R5"]
    )
    with pytest.raises(ValidationError, match="sentinel from R5"):
        ManagedRepoStores.model_validate(
            {"internal": {"remote": "widget-private"}, "public": PUBLIC}
        )


def test_the_documented_binding_validates_and_enables_nothing():
    """A placeholder hive (acme/widget), never a real one: the binding is intent only."""
    bound = _hive(
        internal={"remote": "widget-private"}, public={**PUBLIC, "adapter": "tracker-github"}
    )
    assert validate_config(_doc(bound)) == []
    parsed = BeadhiveConfig.model_validate(_doc(bound))
    assert parsed.managed_repos[0].stores.public.adapter == "tracker-github"
    assert parsed.managed_repos[0].stores.internal.adapter is None
    assert parsed.plugins is None  # the binding never creates or enables the plugin config


# ---- the known-adapter table (and its drift against the built-in manifests, W6) -------------


def _builtin_manifests() -> dict[str, dict]:
    return {
        manifest["plugin_id"]: manifest
        for manifest in (json.loads(doc.payload) for doc in builtin_manifest_documents())
    }


def test_the_known_adapter_table_is_closed_and_consistent_with_the_config_layer():
    assert dict(KNOWN_TRACKER_ADAPTERS) == {"tracker-github": "github-issues"}
    with pytest.raises(TypeError):
        KNOWN_TRACKER_ADAPTERS["tracker-other"] = "x"  # type: ignore[index]
    source_members = set(StoreDescriptor.model_fields["source"].annotation.__args__[0].__args__)
    fragments = {fragment.plugin_id: fragment for fragment in builtin_plugin_fragments()}
    for adapter_id, source in KNOWN_TRACKER_ADAPTERS.items():
        assert source in source_members, adapter_id
        StoreDescriptor(adapter=adapter_id)  # passes its own R2 shape
        assert adapter_id in _FRAGMENT_MODELS and adapter_id in fragments
        assert fragments[adapter_id].namespace == f"plugins.{adapter_id}"
        assert partition.partition_of(f"plugins.{adapter_id}.enabled") == partition.HOST


def _known_adapter_drift(manifests: dict[str, dict], table, fragments: dict) -> list[str]:
    """Both directions between the closed known-adapter table and the built-in manifests:
    every manifest providing ``tracker.adapter`` is a row, and every row has a built-in manifest
    with its namespace and published fragment URN."""
    problems: list[str] = []
    for plugin_id, manifest in manifests.items():
        provided = {cap["id"] for cap in manifest["capabilities"]["provides"]}
        if "tracker.adapter" in provided and plugin_id not in table:
            problems.append(f"{plugin_id}: provides tracker.adapter but is not a table row")
    for adapter_id in table:
        manifest = manifests.get(adapter_id)
        if manifest is None:
            problems.append(f"{adapter_id}: a table row with no built-in manifest")
            continue
        if [cap["id"] for cap in manifest["capabilities"]["provides"]] != ["tracker.adapter"]:
            problems.append(f"{adapter_id}: does not provide exactly tracker.adapter")
        if manifest["configuration"]["namespace"] != f"plugins.{adapter_id}":
            problems.append(f"{adapter_id}: wrong namespace")
        fragment = fragments.get(adapter_id)
        if fragment is None or manifest["configuration"]["schema_artifact"] != fragment.artifact_id:
            problems.append(f"{adapter_id}: schema_artifact is not its published fragment")
    return problems


def test_the_known_adapter_table_matches_every_builtin_tracker_manifest():
    """The table and the built-in manifests agree both ways (W6 shipped the ``tracker-github``
    manifest, so a row without a manifest is now drift, not a pending skeleton)."""
    manifests = _builtin_manifests()
    fragments = {fragment.plugin_id: fragment for fragment in builtin_plugin_fragments()}
    assert "tracker-github" in manifests
    assert _known_adapter_drift(manifests, KNOWN_TRACKER_ADAPTERS, fragments) == []


def test_negative_control_known_adapter_drift_is_detected_in_both_directions():
    manifests = _builtin_manifests()
    fragments = {fragment.plugin_id: fragment for fragment in builtin_plugin_fragments()}
    table = dict(KNOWN_TRACKER_ADAPTERS)

    # a row with no manifest (the removed-manifest direction)
    without = {k: v for k, v in manifests.items() if k != "tracker-github"}
    assert any("no built-in manifest" in p for p in _known_adapter_drift(without, table, fragments))
    # a manifest providing tracker.adapter that the table does not know
    rogue = copy.deepcopy(manifests["tracker-github"])
    rogue["plugin_id"] = "tracker-rogue"
    rogue["configuration"]["namespace"] = "plugins.tracker-rogue"
    extra = {**manifests, "tracker-rogue": rogue}
    assert any("not a table row" in p for p in _known_adapter_drift(extra, table, fragments))
    # a drifted namespace or fragment
    moved = copy.deepcopy(manifests)
    moved["tracker-github"]["configuration"]["namespace"] = "plugins.github"
    assert any("wrong namespace" in p for p in _known_adapter_drift(moved, table, fragments))
    unpublished = copy.deepcopy(manifests)
    unpublished["tracker-github"]["configuration"]["schema_artifact"] = None
    assert any(
        "published fragment" in p for p in _known_adapter_drift(unpublished, table, fragments)
    )


# ---- no input echo (replan item (b), absorbs bh-gdx0c) ----------------------------------------


def _entry_with(**public) -> dict:
    return _hive(internal={"remote": "widget-private"}, public={**PUBLIC, **public})


@pytest.mark.parametrize("field", ["source", "adapter", "authority", "sync"])
def test_a_planted_token_never_reaches_the_configured_stores_error(field):
    with pytest.raises(store_target.StoreConfigInvalid) as caught:
        store_target.configured_stores(_entry_with(**{field: TOKEN}))
    message = str(caught.value)
    assert TOKEN not in message
    assert f"public.{field}" in message  # still names the field path and the rule


def test_negative_control_without_the_setting_the_planted_token_leaks(monkeypatch):
    """Proves the test above can detect the leak: the same call through a stores model with
    ``hide_input_in_errors`` removed prints the token. The nested ``StoreDescriptor`` still
    carries the setting, which is not enough: the VALIDATED model decides."""

    class EchoingStores(ManagedRepoStores):
        model_config = ConfigDict(extra="forbid", hide_input_in_errors=False)

    monkeypatch.setattr(store_target, "ManagedRepoStores", EchoingStores)
    with pytest.raises(store_target.StoreConfigInvalid) as caught:
        store_target.configured_stores(_entry_with(source=TOKEN))
    assert TOKEN in str(caught.value)


@pytest.mark.parametrize(
    "validate",
    [
        lambda raw: ManagedRepoEntry.model_validate(raw),
        lambda raw: BeadhiveConfig.model_validate({"managed_repos": [raw]}),
    ],
    ids=["managed-repo-entry", "beadhive-config"],
)
def test_the_other_validation_roots_of_the_stores_block_hide_input_too(validate):
    """``hq_hive_policy`` validates a ``ManagedRepoEntry``; the daemon/bootstrap call sites
    validate ``BeadhiveConfig`` and some format ``str(exc)``."""
    with pytest.raises(ValidationError) as caught:
        validate(_entry_with(source=TOKEN))
    assert TOKEN not in str(caught.value)


def test_every_stores_and_plugin_validation_root_declares_hide_input_in_errors():
    for model in (
        StoreDescriptor,
        ManagedRepoStores,
        ManagedRepoEntry,
        BeadhiveConfig,
        TrackerGithubConfig,
        PluginKernelSection,
        PluginsConfig,
    ):
        assert model.model_config.get("hide_input_in_errors") is True, model.__name__
        expected_extra = "allow" if model is PluginsConfig else "forbid"
        assert model.model_config.get("extra") == expected_extra, model.__name__


# ---- plugins.tracker-github: every switch off, strict, no secret-shaped key, no repo ---------

#: A key whose NAME suggests a secret, a command or a pre-approval can never exist (bh-rktrh).
FORBIDDEN_KEY = re.compile(
    r"(token|secret|passw|key|auth|header|command|argv|exec|grant|approv|yes|auto|bypass)",
    re.IGNORECASE,
)


def _leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, f"{prefix}{key}.")
    else:
        yield prefix.rstrip("."), value


def test_every_tracker_github_switch_defaults_off():
    defaults = dict(_leaves(TrackerGithubConfig().model_dump(mode="json")))
    switches = {path: value for path, value in defaults.items() if path.endswith("enabled")}
    assert switches == {
        "enabled": False,
        "read.issues.enabled": False,
        "read.comments.enabled": False,
        "http_cache.enabled": False,
        "write.comment.enabled": False,
        "write.labels.enabled": False,
    }
    assert defaults["base_url"] is None and defaults["write.labels.owned_prefixes"] == []
    assert defaults["credential.sources"] == ["env", "gh"]
    kernel = PluginKernelSection()
    assert kernel.allow_external_entry_points is False
    assert kernel.enabled == {} and kernel.capability_owners == {}


@pytest.mark.parametrize("path", ["enabled", "read.issues.enabled", "write.comment.enabled"])
@pytest.mark.parametrize("value", ["false", "true", 1, 0])
def test_tracker_github_booleans_are_strict(path, value):
    node: dict = {}
    cursor = node
    *parents, leaf = path.split(".")
    for part in parents:
        cursor = cursor.setdefault(part, {})
    cursor[leaf] = value
    problems = validate_config(_doc(plugins={"tracker-github": node}))
    assert [p["message"] for p in problems] == [
        f"`plugins.tracker-github.{path}`: Input should be a valid boolean"
    ]


def test_there_is_no_repo_key_on_the_plugin_namespace():
    problems = validate_config(_doc(plugins={"tracker-github": {"repo": "octo/widgets"}}))
    assert [p["level"] for p in problems] == ["error"]
    assert problems[0]["message"].startswith(
        "unknown config key `plugins.tracker-github.repo` — not part of schema v1."
    )


def test_no_new_key_can_hold_a_secret_a_command_or_an_approval():
    paths = [
        field.path
        for field in iter_schema_fields()
        if field.path.startswith(("plugins.tracker-github", "plugin_kernel"))
    ]
    assert "plugins.tracker-github.enabled" in paths and "plugin_kernel.enabled" in paths
    for path in [*paths, "managed_repos[].stores.public.adapter"]:
        for segment in path.split("."):
            if segment in {"credential", "sources"}:
                continue  # names of where to look, never a value (bh-rktrh ALLOWED_PATHS)
            assert not FORBIDDEN_KEY.search(segment), path


@pytest.mark.parametrize(
    "url",
    [
        f"https://{TOKEN}@ghe.example.com/api/v3",
        f"https://user:{TOKEN}@ghe.example.com/api/v3",
        f"http://ghe.example.com/api/v3?token={TOKEN}",
        f"https://ghe.example.com/api/v3?token={TOKEN}",
        f"https://ghe.example.com/api/v3#{TOKEN}",
        "ftp://ghe.example.com",
        "https://",
    ],
)
def test_base_url_must_be_plain_https_and_is_never_echoed(url):
    problems = validate_config(_doc(plugins={"tracker-github": {"base_url": url}}))
    assert [p["message"] for p in problems] == [
        "`plugins.tracker-github.base_url`: Value error, base_url must be an https URL "
        "without userinfo, query or fragment"
    ]
    with pytest.raises(ValidationError) as caught:
        TrackerGithubConfig.model_validate({"base_url": url})
    assert TOKEN not in str(caught.value)


def test_the_full_documented_plugin_config_validates():
    plugin = {
        "enabled": True,
        "base_url": "https://ghe.example.com/api/v3",
        "credential": {"sources": ["gh"]},
        "read": {
            "issues": {"enabled": True},
            "comments": {"enabled": False},
            "include_closed": False,
            "page_size": 50,
        },
        "http_cache": {"enabled": True},
        "retry": {"max_retries": 0, "max_single_wait_seconds": 1.5, "max_total_wait_seconds": 2},
        "write": {"comment": {"enabled": False}, "labels": {"owned_prefixes": ["bh:"]}},
    }
    cfg = _doc(
        plugins={"tracker-github": plugin},
        plugin_kernel={
            "enabled": {"tracker-github": True},
            "capability_owners": {"tracker.adapter@1": "tracker-github"},
        },
    )
    assert validate_config(copy.deepcopy(cfg)) == []
    parsed = BeadhiveConfig.model_validate(cfg)
    assert parsed.plugins.tracker_github.read.page_size == 50
    assert parsed.plugin_kernel.capability_owners == {"tracker.adapter@1": "tracker-github"}
    assert parsed.model_dump(by_alias=True, exclude_none=True)["plugins"]["tracker-github"][
        "enabled"
    ]


@pytest.mark.parametrize(
    ("plugin", "message"),
    [
        (
            {"credential": {"sources": ["env", "env"]}},
            "`plugins.tracker-github.credential.sources`: Value error, credential sources must "
            "be unique members of: env, gh",
        ),
        (
            {"credential": {"sources": ["command"]}},
            "`plugins.tracker-github.credential.sources.0`: Input should be 'env' or 'gh'",
        ),
        (
            {"read": {"page_size": 101}},
            "`plugins.tracker-github.read.page_size`: Input should be less than or equal to 100",
        ),
        (
            {"retry": {"max_single_wait_seconds": 200}},
            "`plugins.tracker-github.retry`: Value error, retry.max_single_wait_seconds must "
            "not exceed retry.max_total_wait_seconds",
        ),
        (
            {"write": {"labels": {"owned_prefixes": [""]}}},
            "`plugins.tracker-github.write.labels.owned_prefixes`: Value error, "
            "write.labels.owned_prefixes entries must be non-empty and trimmed",
        ),
        (
            {"write": {"labels": {"enabled": True}}},
            "`plugins.tracker-github.write.labels`: Value error, write.labels.enabled needs a "
            "non-empty write.labels.owned_prefixes (an empty list means no label write is "
            "possible)",
        ),
        (
            {"write": {"labels": {"enabled": True, "owned_prefixes": []}}},
            "`plugins.tracker-github.write.labels`: Value error, write.labels.enabled needs a "
            "non-empty write.labels.owned_prefixes (an empty list means no label write is "
            "possible)",
        ),
    ],
)
def test_tracker_github_values_fail_closed(plugin, message):
    problems = validate_config(_doc(plugins={"tracker-github": plugin}))
    assert [p["message"] for p in problems] == [message]


@pytest.mark.parametrize(
    "labels",
    [
        {},  # the default: off, no prefixes
        {"owned_prefixes": ["bh:"]},  # prefixes without the flag
        {"enabled": False},
        {"enabled": True, "owned_prefixes": ["bh:", "scope/"]},
    ],
)
def test_labels_change_is_accepted_when_off_or_when_it_owns_a_prefix(labels):
    problems = validate_config(_doc(plugins={"tracker-github": {"write": {"labels": labels}}}))
    assert problems == []


@pytest.mark.parametrize(
    ("kernel", "message"),
    [
        (
            {"enabled": {"Tracker_GitHub": True}},
            "`plugin_kernel.enabled`: Value error, plugin_kernel.enabled keys must be plugin ids",
        ),
        (
            {"enabled": {"tracker-github": "false"}},
            "`plugin_kernel.enabled.tracker-github`: Input should be a valid boolean",
        ),
        (
            {"capability_owners": {"tracker.adapter": "tracker-github"}},
            "`plugin_kernel.capability_owners`: Value error, plugin_kernel.capability_owners "
            "must map capability.id@MAJOR to a plugin id",
        ),
        (
            {"allow_external_entry_points": "yes"},
            "`plugin_kernel.allow_external_entry_points`: Input should be a valid boolean",
        ),
        (
            {"discover": True},
            "unknown config key `plugin_kernel.discover` — not part of schema v1.",
        ),
    ],
)
def test_plugin_kernel_mirrors_the_kernel_parser_shape(kernel, message):
    problems = [p["message"] for p in validate_config(_doc(plugin_kernel=kernel))]
    assert len(problems) == 1 and problems[0].startswith(message)


def test_unknown_plugin_namespaces_stay_opaque_and_builtin_ones_are_validated():
    assert validate_config(_doc(plugins={"future": {"anything": [1, 2]}})) == []
    parsed = BeadhiveConfig.model_validate(_doc(plugins={"future": {"anything": [1, 2]}}))
    assert parsed.plugins.model_extra == {"future": {"anything": [1, 2]}}
    problems = validate_config(_doc(plugins={"hitch": {"enabeld": True}}))
    assert problems[0]["message"].startswith("unknown config key `plugins.hitch.enabeld`")


def test_plugins_fields_cover_exactly_the_published_fragment_registry():
    keys = {field.alias or name for name, field in PluginsConfig.model_fields.items()}
    assert keys == set(_FRAGMENT_MODELS)
    for name, field in PluginsConfig.model_fields.items():
        assert field.default is None
        assert _FRAGMENT_MODELS[field.alias or name] in field.annotation.__args__


def test_schema_walkers_use_the_persisted_plugin_id_key():
    keys = set(known_keys())
    assert "plugins.tracker-github.read.issues.enabled" in keys
    assert not [key for key in keys if "tracker_github" in key]
    assert field_default("plugins.tracker-github.read.include_closed") is True
    assert field_default("plugins.tracker-github.enabled") is False
    assert "work.validate_overrides" in keys  # a populate-by-name alias keeps its field name


# ---- ownership (C2): binding is fleet intent, plugin and kernel are host authority ------------


def test_the_binding_is_fleet_owned_and_the_plugin_and_kernel_are_host_owned():
    assert partition.partition_of("managed_repos.0.stores.public.adapter") == partition.FLEET
    for path in (
        "plugins.tracker-github.enabled",
        "plugins.tracker-github.write.comment.enabled",
        "plugins.tracker-github.write.labels.owned_prefixes",
        "plugin_kernel.enabled.tracker-github",
        "plugin_kernel.capability_owners",
        "plugin_kernel.allow_external_entry_points",
        "plugins.hitch.enabled",
    ):
        assert partition.partition_of(path) == partition.HOST, path
    assert "plugins.tracker-github.write.comment.enabled" in partition.HOST_KEYS
    assert not [key for key in partition.FLEET_KEYS if key.startswith(("plugins", "plugin_"))]


# ---- the published fragment and the composed config schema (C3) ------------------------------


def test_the_tracker_github_fragment_is_published_and_inlined():
    fragment = next(f for f in builtin_plugin_fragments() if f.plugin_id == "tracker-github")
    assert fragment.namespace == "plugins.tracker-github"
    assert fragment.artifact_id == "urn:beadhive:wire-schema:plugin-config:tracker-github:1"
    assert "repo" not in fragment.schema["properties"]
    assert fragment.schema["additionalProperties"] is False
    schema = generate_config_json_schema()
    assert "plugin_kernel" in schema["properties"]
    validator = Draft202012Validator(schema)
    good = _doc(
        plugins={"tracker-github": {"enabled": True, "read": {"issues": {"enabled": True}}}}
    )
    assert not list(validator.iter_errors(good))
    for bad in (
        {"repo": "octo/widgets"},
        {"read": {"issues": {"enabled": "true"}}},
        {"write": {"comment": {"grant": "x"}}},
    ):
        assert list(validator.iter_errors(_doc(plugins={"tracker-github": bad}))), bad


def test_a_present_tracker_github_fragment_is_never_activated_without_its_manifest():
    """Presence is not activation: with no tracker-github manifest declared (today), the
    validated subtree is retained inactive."""
    result = compose_plugin_config(
        {"plugins": {"tracker-github": {"enabled": True}}},
        (),
        available_plugin_ids=("tracker-github",),
        source_layer=SourceLayer.HOST,
    )
    assert "tracker-github" not in result.active
    assert [(d.code, d.plugin_id) for d in result.diagnostics] == [
        ("unavailable_plugin", "tracker-github")
    ]
    manifest = PluginConfigManifest(
        "tracker-github",
        "plugins.tracker-github",
        "urn:beadhive:wire-schema:plugin-config:tracker-github:1",
        "1.0.0",
        TrustedManifestProvenance("built-in", "tracker-github.json"),
    )
    disabled = compose_plugin_config(
        {"plugins": {"tracker-github": {"enabled": "true"}}},
        (manifest,),
        available_plugin_ids=("tracker-github",),
        source_layer=SourceLayer.HOST,
    )
    assert "tracker-github" not in disabled.active  # a non-boolean never enables
    assert [(d.code, d.plugin_id) for d in disabled.diagnostics] == [
        ("invalid_config", "tracker-github")
    ]


# ---- nothing in the repository declares the keys (rollout item (a); bh-uf986 gates first use) -


def _yaml_documents():
    yield "config.example.yaml", _example()
    yield "tests/fixture_config.yaml", _fixture()
    for template in sorted((ROOT / "src/beadhive/templates").glob("*.yaml")):
        yield str(template.relative_to(ROOT)), YAML(typ="safe").load(template.read_text())
    for doc in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for index, block in enumerate(re.findall(r"```ya?ml\n(.*?)```", doc.read_text(), re.S)):
            try:
                parsed = YAML(typ="safe").load(block)
            except Exception:  # an illustrative fragment, not a config document
                continue
            yield f"{doc.relative_to(ROOT)}#{index}", parsed


def _declares_new_keys(node) -> bool:
    if isinstance(node, dict):
        if "adapter" in node and isinstance(node.get("adapter"), str):
            return True
        if {"plugins", "plugin_kernel"} & set(node):
            return True
        return any(_declares_new_keys(value) for value in node.values())
    if isinstance(node, list):
        return any(_declares_new_keys(value) for value in node)
    return False


def test_no_repository_config_fixture_or_operator_doc_example_declares_the_new_keys():
    offenders = [name for name, document in _yaml_documents() if _declares_new_keys(document)]
    assert offenders == []
    assert _declares_new_keys({"managed_repos": [{"stores": {"public": {"adapter": "x"}}}]})
