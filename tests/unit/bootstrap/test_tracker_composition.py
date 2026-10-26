"""The ``tracker.adapter`` composition root, end to end with ``MemoryTracker``.

A TEST-ONLY manifest source declares ``tracker.adapter@1`` (the real kernel discovers it); the
provider row builds a ``MemoryTracker``. No plugin package, no network, no clock: the category is
proven before the GitHub package exists (tracker-adapter-plugin-adr.md, W4). Every refusal
message below is the exact ADR text (section 4, the bh-6bkun table).
"""

from __future__ import annotations

import ast
import copy
import importlib
import importlib.abc
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from beadhive.bootstrap import tracker as tracker_root
from beadhive.bootstrap.tracker import (
    BUILTIN_TRACKER_PROVIDERS,
    TRACKER_ADAPTER,
    TRACKER_ADAPTER_PORT,
    TrackerAdapterProvider,
    TrackerAdapterRequest,
    TrackerAdapters,
    TrackerBindingError,
    TrackerRule,
    TrackerUnconfigured,
)
from beadhive.kernel.plugins import (
    BuiltInManifestSource,
    DiagnosticCode,
    ManifestDocument,
    ManifestProvenance,
)
from beadhive.modules.tracker.contracts import (
    CommentWriter,
    IssueQuery,
    IssueResolver,
    LabelOwnership,
    LabelWriter,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    ProjectRef,
    TrackerAdapter,
    WriteGrant,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

REPO = Path(__file__).resolve().parents[3]
HIVE = "github/acme/widget"

# The manifest a tracker plugin ships (bh-rktrh section 2), as test data only.
MANIFEST = {
    "capabilities": {"provides": [{"api_version": 1, "id": "tracker.adapter"}]},
    "compatibility": {
        "beadhive": {"maximum_exclusive": "1.0.0", "minimum_inclusive": "0.15.0"},
        "plugin_kernel": {"maximum_exclusive": "2.0.0", "minimum_inclusive": "1.0.0"},
    },
    "configuration": {
        "legacy_namespaces": [],
        "namespace": "plugins.tracker-github",
        "schema_artifact": "urn:beadhive:wire-schema:plugin-config:tracker-github:1",
    },
    "lifecycle": {"subscriptions": []},
    "manifest_version": 1,
    "plugin_id": "tracker-github",
    "plugin_version": "1.0.0",
    "presentation": {
        "cli": [{"capability_id": "tracker.adapter", "command": "plugin tracker-github status"}]
    },
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
        "executables": [],
        "permissions": [],
    },
}


def manifest(plugin_id: str = "tracker-github") -> dict:
    doc = copy.deepcopy(MANIFEST)
    doc["plugin_id"] = plugin_id
    doc["configuration"]["namespace"] = f"plugins.{plugin_id}"
    if plugin_id != "tracker-github":
        doc["configuration"]["schema_artifact"] = None
    doc["presentation"]["cli"][0]["command"] = f"plugin {plugin_id} status"
    return doc


def source(*plugin_ids: str) -> BuiltInManifestSource:
    return BuiltInManifestSource(
        tuple(
            ManifestDocument(
                ManifestProvenance("test-only", f"{pid}.json"), json.dumps(manifest(pid)).encode()
            )
            for pid in plugin_ids
        )
    )


def entry(provider="github", org="acme", repo="widget", adapter="tracker-github") -> dict:
    public = {"prefix": "wpub", "path": "/s/p", "remote": "git-origin", "source": "github-issues"}
    if adapter is not None:
        public["adapter"] = adapter
    return {
        "provider": provider,
        "org": org,
        "repo": repo,
        "prefix": "w",
        "stores": {"internal": {"remote": "r"}, "public": public},
    }


def plugin(enabled=True, *, issues=True, comments=False, comment=False, labels=False, prefixes=()):
    return {
        "enabled": enabled,
        "read": {"issues": {"enabled": issues}, "comments": {"enabled": comments}},
        "write": {
            "comment": {"enabled": comment},
            "labels": {"enabled": labels, "owned_prefixes": list(prefixes)},
        },
    }


class Factory:
    """A provider loader that builds a MemoryTracker and records every call."""

    def __init__(self) -> None:
        self.world = MemoryWorld(locator="acme/widget")
        self.requests: list[TrackerAdapterRequest] = []

    def __call__(self, request: TrackerAdapterRequest) -> MemoryTracker:
        self.requests.append(request)
        return MemoryTracker(self.world, enabled=request.enabled)


