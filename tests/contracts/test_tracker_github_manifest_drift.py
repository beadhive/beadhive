"""P3: the packaged ``tracker-github`` manifest cannot drift from what root relies on.

``packages/beadhive-tracker-github`` ships ``plugin.json`` and root ships a byte copy under
``kernel/plugins/manifests/`` (package manifests are copied, never generated). Three facts must
stay in step, and each check below has a negative control proving it can fail:

* the packaged manifest and the built-in copy are byte-identical;
* the manifest provides exactly the ``tracker.adapter`` capability root composes
  (``bootstrap.tracker.TRACKER_ADAPTER``);
* the credential and executable declarations match what the token-source builder
  (``run.github_token_source``) and the config layer's ``credential.sources`` names accept.

The known-adapter table direction lives in ``tests/test_config_store_binding.py``.
"""

from __future__ import annotations

import copy
import json
from importlib import resources

import pytest

from beadhive import run
from beadhive.bootstrap.tracker import TRACKER_ADAPTER
from beadhive.kernel.plugins import builtin_manifest_documents
from beadhive.modules.config.contracts import TRACKER_GITHUB_CREDENTIAL_SOURCES

PLUGIN_ID = "tracker-github"

#: Environment variable names a careless token builder or manifest could plausibly add; the
#: behavioural probe below asks the real builder about each, so a drift in EITHER direction
#: (a variable the manifest omits, or declares but the builder ignores) is detected.
_PROBED_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_PAT",
    "TOKEN",
)


def _packaged_bytes() -> bytes:
    return (resources.files("beadhive_tracker_github") / "plugin.json").read_bytes()


def _builtin_bytes() -> bytes:
    document = next(
        doc
        for doc in builtin_manifest_documents()
        if doc.provenance.source_name == f"{PLUGIN_ID}.json"
    )
    return document.payload


def _copy_drift(packaged: bytes, builtin: bytes) -> list[str]:
    """Why the two copies differ (empty when byte-identical)."""
    if packaged == builtin:
        return []
    try:
        same_json = json.loads(packaged) == json.loads(builtin)
    except ValueError:
        same_json = False
    return ["same JSON, different bytes" if same_json else "different content"]


def _capability_drift(manifest: dict) -> list[str]:
    provides = [(c["id"], c["api_version"]) for c in manifest["capabilities"]["provides"]]
    expected = [(TRACKER_ADAPTER.capability_id, TRACKER_ADAPTER.api_version)]
    return [] if provides == expected else [f"provides {provides}, root composes {expected}"]


def _accepted_variables() -> set[str]:
    """The environment variables the real token-source builder reads, found by asking it."""
    accepted = set()
    for variable in _PROBED_VARIABLES:
        source = run.github_token_source({variable: "probe"}, names=("env",))
        if source() is not None:
            accepted.add(variable)
    return accepted


def _credential_drift(manifest: dict) -> list[str]:
    """Disagreements between the manifest and the credential chain the adapter receives."""
    problems: list[str] = []
    credentials = manifest["security"]["credentials"]
    sources = [source for credential in credentials for source in credential["sources"]]
    # A command such as `gh auth token` cannot be a credential source in the wire schema; every
    # declared source here is an environment variable.
    if {source["kind"] for source in sources} - {"environment-variable"}:
        problems.append("a credential source is not an environment variable")
    declared = {source["name"] for source in sources}
    accepted = _accepted_variables()
    if declared != accepted:
        problems.append(f"manifest declares {sorted(declared)}, builder reads {sorted(accepted)}")
    if any(credential["required"] for credential in credentials):
        problems.append("a credential is required (anonymous reads must stay possible)")
    # `credential.sources` names: each must be backed by something the manifest declares.
    executables = {item["name"]: item["required"] for item in manifest["security"]["executables"]}
    for name in TRACKER_GITHUB_CREDENTIAL_SOURCES:
        if name == "env" and not declared:
            problems.append("credential.sources 'env' is backed by no declared variable")
        if name == "gh" and executables.get("gh") is not False:
            problems.append("credential.sources 'gh' is not backed by an optional `gh` executable")
    if set(TRACKER_GITHUB_CREDENTIAL_SOURCES) != set(run.GITHUB_TOKEN_SOURCE_NAMES):
        problems.append("config credential.sources names differ from the token-source builder's")
    return problems


