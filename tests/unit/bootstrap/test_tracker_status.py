"""The generic ``tracker.adapter`` status and doctor renderer.

Identity, credential presence and the capability tri-state are rendered from the neutral port and
the category composition, offline, through a TEST-ONLY manifest and ``MemoryTracker``. No output
form ever contains a credential: planted tokens are asserted absent, with a negative control that
disables the scrub and proves the assertion would catch a leak.
"""

from __future__ import annotations

import copy
import json
import logging
import socket
import subprocess

import pytest

from beadhive.bootstrap import tracker_status as status_module
from beadhive.bootstrap.tracker import compose_tracker_category
from beadhive.bootstrap.tracker_status import (
    STATUS_SCHEMA,
    doctor_exit_code,
    render_doctor_json,
    render_doctor_text,
    render_json,
    render_text,
    tracker_doctor,
    tracker_status,
)
from beadhive.kernel.plugins import (
    BuiltInManifestSource,
    DiagnosticCode,
    DiagnosticSeverity,
    ManifestDocument,
    ManifestProvenance,
)
from beadhive.modules.tracker.contracts import (
    Actor,
    ActorKey,
    ActorKind,
    CapabilityReport,
    CapabilityState,
    Operation,
    Tri,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld

SHAPED = "ghp_" + "A1b2C3d4E5" * 4  # caught by the token-shape pattern
UNSHAPED = "plain-secret-with-no-known-prefix"  # only the exact-value pass can catch it

MANIFEST = {
    "capabilities": {"provides": [{"api_version": 1, "id": "tracker.adapter"}]},
    "compatibility": {
        "beadhive": {"maximum_exclusive": "1.0.0", "minimum_inclusive": "0.15.0"},
        "plugin_kernel": {"maximum_exclusive": "2.0.0", "minimum_inclusive": "1.0.0"},
    },
    "configuration": {
        "legacy_namespaces": [],
        "namespace": "plugins.tracker-github",
        "schema_artifact": None,
    },
    "lifecycle": {"subscriptions": []},
    "manifest_version": 1,
    "plugin_id": "tracker-github",
    "plugin_version": "1.0.0",
    "presentation": {"cli": []},
    "security": {
        "credentials": [
            {
                "id": "github-api",
                "purpose": "authenticate tracker reads",
                "required": False,
                "sources": [
                    {"kind": "environment-variable", "name": "GH_TOKEN"},
                    {"kind": "environment-variable", "name": "GITHUB_TOKEN"},
                ],
            }
        ],
        "executables": [
            {
                "name": "gh",
                "required": False,
                "version": {"maximum_exclusive": "999.0.0", "minimum_inclusive": "0.0.0"},
            }
        ],
        "permissions": [],
    },
}


def source(*plugin_ids: str) -> BuiltInManifestSource:
    docs = []
    for pid in plugin_ids:
        doc = copy.deepcopy(MANIFEST)
        doc["plugin_id"] = pid
        doc["configuration"]["namespace"] = f"plugins.{pid}"
        docs.append(
            ManifestDocument(
                ManifestProvenance("test-only", f"{pid}.json"), json.dumps(doc).encode()
            )
        )
    return BuiltInManifestSource(tuple(docs))


BOUND = {
    "provider": "github",
    "org": "octo",
    "repo": "widgets",
    "stores": {"public": {"source": "github-issues", "adapter": "tracker-github"}},
}


def plugin(enabled=True, *, issues=True, comments=True, comment=False):
    return {
        "enabled": enabled,
        "read": {"issues": {"enabled": issues}, "comments": {"enabled": comments}},
        "write": {"comment": {"enabled": comment}},
    }


def status(
    plugins=None,
    *,
    kernel=None,
    sources=None,
    hives=(BOUND,),
    environ=None,
    gh=False,
    report=None,
    error=None,
):
    composition = compose_tracker_category(
        plugins={} if plugins is None else plugins,
        plugin_kernel=kernel,
        sources=[source("tracker-github")] if sources is None else sources,
    )
    return tracker_status(
        "tracker-github",
        composition=composition,
        hives=hives,
        environ={} if environ is None else environ,
        which=lambda name: "/usr/bin/gh" if gh and name == "gh" else None,
        report=report,
        error=error,
    )


def codes(rows) -> list[tuple[str, str]]:
    return [(row.code.value, row.severity.value) for row in rows]


# ---- rendering --------------------------------------------------------------------------


def test_the_disabled_default_renders_these_exact_lines():
    doc = status()
    assert render_text(doc).splitlines() == [
        "tracker-github  disabled",
        "  capability   tracker.adapter@1  (owner: none selected)",
        "  stores       1 bound",
        "               github/octo/widgets [public] -> octo/widgets",
        "  credential   env:GH_TOKEN unset, env:GITHUB_TOKEN unset; gh not found",
        "  identity     not probed (bh plugin tracker-github doctor --probe)",
        "  operations   issues.read     disabled  probe=not-probed",
        "               comments.read   disabled  probe=not-probed",
        "               comment.create  disabled  probe=not-probed  "
        "[operator-approved single-use grant per use]",
        "               labels.change   disabled  probe=not-probed  "
        "[operator-approved single-use grant per use]",
    ]
    rows = tracker_doctor(doc)
    assert codes(rows) == [("disabled-plugin", "info")]
    assert doctor_exit_code(doc, rows) == 0


def test_an_enabled_probed_store_renders_identity_credential_and_the_tri_state():
    world = MemoryWorld(locator="octo/widgets")
    tracker = MemoryTracker(world, enabled=frozenset({Operation.ISSUES_READ}))
    probed = tracker.probe_capabilities()
    report = CapabilityReport(
        probed.actor,
        "env:GITHUB_TOKEN",
        {
            **probed.states,
            Operation.COMMENTS_READ: CapabilityState(
                Operation.COMMENTS_READ, True, True, Tri.UNKNOWN, "token grants are not readable"
            ),
            Operation.LABELS_CHANGE: CapabilityState(
                Operation.LABELS_CHANGE, True, False, Tri.NO, "no triage role"
            ),
        },
    )
    doc = status(
        {"tracker-github": plugin()}, environ={"GITHUB_TOKEN": UNSHAPED}, gh=True, report=report
    )
    assert render_text(doc).splitlines() == [
        "tracker-github  ready",
        "  capability   tracker.adapter@1  (owner: tracker-github)",
        "  stores       1 bound",
        "               github/octo/widgets [public] -> octo/widgets",
        "  credential   env:GH_TOKEN unset, env:GITHUB_TOKEN set; gh found",
        "               resolved via env:GITHUB_TOKEN",
        "  identity     id=human-maintainer kind=human display=maintainer",
        "  operations   issues.read     enabled   probe=yes        usable",
        "               comments.read   enabled   probe=unknown    usable",
        "               comment.create  disabled  probe=yes        not usable  "
        "[operator-approved single-use grant per use]",
        "               labels.change   disabled  probe=no         not usable  "
        "[operator-approved single-use grant per use]",
    ]
    parsed = json.loads(render_json(doc))
    assert parsed["schema"] == STATUS_SCHEMA
    assert parsed["identity"] == {
        "id": "human-maintainer",
        "kind": "human",
        "display": "maintainer",
    }
    assert [op["probe"] for op in parsed["operations"]] == ["yes", "unknown", "yes", "no"]
    assert parsed["credential"]["legs"] == [
        {"source": "env:GH_TOKEN", "present": False},
        {"source": "env:GITHUB_TOKEN", "present": True},
    ]
    assert tracker_doctor(doc) == ()


def test_an_anonymous_probe_renders_as_anonymous():
    report = CapabilityReport(None, None, {})
    doc = status({"tracker-github": plugin()}, report=report)
    assert "  identity     anonymous (no credential)" in render_text(doc).splitlines()


def test_states_cover_unconfigured_not_installed_conflict_and_invalid_config():
    assert status({"tracker-github": plugin()}, hives=())["state"] == "unconfigured"
    assert status(sources=[])["state"] == "not-installed"
    assert (
        status(
            {"tracker-github": plugin(), "tracker-other": plugin()},
            sources=[source("tracker-github", "tracker-other")],
        )["state"]
        == "conflict"
    )
    assert status({"tracker-github": {"enabled": True, "repo": "x/y"}})["state"] == "invalid-config"
    assert (
        status(kernel={"enabled": {"tracker-github": False}}, plugins={"tracker-github": plugin()})[
            "state"
        ]
        == "disabled"
    )


# ---- doctor -----------------------------------------------------------------------------


def test_a_disabled_or_uninstalled_plugin_never_fails_doctor():
    for doc in (status(), status(sources=[])):
        rows = tracker_doctor(doc)
        assert all(row.severity is not DiagnosticSeverity.ERROR for row in rows)
        assert doctor_exit_code(doc, rows) == 0
    assert codes(tracker_doctor(status(sources=[]))) == [("tracker-package", "info")]


def test_an_enabled_but_uninstalled_plugin_is_an_error_naming_the_extra():
    doc = status({"tracker-github": plugin()}, sources=[])
    rows = tracker_doctor(doc)
    assert codes(rows) == [("tracker-package", "error")]
    assert rows[0].detail.endswith("install `beadhive[github]`")
    assert doctor_exit_code(doc, rows) == 1


def test_doctor_codes_for_config_conflict_and_unconfigured():
    invalid = status({"tracker-github": {"enabled": True, "repo": "x/y"}})
    assert codes(tracker_doctor(invalid)) == [("tracker-config", "error")]
    conflict = status(
        {"tracker-github": plugin(), "tracker-other": plugin()},
        sources=[source("tracker-github", "tracker-other")],
    )
    assert codes(tracker_doctor(conflict)) == [("duplicate-capability", "error")]
    unconfigured = status({"tracker-github": plugin()}, hives=(), gh=True)
    assert codes(tracker_doctor(unconfigured)) == [("tracker-config", "warning")]
    assert doctor_exit_code(unconfigured, tracker_doctor(unconfigured)) == 0


def test_doctor_credential_rows():
    anonymous_reads = status({"tracker-github": plugin()})
    assert codes(tracker_doctor(anonymous_reads)) == [("tracker-credential", "warning")]
    anonymous_write = status({"tracker-github": plugin(comment=True)})
    assert codes(tracker_doctor(anonymous_write)) == [
        ("tracker-credential", "error"),
        ("tracker-capability", "info"),
    ]
    assert "operator-approved" in tracker_doctor(anonymous_write)[1].detail


def test_both_token_variables_set_and_different_is_a_state_only_advisory():
    differ = status(
        {"tracker-github": plugin()}, environ={"GH_TOKEN": SHAPED, "GITHUB_TOKEN": UNSHAPED}
    )
    assert differ["credential"]["disagree"] is True
    text = render_text(differ)
    assert "warning: GH_TOKEN and GITHUB_TOKEN are both set and differ" in text
    assert codes(tracker_doctor(differ)) == [("tracker-credential", "warning")]
    same = status(
        {"tracker-github": plugin()}, environ={"GH_TOKEN": SHAPED, "GITHUB_TOKEN": SHAPED}
    )
    assert same["credential"]["disagree"] is False
    assert tracker_doctor(same) == ()


def test_an_enabled_operation_that_probes_no_is_a_capability_warning():
    report = CapabilityReport(
        None,
        None,
        {Operation.ISSUES_READ: CapabilityState(Operation.ISSUES_READ, True, True, Tri.NO, "404")},
    )
    doc = status({"tracker-github": plugin(comments=False)}, gh=True, report=report)
    rows = tracker_doctor(doc)
    assert codes(rows) == [("tracker-capability", "warning")]
    assert rows[0].code is DiagnosticCode.TRACKER_CAPABILITY


# ---- secrets and effects ----------------------------------------------------------------


def _leaky_inputs() -> dict:
    report = CapabilityReport(
        Actor(ActorKey("583231", ActorKind.HUMAN), display=f"octocat {UNSHAPED}"),
        f"env:GITHUB_TOKEN {UNSHAPED}",
        {
            Operation.ISSUES_READ: CapabilityState(
                Operation.ISSUES_READ, True, True, Tri.NO, f"upstream said Bearer {SHAPED}"
            ),
            Operation.COMMENT_CREATE: CapabilityState(
                Operation.COMMENT_CREATE, True, True, Tri.UNKNOWN, f"token {UNSHAPED} unreadable"
            ),
        },
    )
    return {
        "plugins": {"tracker-github": plugin(comment=True)},
        "environ": {"GH_TOKEN": SHAPED, "GITHUB_TOKEN": UNSHAPED},
        "report": report,
        "error": f"401 bad credentials for {SHAPED} / {UNSHAPED}",
    }


def _every_output(doc) -> str:
    rows = tracker_doctor(doc)
    return "\n".join(
        [
            render_text(doc),
            render_json(doc),
            render_doctor_text(doc, rows),
            render_doctor_json(doc, rows),
            *(row.render() for row in rows),
            repr(doc),
        ]
    )


def test_no_output_form_ever_contains_a_planted_credential(caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    inputs = _leaky_inputs()
    doc = status(
        inputs["plugins"], environ=inputs["environ"], report=inputs["report"], error=inputs["error"]
    )
    out = _every_output(doc)
    (tmp_path / "status.json").write_text(render_json(doc))
    written = (tmp_path / "status.json").read_text()
    for secret in (SHAPED, UNSHAPED):
        assert secret not in out
        assert secret not in written
        assert secret not in caplog.text
    assert "***" in out  # the scrub ran, so the assertion above is not vacuous by omission


def test_negative_control_without_the_scrub_the_leak_is_detected(monkeypatch):
    monkeypatch.setattr(status_module, "scrub", lambda text, secrets=(): text)
    inputs = _leaky_inputs()
    doc = status(
        inputs["plugins"], environ=inputs["environ"], report=inputs["report"], error=inputs["error"]
    )
    out = _every_output(doc)
    assert SHAPED in out and UNSHAPED in out


def test_status_and_doctor_make_no_network_call_and_run_no_process(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("status must stay offline")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    doc = status({"tracker-github": plugin()}, environ={"GH_TOKEN": SHAPED})
    render_doctor_text(doc, tracker_doctor(doc))


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_variable_counts_as_unset(value):
    doc = status(environ={"GH_TOKEN": value})
    assert doc["credential"]["legs"][0] == {"source": "env:GH_TOKEN", "present": False}


# ---- the per-store listing ---------------------------------------------------------------


def test_status_lists_every_bound_store_with_its_derived_project_and_skips_unbound_ones():
    other = {**BOUND, "org": "acme", "repo": "api"}
    unbound = {"provider": "github", "org": "o", "repo": "r", "stores": {"public": {}}}
    doc = status({"tracker-github": plugin()}, hives=(BOUND, unbound, other))
    assert doc["bound_stores"] == 2
    assert doc["stores"] == [
        {
            "hive": "github/octo/widgets",
            "scope": "public",
            "project": "octo/widgets",
            "reason": None,
        },
        {"hive": "github/acme/api", "scope": "public", "project": "acme/api", "reason": None},
    ]
    lines = render_text(doc).splitlines()
    assert "               github/octo/widgets [public] -> octo/widgets" in lines
    assert "               github/acme/api [public] -> acme/api" in lines
    assert json.loads(render_json(doc))["stores"] == doc["stores"]


def test_status_reports_an_unresolvable_project_with_a_reason_instead_of_a_slug():
    gitlab = {**BOUND, "provider": "gitlab"}
    doc = status({"tracker-github": plugin()}, hives=(gitlab,))
    (store,) = doc["stores"]
    assert store["project"] is None and "not 'github'" in store["reason"]
    assert "no project (" in render_text(doc)


def test_an_unbound_hive_set_lists_nothing():
    doc = status({"tracker-github": plugin()}, hives=())
    assert doc["stores"] == [] and doc["bound_stores"] == 0