def roots(
    plugins=None,
    *,
    kernel=None,
    sources=None,
    factory: Factory | None = None,
    ids=("tracker-github",),
):
    factory = factory or Factory()
    providers = tuple(TrackerAdapterProvider(pid, "beadhive[github]", factory) for pid in ids)
    adapters = TrackerAdapters(
        plugins={"tracker-github": plugin()} if plugins is None else plugins,
        plugin_kernel=kernel,
        token_source=lambda: None,
        sources=[source("tracker-github")] if sources is None else sources,
        providers=providers,
        sleep=factory.world.sleep,
    )
    return adapters, factory


def refusal(adapters, the_entry=None, **kwargs) -> TrackerBindingError:
    with pytest.raises(TrackerBindingError) as caught:
        adapters.for_store(entry() if the_entry is None else the_entry, **kwargs)
    return caught.value


# ---- a bound store resolves -----------------------------------------------------------------


def test_a_bound_enabled_store_resolves_a_memory_tracker_through_the_kernel():
    adapters, factory = roots()
    bound = adapters.for_store(entry(), requested=frozenset({Operation.ISSUES_READ}))
    factory.world.add_issue("first", at=at(1))

    assert isinstance(bound.port, TrackerAdapter)
    assert [i.title for i in bound.port.list_issues(IssueQuery()).items] == ["first"]
    assert (bound.hive, bound.scope, bound.adapter_id) == (HIVE, "public", "tracker-github")
    assert bound.project == ProjectRef("github", "acme/widget")
    assert bound.enabled == frozenset({Operation.ISSUES_READ})
    (request,) = factory.requests
    assert request.project == bound.project
    # The factory sees the folded settings: every flag equals the resolved enabled set.
    assert request.settings["read"]["issues"]["enabled"] is True
    assert request.settings["read"]["comments"]["enabled"] is False
    assert request.settings["write"]["comment"]["enabled"] is False
    assert TRACKER_ADAPTER.render() == "tracker.adapter@1"
    assert TRACKER_ADAPTER_PORT.port_type is TrackerAdapter


def test_one_adapter_per_hive_and_store_scope_is_cached_never_recreated():
    adapters, factory = roots()
    first = adapters.for_store(entry())
    again = adapters.for_store(entry(), requested=frozenset({Operation.ISSUES_READ}))
    other = adapters.for_store(entry(repo="gadget"))

    assert again.port is first.port
    assert other.port is not first.port
    assert other.project == ProjectRef("github", "acme/gadget")
    assert len(factory.requests) == 2


def test_a_cached_adapter_still_refuses_an_operation_that_is_not_enabled():
    adapters, _ = roots()
    adapters.for_store(entry())
    error = refusal(adapters, requested=frozenset({Operation.COMMENTS_READ}))
    assert error.rule is TrackerRule.READ_NOT_ENABLED


# ---- R6 to R13, exact messages, first failing rule wins -------------------------------------


R6 = (
    f"{HIVE}: stores.public binds tracker adapter 'tracker-github' but the plugin is disabled — "
    "set plugins.tracker-github.enabled: true on this host (a binding never enables a plugin)"
)


def test_r13_an_unbound_store_is_the_consumers_tracker_unconfigured():
    adapters, factory = roots()
    error = refusal(adapters, entry(adapter=None))
    assert isinstance(error, TrackerUnconfigured)
    assert (error.rule, error.rule_id) == (TrackerRule.NOT_BOUND, "R13")
    assert error.message == (
        f"{HIVE}: stores.public binds no tracker adapter — declare `stores.public.adapter` "
        "to opt in"
    )
    no_stores = {k: v for k, v in entry().items() if k != "stores"}
    assert isinstance(refusal(adapters, no_stores), TrackerUnconfigured)
    assert factory.requests == []


@pytest.mark.parametrize(
    "plugins",
    [
        {},
        {"tracker-github": {}},
        {"tracker-github": plugin(enabled=False)},
        {"tracker-github": plugin(enabled="true")},
        {"tracker-github": plugin(enabled=1)},
    ],
    ids=["absent-namespace", "absent-flag", "false", "string-true", "truthy-int"],
)
def test_r6_every_disabled_form_refuses_with_the_plugin_disabled_message(plugins):
    adapters, factory = roots(plugins)
    error = refusal(adapters, requested=frozenset({Operation.ISSUES_READ}))
    assert (error.rule, error.rule_id, error.message) == (TrackerRule.DISABLED, "R6", R6)
    assert error.code is DiagnosticCode.DISABLED_PLUGIN
    assert factory.requests == []