def _manifest() -> dict:
    return json.loads(_builtin_bytes())


# ---- the packaged manifest and the built-in copy -----------------------------------------------


def test_the_packaged_manifest_is_byte_identical_to_the_builtin_copy():
    assert _copy_drift(_packaged_bytes(), _builtin_bytes()) == []


def test_negative_control_a_drifted_copy_is_detected():
    packaged = _packaged_bytes()
    reformatted = json.dumps(json.loads(packaged), indent=2).encode()
    changed = packaged.replace(b'"plugin_version": "1.0.0"', b'"plugin_version": "1.0.1"')

    assert changed != packaged  # the mutation applied, so the control is not vacuous
    assert _copy_drift(packaged, reformatted) == ["same JSON, different bytes"]
    assert _copy_drift(packaged, changed) == ["different content"]
    assert _copy_drift(packaged, b"{") == ["different content"]


# ---- the capability root composes ----------------------------------------------------------------


def test_the_manifest_provides_exactly_the_capability_root_composes():
    assert TRACKER_ADAPTER.render() == "tracker.adapter@1"
    assert _capability_drift(_manifest()) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["capabilities"]["provides"].clear(),
        lambda m: m["capabilities"]["provides"][0].update(api_version=2),
        lambda m: m["capabilities"]["provides"][0].update(id="tracker.writer"),
        lambda m: m["capabilities"]["provides"].append({"api_version": 1, "id": "build.impact"}),
    ],
)
def test_negative_control_capability_drift_is_detected(mutate):
    manifest = copy.deepcopy(_manifest())
    mutate(manifest)
    assert _capability_drift(manifest) != []


# ---- the credential chain ------------------------------------------------------------------------


def test_the_credential_declarations_match_what_the_token_source_builder_accepts():
    assert _accepted_variables() == {"GH_TOKEN", "GITHUB_TOKEN"}
    assert _credential_drift(_manifest()) == []


def test_the_gh_leg_is_an_optional_executable_and_never_a_credential_source():
    manifest = _manifest()
    assert [(e["name"], e["required"]) for e in manifest["security"]["executables"]] == [
        ("gh", False)
    ]
    assert run.GITHUB_TOKEN_SOURCE_NAMES == ("env", "gh") == TRACKER_GITHUB_CREDENTIAL_SOURCES


@pytest.mark.parametrize(
    ("label", "mutate", "expected"),
    [
        (
            "a variable the manifest forgot",
            lambda m: m["security"]["credentials"][0]["sources"].pop(),
            "builder reads",
        ),
        (
            "a variable the builder does not read",
            lambda m: m["security"]["credentials"][0]["sources"].append(
                {"kind": "environment-variable", "name": "GH_ENTERPRISE_TOKEN"}
            ),
            "builder reads",
        ),
        (
            "a command declared as a credential source",
            lambda m: m["security"]["credentials"][0]["sources"].append(
                {"kind": "command", "name": "gh auth token"}
            ),
            "not an environment variable",
        ),
        (
            "a required credential",
            lambda m: m["security"]["credentials"][0].update(required=True),
            "anonymous reads",
        ),
        (
            "the optional gh executable dropped",
            lambda m: m["security"]["executables"].clear(),
            "not backed by an optional `gh` executable",
        ),
        (
            "the gh executable made required",
            lambda m: m["security"]["executables"][0].update(required=True),
            "not backed by an optional `gh` executable",
        ),
    ],
)
def test_negative_control_credential_drift_is_detected(label, mutate, expected):
    manifest = copy.deepcopy(_manifest())
    mutate(manifest)
    problems = _credential_drift(manifest)
    assert any(expected in problem for problem in problems), (label, problems)
