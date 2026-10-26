"""The ``tracker-github`` plugin skeleton through the real composition root (bh-h8kab).

These tests use the REAL built-in manifest (``BUILTIN_PLUGIN_IDS`` lists ``tracker-github``) and
the REAL ``BUILTIN_TRACKER_PROVIDERS`` row, and prove the off-by-default contract the plugin ADR
makes: installing the package and shipping its manifest enables nothing, a binding enables
nothing, enabling the plugin with no binding changes nothing, and nothing imports the package
(or githubkit) before every rule has passed. The factory is a skeleton, so the last step is a
typed refusal, never an adapter and never a request.
"""

from __future__ import annotations

import sys

import pytest

from beadhive.bootstrap import tracker as tracker_root
from beadhive.bootstrap.tracker import (
    BUILTIN_TRACKER_PROVIDERS,
    TrackerAdapters,
    TrackerBindingError,
    TrackerRule,
    TrackerUnconfigured,
)
from beadhive.kernel.plugins import BUILTIN_PLUGIN_IDS
from beadhive.modules.config.contracts import TrackerGithubConfig
from beadhive.modules.tracker.contracts import Operation
from beadhive_tracker_github.factory import TrackerAdapterNotImplemented, build

HIVE = "github/acme/widget"
READ = frozenset({Operation.ISSUES_READ})


def entry(adapter: str | None = "tracker-github") -> dict:
    public = {"prefix": "wpub", "path": "/s/p", "remote": "git-origin", "source": "github-issues"}
    if adapter is not None:
        public["adapter"] = adapter
    return {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "w",
        "stores": {"internal": {"remote": "r"}, "public": public},
    }


def enabled_plugin(**extra) -> dict:
    return {"enabled": True, "read": {"issues": {"enabled": True}}, **extra}


@pytest.fixture
def loads(monkeypatch):
    """Record every lazy package import the composition root makes (and the transport)."""
    calls: list[str] = []
    real = tracker_root.import_module

    def spy(name, *args, **kwargs):
        calls.append(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(tracker_root, "import_module", spy)
    return calls


def root(plugins=None, kernel=None) -> TrackerAdapters:
    return TrackerAdapters(
        plugins=plugins, plugin_kernel=kernel, token_source=lambda: None, sleep=lambda _s: None
    )


def refusal(adapters: TrackerAdapters, the_entry: dict, **kwargs) -> TrackerBindingError:
    with pytest.raises(TrackerBindingError) as caught:
        adapters.for_store(the_entry, **kwargs)
    return caught.value


# ---- the manifest is shipped and discovered, but nothing is on --------------------------------


def test_the_builtin_catalog_lists_the_plugin_and_the_provider_row_is_registered():
    assert "tracker-github" in BUILTIN_PLUGIN_IDS
    assert [row.plugin_id for row in BUILTIN_TRACKER_PROVIDERS] == ["tracker-github"]
    assert BUILTIN_TRACKER_PROVIDERS[0].install_hint == "beadhive[github]"


def test_the_shipped_defaults_are_all_off():
    config = TrackerGithubConfig()
    assert config.enabled is False
    assert config.read.issues.enabled is config.read.comments.enabled is False
    assert config.write.comment.enabled is config.write.labels.enabled is False
    assert config.http_cache.enabled is False


@pytest.mark.parametrize(
    "plugins", [None, {}, {"tracker-github": {}}, {"tracker-github": {"enabled": False}}]
)
def test_discovery_finds_the_real_manifest_yet_selects_no_owner_by_default(plugins):
    composition = root(plugins).composition
    assert composition.manifest("tracker-github") is not None  # shipped, discoverable
    assert composition.owner is None  # ... and not on: no owner, so no port
    assert composition.enabled_operations("tracker-github") == frozenset()


# ---- a binding enables nothing; enabling with no binding changes nothing ----------------------


def test_a_binding_alone_never_enables_the_plugin(loads):
    error = refusal(root(), entry(), requested=READ)
    assert (error.rule, error.rule_id) == (TrackerRule.DISABLED, "R6")
    assert loads == []  # no package import before the rules pass


@pytest.mark.parametrize("enabled", [False, True])
def test_enabling_the_plugin_with_no_binding_changes_nothing(loads, enabled):
    plugins = {"tracker-github": enabled_plugin() if enabled else {"enabled": False}}
    error = refusal(root(plugins), entry(adapter=None))
    assert isinstance(error, TrackerUnconfigured)
    assert (error.rule, error.rule_id) == (TrackerRule.NOT_BOUND, "R13")
    assert error.message == (
        f"{HIVE}: stores.public binds no tracker adapter — declare `stores.public.adapter` "
        "to opt in"
    )
    assert loads == []


def test_the_kernel_policy_still_wins_over_the_plugins_own_flag(loads):
    kernel = {"enabled": {"tracker-github": False}}
    error = refusal(root({"tracker-github": enabled_plugin()}, kernel), entry(), requested=READ)
    assert error.rule is TrackerRule.DISABLED
    assert loads == []


# ---- only after every rule passes does the skeleton factory run, and it refuses ----------------


def test_enabled_and_bound_reaches_the_skeleton_factory_which_refuses_without_the_transport(loads):
    githubkit_before = "githubkit" in sys.modules
    error = refusal(root({"tracker-github": enabled_plugin()}), entry(), requested=READ)

    assert loads == ["beadhive_tracker_github.factory"]  # imported lazily, after selection
    assert error.rule is TrackerRule.NOT_CONFORMING
    assert error.message == (
        f"{HIVE}: tracker adapter 'tracker-github' could not be built "
        "(TrackerAdapterNotImplemented)"
    )
    assert "githubkit" not in error.message
    assert ("githubkit" in sys.modules) is githubkit_before  # the skeleton never imports it


def test_negative_control_the_loader_assertions_would_notice_an_eager_import(loads):
    """The enabled+bound case above is the only one that loads the package; the disabled and
    unbound cases did not. If this stopped being true the assertions above would catch it."""
    refusal(root(), entry(), requested=READ)
    assert loads == []
    refusal(root({"tracker-github": enabled_plugin()}), entry(), requested=READ)
    assert loads == ["beadhive_tracker_github.factory"]


def test_the_factory_builds_nothing_and_names_no_configuration():
    with pytest.raises(TrackerAdapterNotImplemented) as caught:
        build({"token": "must-not-appear"}, lambda: None, project=None, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert "must-not-appear" not in str(caught.value)
    assert isinstance(caught.value, NotImplementedError)