def test_r6_kernel_disabled_wins_over_the_plugins_own_flag():
    adapters, factory = roots(kernel={"enabled": {"tracker-github": False}})
    error = refusal(adapters)
    assert (error.rule_id, error.message) == ("R6", R6)
    assert factory.requests == []


def test_r12_identity_does_not_resolve():
    adapters, _ = roots()
    gitlab = refusal(adapters, entry(provider="gitlab"))
    assert (gitlab.rule, gitlab.rule_id) == (TrackerRule.IDENTITY, "R12")
    assert gitlab.message == (
        "gitlab/acme/widget: cannot derive a GitHub repository — the hive provider is "
        "'gitlab', not 'github'"
    )
    nameless = refusal(adapters, entry(repo=""))
    assert nameless.message == "github/acme/: the hive identity names no owner/repository"


def test_r9_no_discovered_plugin_has_the_bound_id():
    adapters, factory = roots(sources=[source("tracker-other")], ids=("tracker-other",))
    error = refusal(adapters)
    assert (error.rule, error.rule_id) == (TrackerRule.NOT_INSTALLED, "R9")
    assert error.message == (
        f"{HIVE}: stores.public binds tracker adapter 'tracker-github' but no discovered plugin "
        "has that id — install `beadhive[github]`"
    )
    assert error.code is DiagnosticCode.TRACKER_PACKAGE
    assert factory.requests == []


def test_r9_a_selected_plugin_whose_package_is_absent_names_the_extra():
    def absent(_request):
        raise ModuleNotFoundError("No module named 'beadhive_tracker_github'")

    adapters = TrackerAdapters(
        plugins={"tracker-github": plugin()},
        plugin_kernel=None,
        token_source=lambda: None,
        sources=[source("tracker-github")],
        providers=(TrackerAdapterProvider("tracker-github", "beadhive[github]", absent),),
    )
    error = refusal(adapters)
    assert (error.rule, error.rule_id) == (TrackerRule.PACKAGE_MISSING, "R9")
    assert error.message.endswith("install `beadhive[github]`")


def test_r11_two_enabled_providers_with_no_owner_selected():
    configs = {"tracker-github": plugin(), "tracker-other": plugin()}
    adapters, factory = roots(
        configs,
        sources=[source("tracker-github", "tracker-other")],
        ids=("tracker-github", "tracker-other"),
    )
    error = refusal(adapters)
    assert (error.rule, error.rule_id) == (TrackerRule.CONFLICT, "R11")
    assert error.message == (
        f"{HIVE}: two enabled plugins provide tracker.adapter@1 — select one with "
        "plugin_kernel.capability_owners"
    )
    assert factory.requests == []


def test_r11_does_not_fire_while_the_second_provider_is_off_by_default():
    adapters, _ = roots(
        sources=[source("tracker-github", "tracker-other")], ids=("tracker-github", "tracker-other")
    )
    assert adapters.for_store(entry()).adapter_id == "tracker-github"


def test_r10_another_plugin_is_the_selected_owner():
    configs = {"tracker-github": plugin(), "tracker-other": plugin()}
    adapters, factory = roots(
        configs,
        kernel={"capability_owners": {"tracker.adapter@1": "tracker-other"}},
        sources=[source("tracker-github", "tracker-other")],
        ids=("tracker-github", "tracker-other"),
    )
    error = refusal(adapters)
    assert (error.rule, error.rule_id) == (TrackerRule.NOT_OWNER, "R10")
    assert error.message == (
        f"{HIVE}: stores.public binds tracker adapter 'tracker-github' but 'tracker-other' is "
        "the selected owner of tracker.adapter@1"
    )
    assert factory.requests == []


def test_r8_a_read_requested_without_enablement():
    adapters, factory = roots()
    error = refusal(adapters, requested=frozenset({Operation.COMMENTS_READ}))
    assert (error.rule, error.rule_id) == (TrackerRule.READ_NOT_ENABLED, "R8")
    assert error.message == (
        f"{HIVE}: capability comments.read was requested for stores.public but is not enabled "
        "— set plugins.tracker-github.read.comments.enabled: true"
    )
    assert factory.requests == []


def test_r7_a_write_requested_without_enablement_fails_before_any_approval():
    adapters, factory = roots()
    error = refusal(adapters, requested=frozenset({Operation.COMMENT_CREATE}))
    assert (error.rule, error.rule_id) == (TrackerRule.WRITE_NOT_ENABLED, "R7")
    assert error.message == (
        f"{HIVE}: write comment.create was requested for stores.public but is not enabled — "
        "set plugins.tracker-github.write.comment.enabled: true (enabling a write is necessary, "
        "never sufficient: each write still needs an operator-approved single-use grant)"
    )
    assert error.code is DiagnosticCode.TRACKER_CAPABILITY
    assert factory.requests == []


