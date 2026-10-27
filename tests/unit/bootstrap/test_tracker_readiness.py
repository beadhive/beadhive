"""The offline T2 readiness rows of the ``tracker.adapter`` category (one per bound store).

Rows are informational: no state maps to a failing ``hive ready`` result, an unbound hive costs
nothing, an off plugin never nags, and no row carries a credential. Everything runs through a
TEST-ONLY manifest source; no package is imported and nothing touches the network.
"""

from __future__ import annotations

import copy
import json
import socket
import subprocess

import pytest

from beadhive.bootstrap import tracker as tracker_module
from beadhive.bootstrap.tracker_readiness import has_binding, tracker_store_rows
from beadhive.kernel.plugins import BuiltInManifestSource, ManifestDocument, ManifestProvenance

SHAPED = "ghp_" + "A1b2C3d4E5" * 4

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
    "security": {"credentials": [], "executables": [], "permissions": []},
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


def hive(
    org="octo", repo="widgets", *, provider="github", adapter="tracker-github", scope="public"
):
    stores: dict = {"public": {"source": "github-issues"}}
    if adapter is not None:
        stores.setdefault(scope, {})["adapter"] = adapter
    return {"provider": provider, "org": org, "repo": repo, "stores": stores}


def on(*, comment=False):
    return {
        "enabled": True,
        "read": {"issues": {"enabled": True}, "comments": {"enabled": True}},
        "write": {"comment": {"enabled": comment}},
    }


def rows(hives, plugins=None, kernel=None, sources=None):
    return tracker_store_rows(
        hives,
        plugins=plugins,
        plugin_kernel=kernel,
        sources=[source("tracker-github")] if sources is None else sources,
    )


def test_a_hive_with_no_binding_yields_nothing_and_never_builds_the_composition(monkeypatch):
    def forbidden(**_kwargs):
        raise AssertionError("the composition must not be built for an unbound hive")

    monkeypatch.setattr(tracker_module, "compose_tracker_category", forbidden)
    unbound = [hive(adapter=None), {"provider": "github", "org": "o", "repo": "r"}]
    assert has_binding(unbound) is False
    assert tracker_store_rows(unbound, plugins=None, plugin_kernel=None) == ()


def test_a_disabled_plugin_is_one_informational_row():
    (row,) = rows([hive()])
    assert (row.state, row.severity, row.rule, row.code) == (
        "disabled",
        "info",
        "R6",
        "disabled-plugin",
    )
    assert row.check_state() == "na"
    assert row.hive == "github/octo/widgets" and row.scope == "public"
    assert "a binding never enables a plugin" in row.detail


def test_a_kernel_policy_off_wins_over_the_plugins_own_flag():
    (row,) = rows([hive()], {"tracker-github": on()}, {"enabled": {"tracker-github": False}})
    assert (row.state, row.severity) == ("disabled", "info")


def test_a_plugin_that_is_off_and_not_installed_is_information_naming_the_extra():
    (row,) = rows([hive()], sources=[])
    assert (row.state, row.severity, row.rule) == ("not-installed", "info", "R9")
    assert row.check_state() == "off"
    assert "beadhive[github]" in row.detail


def test_an_enabled_plugin_that_is_not_installed_is_a_problem_row():
    (row,) = rows([hive()], {"tracker-github": on()}, sources=[])
    assert (row.state, row.severity, row.rule) == ("not-installed", "error", "R9")
    assert row.check_state() == "warn"


def test_a_ready_row_names_the_bound_repository_and_the_enabled_operations():
    (row,) = rows([hive()], {"tracker-github": on()})
    assert (row.state, row.severity, row.check_state()) == ("ready", "info", "ok")
    assert row.project == "octo/widgets"
    assert row.operations == ("issues.read", "comments.read")
    assert (
        row.detail == "tracker-github: ready — octo/widgets; operations: issues.read, comments.read"
    )


def test_a_ready_row_with_a_write_enabled_still_names_the_grant_requirement():
    (row,) = rows([hive()], {"tracker-github": on(comment=True)})
    assert "comment.create" in row.operations
    assert row.detail.endswith("each write needs an operator-approved grant")


def test_an_unresolvable_identity_is_a_warning_only_once_the_plugin_is_on():
    gitlab = hive(provider="gitlab")
    (off,) = rows([gitlab])
    assert (off.state, off.severity) == ("disabled", "info")  # an off plugin never nags
    (live,) = rows([gitlab], {"tracker-github": on()})
    assert (live.state, live.severity, live.rule) == ("identity", "warning", "R12")
    assert live.check_state() == "warn"
    assert live.project is None


def test_two_enabled_providers_without_an_owner_is_a_conflict_row():
    plugins = {"tracker-github": on(), "tracker-other": on()}
    (row,) = rows([hive()], plugins, sources=[source("tracker-github", "tracker-other")])
    assert (row.state, row.severity, row.rule) == ("conflict", "error", "R11")


def test_an_invalid_plugin_fragment_is_retained_inactive_and_reported():
    (row,) = rows([hive()], {"tracker-github": {"enabled": True, "repo": "x/y"}})
    assert (row.state, row.severity, row.check_state()) == ("invalid-config", "error", "warn")


def test_an_adapter_on_a_non_public_store_is_a_binding_warning():
    (row,) = rows([hive(scope="internal")])
    assert (row.state, row.severity, row.scope, row.adapter) == (
        "invalid-binding",
        "warning",
        "internal",
        None,
    )
    assert "only valid on a public store" in row.detail


def test_a_malformed_adapter_value_is_never_echoed():
    (row,) = rows([hive(adapter=SHAPED)])
    assert row.state == "invalid-binding"
    assert SHAPED not in json.dumps(row.as_dict()) and SHAPED not in repr(row)


def test_one_row_per_bound_store_across_hives():
    found = rows(
        [hive("octo", "a"), hive("octo", "b", adapter=None), hive("octo", "c")],
        {"tracker-github": on()},
    )
    assert [(r.hive, r.project) for r in found] == [
        ("github/octo/a", "octo/a"),
        ("github/octo/c", "octo/c"),
    ]


@pytest.mark.parametrize(
    "case",
    [
        {},
        {"plugins": {"tracker-github": on()}},
        {"plugins": {"tracker-github": on()}, "hives": [hive(provider="gitlab")]},
        {"sources": []},
        {"plugins": {"tracker-github": on()}, "sources": []},
        {"plugins": {"tracker-github": {"enabled": True, "repo": "x/y"}}},
        {"hives": [hive(scope="internal")]},
        {"plugins": {"tracker-github": on(), "tracker-other": on()}, "two": True},
    ],
)
def test_no_row_can_ever_be_a_failing_readiness_state(case):
    sources = (
        [source("tracker-github", "tracker-other")] if case.get("two") else case.get("sources")
    )
    for row in rows(case.get("hives", [hive()]), case.get("plugins"), sources=sources):
        assert row.check_state() in {"ok", "na", "off", "warn"}


def test_rows_are_offline_and_carry_no_credential(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("the readiness row must stay offline")

    monkeypatch.setenv("GH_TOKEN", SHAPED)
    monkeypatch.setenv("GITHUB_TOKEN", SHAPED)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    found = rows(
        [hive(), hive(provider="gitlab"), hive(scope="internal")], {"tracker-github": on()}
    )
    assert len(found) == 3
    assert SHAPED not in json.dumps([r.as_dict() for r in found])
