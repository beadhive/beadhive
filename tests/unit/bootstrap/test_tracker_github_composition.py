"""The ``tracker-github`` plugin through the real composition root (bh-h8kab, bh-51fdo).

These tests use the REAL built-in manifest (``BUILTIN_PLUGIN_IDS`` lists ``tracker-github``) and
the REAL ``BUILTIN_TRACKER_PROVIDERS`` row, and prove the off-by-default contract the plugin ADR
makes: installing the package and shipping its manifest enables nothing, a binding enables
nothing, enabling the plugin with no binding changes nothing, and nothing imports the package
(or githubkit) before every rule has passed. Once every rule passes, the factory builds the
adapter without resolving a credential, sending a request or importing githubkit. The bound
object carries a writer only for an enabled write (``comment.create``, W8; ``labels.change``,
W9), and never for a disabled one. No test here touches the network: only offline port calls
(``describe``, ``resolve``) are made, and no write is ever attempted.
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
from beadhive.modules.tracker.contracts import (
    CommentWriter,
    IssueKey,
    IssueResolver,
    LabelWriter,
    Operation,
    ProjectRef,
    TrackerAdapter,
)
from beadhive_tracker_github.factory import build

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


# ---- only after every rule passes does the factory build the read-path adapter ----------------


def counting_root(plugins=None, kernel=None) -> tuple[TrackerAdapters, list[int]]:
    calls: list[int] = []

    def tokens():
        calls.append(1)
        return None

    adapters = TrackerAdapters(
        plugins=plugins, plugin_kernel=kernel, token_source=tokens, sleep=lambda _s: None
    )
    return adapters, calls


def test_enabled_and_bound_builds_the_read_path_adapter_offline(loads):
    githubkit_before = "githubkit" in sys.modules
    adapters, token_calls = counting_root({"tracker-github": enabled_plugin()})
    bound = adapters.for_store(entry(), requested=READ)

    assert loads == ["beadhive_tracker_github.factory"]  # imported lazily, after selection
    assert bound.enabled == READ
    port = bound.port
    assert isinstance(port, TrackerAdapter) and isinstance(port, IssueResolver)
    assert port.describe().project == ProjectRef("github", "acme/widget")
    assert port.describe().supported == {
        Operation.ISSUES_READ,
        Operation.COMMENTS_READ,
        Operation.COMMENT_CREATE,
        Operation.LABELS_CHANGE,
    }
    assert not isinstance(port, CommentWriter)  # comment.create was not enabled
    assert not isinstance(port, LabelWriter)  # labels.change is not enabled: no writer exposed
    assert port.resolve("https://github.com/acme/widget/issues/3#top") == IssueKey(
        "#3", "https://github.com/acme/widget/issues/3"
    )
    assert token_calls == []  # nothing resolved a credential, so nothing was sent
    assert ("githubkit" in sys.modules) is githubkit_before  # building never imports it


def test_the_comment_write_flag_binds_the_comment_writer_and_sends_nothing(loads):
    plugins = {"tracker-github": enabled_plugin(write={"comment": {"enabled": True}})}
    adapters, token_calls = counting_root(plugins)
    bound = adapters.for_store(entry(), requested=READ | {Operation.COMMENT_CREATE})

    assert Operation.COMMENT_CREATE in bound.enabled  # enabled is necessary, never sufficient
    assert isinstance(bound.port, CommentWriter)
    assert Operation.COMMENT_CREATE in bound.port.describe().supported
    assert not isinstance(bound.port, LabelWriter)  # labels.change was not enabled
    assert token_calls == []  # binding a writer resolves no credential and sends nothing


def test_the_labels_write_flag_binds_the_label_writer_and_sends_nothing(loads):
    plugins = {
        "tracker-github": enabled_plugin(
            write={"labels": {"enabled": True, "owned_prefixes": ["bh:"]}}
        )
    }
    adapters, token_calls = counting_root(plugins)
    bound = adapters.for_store(entry(), requested=READ | {Operation.LABELS_CHANGE})

    assert Operation.LABELS_CHANGE in bound.enabled
    assert isinstance(bound.port, LabelWriter)  # labels.change: enabled AND implemented
    assert not isinstance(bound.port, CommentWriter)  # comment.create was not enabled
    assert token_calls == []


def test_both_write_flags_bind_both_writers(loads):
    plugins = {
        "tracker-github": enabled_plugin(
            write={
                "comment": {"enabled": True},
                "labels": {"enabled": True, "owned_prefixes": ["bh:"]},
            }
        )
    }
    bound = root(plugins).for_store(
        entry(), requested=READ | {Operation.COMMENT_CREATE, Operation.LABELS_CHANGE}
    )

    assert {Operation.COMMENT_CREATE, Operation.LABELS_CHANGE} <= bound.enabled
    assert isinstance(bound.port, CommentWriter) and isinstance(bound.port, LabelWriter)


def test_labels_enabled_with_owned_prefixes_off_binds_no_label_writer(loads):
    labels_off = {"tracker-github": enabled_plugin(write={"labels": {"owned_prefixes": ["bh:"]}})}
    bound = root(labels_off).for_store(entry(), requested=READ)
    assert not isinstance(bound.port, LabelWriter)
    assert not hasattr(bound.port, "change_labels")


def test_negative_control_the_loader_assertions_would_notice_an_eager_import(loads):
    """The enabled+bound case above is the only one that loads the package; the disabled and
    unbound cases did not. If this stopped being true the assertions above would catch it."""
    refusal(root(), entry(), requested=READ)
    assert loads == []
    root({"tracker-github": enabled_plugin()}).for_store(entry(), requested=READ)
    assert loads == ["beadhive_tracker_github.factory"]


def test_the_factory_refuses_a_malformed_call_and_names_no_configuration():
    with pytest.raises(TypeError) as caught:
        build({"token": "must-not-appear"}, lambda: None, project=None, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert "must-not-appear" not in str(caught.value)
    with pytest.raises(ValueError) as caught_value:
        build(
            {"base_url": "https://must-not-appear@ghe.example"},
            lambda: None,
            project=ProjectRef("github", "acme/widget"),
            sleep=lambda _s: None,
        )
    assert "must-not-appear" not in str(caught_value.value)