def test_r7_labels_with_no_owned_prefixes_are_not_enabled():
    adapters, _ = roots({"tracker-github": plugin(labels=True)})
    error = refusal(adapters, requested=frozenset({Operation.LABELS_CHANGE}))
    assert error.rule_id == "R7"
    assert "plugins.tracker-github.write.labels.owned_prefixes to a non-empty list" in error.message


def test_enabling_a_write_enables_no_read():
    adapters, _ = roots({"tracker-github": plugin(issues=False, comment=True)})
    error = refusal(adapters, requested=frozenset({Operation.ISSUES_READ}))
    assert error.rule_id == "R8"


@pytest.mark.parametrize(
    ("the_entry", "plugins", "sources", "rule"),
    [
        # unbound beats everything else being wrong too
        (entry(provider="gitlab", adapter=None), {}, [], TrackerRule.NOT_BOUND),
        # identity beats not installed
        (entry(provider="gitlab"), {}, [], TrackerRule.IDENTITY),
        # installed beats disabled
        (entry(), {}, [], TrackerRule.NOT_INSTALLED),
        # disabled beats the requested write: no write complaint about a plugin that is off
        (entry(), {"tracker-github": plugin(enabled=False)}, None, TrackerRule.DISABLED),
    ],
    ids=["not-bound", "identity", "not-installed", "disabled"],
)
def test_the_first_failing_rule_is_reported_in_adr_order(the_entry, plugins, sources, rule):
    adapters, _ = roots(plugins, sources=sources)
    error = refusal(adapters, the_entry, requested=frozenset({Operation.COMMENT_CREATE}))
    assert error.rule is rule


def test_an_invalid_fragment_is_retained_inactive_and_refused():
    adapters, factory = roots({"tracker-github": {**plugin(), "repo": "acme/widget"}})
    error = refusal(adapters)
    assert error.rule is TrackerRule.INVALID_CONFIG
    assert error.code is DiagnosticCode.TRACKER_CONFIG
    assert "acme/widget" not in error.message.split(": ", 1)[1]  # the value is not repeated
    assert factory.requests == []


def test_a_malformed_binding_is_refused_without_repeating_the_value():
    secret = "ghp_" + "Z9y8X7w6V5" * 4
    adapters, _ = roots()
    error = refusal(adapters, entry(adapter=secret))
    assert error.rule is TrackerRule.INVALID_BINDING
    assert secret not in error.message and secret not in str(error)


def test_the_default_host_binds_nothing_and_imports_no_package():
    factory = Factory()
    adapters = TrackerAdapters(
        plugins=None,
        plugin_kernel=None,
        token_source=lambda: None,
        providers=(TrackerAdapterProvider("tracker-github", "beadhive[github]", factory),),
    )
    error = refusal(adapters, requested=frozenset({Operation.ISSUES_READ}))
    assert error.rule in {TrackerRule.NOT_INSTALLED, TrackerRule.DISABLED}
    assert factory.requests == []


# ---- optional writers: isinstance on the bound object, grant still required -----------------


def test_writers_are_absent_from_the_bound_object_unless_their_write_is_enabled():
    adapters, _ = roots()
    port = adapters.for_store(entry()).port
    # MemoryTracker implements both writers, but neither write is enabled.
    assert not isinstance(port, CommentWriter)
    assert not isinstance(port, LabelWriter)
    assert isinstance(port, IssueResolver)  # optional, passed through when implemented


def test_enabling_a_write_flag_adds_only_its_writer():
    adapters, _ = roots({"tracker-github": plugin(comment=True)})
    port = adapters.for_store(entry(), requested=frozenset({Operation.COMMENT_CREATE})).port
    assert isinstance(port, CommentWriter)
    assert not isinstance(port, LabelWriter)


def test_an_enabled_write_without_a_write_grant_is_still_refused():
    adapters, factory = roots(
        {"tracker-github": plugin(comment=True, labels=True, prefixes=("bh:",))}
    )
    port = adapters.for_store(entry()).port
    world = factory.world
    world.set_write_time(at(2))
    issue = world.add_issue("target", at=at(1))
    marker = OwnMarker("bh", "install", "outbox-1")
    assert isinstance(port, CommentWriter) and isinstance(port, LabelWriter)

    with pytest.raises(OperationNotEnabled):
        port.post_comment(issue, "hello", marker=marker, grant=None)  # type: ignore[arg-type]
    wrong = WriteGrant("g-1", Operation.LABELS_CHANGE, issue)
    with pytest.raises(OperationNotEnabled):
        port.post_comment(issue, "hello", marker=marker, grant=wrong)
    with pytest.raises(OperationNotEnabled):
        port.change_labels(
            issue,
            add=frozenset({"bh:x"}),
            remove=frozenset(),
            ownership=LabelOwnership(("bh:",)),
            grant="approved",  # type: ignore[arg-type]
        )
    assert world.writes == 0

    receipt = port.post_comment(
        issue, "hello", marker=marker, grant=WriteGrant("g-2", Operation.COMMENT_CREATE, issue)
    )
    assert receipt.grant_id == "g-2" and world.writes == 1


# ---- structure: lazy package import, one version constant, no higher layers -----------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_builtin_row_imports_the_adapter_package_lazily_only():
    (row,) = BUILTIN_TRACKER_PROVIDERS
    assert (row.plugin_id, row.install_hint) == ("tracker-github", "beadhive[github]")
    for name in ("tracker.py", "tracker_status.py"):
        imported = _imports(REPO / "src/beadhive/bootstrap" / name)
        assert not {m for m in imported if m.startswith("beadhive_tracker_github")}
    # No `sys.modules` assertion: the package exists in this workspace now, and another test in
    # the same process may legitimately have imported it. The static scan above is the proof;
    # the runtime half (a fresh interpreter) lives in the package's own tests
    # (packages/beadhive-tracker-github/tests/test_import_rules.py).


def test_composition_roots_share_one_host_and_kernel_version_constant():
    roots_using_discovery = [
        REPO / "src/beadhive/plugins.py",
        *(REPO / "src/beadhive/bootstrap").glob("*.py"),
    ]
    literals = []
    for path in roots_using_discovery:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and node.value in {"0.15.1", "1.0.0"}:
                literals.append(f"{path.name}:{node.lineno}")
    assert literals == []
    from beadhive.kernel.plugins import BEADHIVE_COMPATIBILITY_VERSION, PLUGIN_KERNEL_VERSION

    assert (BEADHIVE_COMPATIBILITY_VERSION, PLUGIN_KERNEL_VERSION) == ("0.15.1", "1.0.0")


_FORBIDDEN_IMPORTS = (
    "beadhive.cli",
    "beadhive.mcp",
    "beadhive.config",
    "beadhive.plugins",
    "beadhive.run",
    "beadhive.work",
    "beadhive.modules.work",
    "beadhive.modules.intake",
    "beadhive_tracker_github",
    "typer",
    "httpx",
    "githubkit",
)


def test_the_composition_root_runs_without_higher_layers_or_effects(monkeypatch):
    """Module-internal evidence: the category composes and binds a MemoryTracker with the CLI,
    the legacy config and plugin facades, the work layer, every HTTP client and the adapter
    package refused at import, and with no socket, process or real sleep allowed."""
    for name in tuple(sys.modules):
        if name.startswith(("beadhive.bootstrap.tracker", *_FORBIDDEN_IMPORTS)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class Refuse(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(_FORBIDDEN_IMPORTS):
                raise AssertionError(f"tracker composition imported a higher layer: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"tracker composition attempted {action}")

        return fail

    finder = Refuse()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(socket, "create_connection", forbidden("a network connection"))
    monkeypatch.setattr(socket.socket, "connect", forbidden("a network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("a process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("a process spawn"))
    monkeypatch.setattr(time, "sleep", forbidden("a real sleep"))
    try:
        root = importlib.import_module("beadhive.bootstrap.tracker")
        status = importlib.import_module("beadhive.bootstrap.tracker_status")
        factory = Factory()
        adapters = root.TrackerAdapters(
            plugins={"tracker-github": plugin()},
            plugin_kernel=None,
            token_source=lambda: None,
            sources=[source("tracker-github")],
            providers=(root.TrackerAdapterProvider("tracker-github", "x", factory),),
            sleep=factory.world.sleep,
        )
        bound = adapters.for_store(entry(), requested=frozenset({Operation.ISSUES_READ}))
        assert bound.port.list_issues(IssueQuery()).items == ()
        doc = status.tracker_status(
            "tracker-github",
            composition=adapters.composition,
            hives=[entry()],
            environ={},
            which=lambda _name: None,
        )
        assert doc["state"] == "ready"
        loaded = {name for name in sys.modules if name.startswith(_FORBIDDEN_IMPORTS)}
        assert not loaded, sorted(loaded)
    finally:
        sys.meta_path.remove(finder)


def test_the_module_reexports_what_it_declares():
    assert set(tracker_root.__all__) <= set(dir(tracker_root))
