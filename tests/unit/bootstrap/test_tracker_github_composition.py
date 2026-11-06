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
    CloseReason,
    CommentWriter,
    IssueCloser,
    IssueKey,
    IssueResolver,
    LabelDefiner,
    LabelDefinition,
    LabelPruner,
    LabelReader,
    LabelWriter,
    Operation,
    OperationNotEnabled,
    ProjectRef,
    TrackerAdapter,
    WriteGrant,
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
        Operation.LABELS_READ,
        Operation.LABELS_DEFINE,
        Operation.ISSUE_CLOSE,
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


# ---- labels.read / labels.define (bh-la587): flags wired, adapter implementation is later ----


def test_the_operation_flag_keys_match_the_plugins_settings_and_the_config_model():
    from beadhive_tracker_github.adapter import settings as plugin_settings

    assert dict(tracker_root.OPERATION_FLAG_KEYS) == dict(plugin_settings.OPERATION_FLAG_KEYS)
    assert set(tracker_root.OPERATION_FLAG_KEYS) == set(Operation)
    assert tracker_root.OPERATION_FLAG_KEYS[Operation.LABELS_READ] == "read.labels.enabled"
    assert (
        tracker_root.OPERATION_FLAG_KEYS[Operation.LABELS_DEFINE]
        == "write.label_definitions.enabled"
    )
    config = TrackerGithubConfig().model_dump()
    for key in tracker_root.OPERATION_FLAG_KEYS.values():
        node = config
        for part in key.split("."):
            node = node[part]
        assert node is False  # every operation flag exists in the model and defaults off


def test_the_adapter_settings_parse_the_two_label_flags():
    from beadhive_tracker_github.adapter.settings import parse_settings

    assert parse_settings({}).enabled == frozenset()
    flags = {
        "read": {"labels": {"enabled": True}},
        "write": {"label_definitions": {"enabled": True}},
    }
    # labels.define only ever writes owned names, so it is off without an owned prefix.
    assert parse_settings(flags).enabled == {Operation.LABELS_READ}
    owned = {**flags, "write": {**flags["write"], "labels": {"owned_prefixes": ["bh:"]}}}
    parsed = parse_settings(owned)
    assert parsed.enabled == {Operation.LABELS_READ, Operation.LABELS_DEFINE}
    with pytest.raises(ValueError):
        parse_settings({"read": {"labels": {"enabled": "true"}}})


def test_the_two_label_flags_are_folded_into_the_enabled_operations():
    plugins = {
        "tracker-github": enabled_plugin(
            read={"issues": {"enabled": True}, "labels": {"enabled": True}},
            write={"label_definitions": {"enabled": True}},
        )
    }
    settings = tracker_root.plugin_settings("tracker-github", plugins)

    assert settings.operations() == {
        Operation.ISSUES_READ,
        Operation.LABELS_READ,
        Operation.LABELS_DEFINE,
    }
    assert tracker_root.plugin_settings("tracker-github", {}).operations() == frozenset()


class _LabelAdapter:
    """A bare adapter implementing both optional label protocols."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def describe(self):
        raise AssertionError("not used")

    def list_label_definitions(self):
        self.calls.append("read")
        return (LabelDefinition("bh:intake/accepted", "0E8A16", "x"),)

    def define_labels(self, definitions, *, grant):
        self.calls.append("define")
        return grant

    def delete_labels(self, names, *, grant):
        self.calls.append("delete")
        return grant


class _DefineOnlyAdapter:
    """An adapter that defines labels but cannot delete them (no ``LabelPruner``)."""

    def describe(self):
        raise AssertionError("not used")

    def define_labels(self, definitions, *, grant):
        return grant


def test_narrow_binds_the_label_reader_and_definer_only_when_their_operation_is_enabled():
    adapter = _LabelAdapter()
    off = tracker_root.narrow(adapter, frozenset())
    only_read = tracker_root.narrow(adapter, frozenset({Operation.LABELS_READ}))
    only_define = tracker_root.narrow(adapter, frozenset({Operation.LABELS_DEFINE}))
    both = tracker_root.narrow(adapter, frozenset({Operation.LABELS_READ, Operation.LABELS_DEFINE}))

    assert not isinstance(off, LabelReader) and not isinstance(off, LabelDefiner)
    assert isinstance(only_read, LabelReader) and not isinstance(only_read, LabelDefiner)
    assert isinstance(only_define, LabelDefiner) and not isinstance(only_define, LabelReader)
    assert isinstance(both, LabelReader) and isinstance(both, LabelDefiner)
    assert not isinstance(both, LabelWriter) and not isinstance(both, CommentWriter)
    assert both.list_label_definitions()[0].name == "bh:intake/accepted"
    assert adapter.calls == ["read"]


def test_narrow_binds_the_label_pruner_only_when_labels_define_is_enabled_and_implemented():
    adapter = _LabelAdapter()
    off = tracker_root.narrow(adapter, frozenset())
    only_read = tracker_root.narrow(adapter, frozenset({Operation.LABELS_READ}))
    only_define = tracker_root.narrow(adapter, frozenset({Operation.LABELS_DEFINE}))
    define_only = tracker_root.narrow(_DefineOnlyAdapter(), frozenset({Operation.LABELS_DEFINE}))

    assert not isinstance(off, LabelPruner) and not hasattr(off, "delete_labels")
    assert not isinstance(only_read, LabelPruner)
    assert isinstance(only_define, LabelPruner) and isinstance(only_define, LabelDefiner)
    assert isinstance(define_only, LabelDefiner) and not isinstance(define_only, LabelPruner)
    assert not hasattr(define_only, "delete_labels")


def test_a_label_deletion_needs_a_matching_single_use_grant_of_the_define_operation():
    adapter = _LabelAdapter()
    port = tracker_root.narrow(adapter, frozenset({Operation.LABELS_DEFINE}))
    project = ProjectRef("github", "acme/widget")

    with pytest.raises(OperationNotEnabled):
        port.delete_labels(("bh:x",), grant="approved")  # type: ignore[attr-defined]
    with pytest.raises(OperationNotEnabled):
        port.delete_labels(  # type: ignore[attr-defined]
            ("bh:x",), grant=WriteGrant("g-1", Operation.LABELS_CHANGE, project)
        )
    assert adapter.calls == []
    grant = WriteGrant("g-2", Operation.LABELS_DEFINE, project)
    assert port.delete_labels(("bh:x",), grant=grant) is grant  # type: ignore[attr-defined]
    assert adapter.calls == ["delete"]


def test_the_built_github_adapter_is_a_pruner_in_the_root_view_only_when_define_is_enabled(
    loads,
):
    write = {
        "label_definitions": {"enabled": True},
        "labels": {"owned_prefixes": ["bh:"]},
    }
    enabled = root({"tracker-github": enabled_plugin(write=write)}).for_store(
        entry(), requested=READ | {Operation.LABELS_DEFINE}
    )
    assert Operation.LABELS_DEFINE in enabled.enabled
    assert isinstance(enabled.port, LabelDefiner) and isinstance(enabled.port, LabelPruner)

    off = root({"tracker-github": enabled_plugin()}).for_store(entry(), requested=READ)
    assert not isinstance(off.port, LabelDefiner) and not isinstance(off.port, LabelPruner)
    assert not hasattr(off.port, "delete_labels")


def test_a_label_definition_write_needs_a_matching_single_use_grant():
    adapter = _LabelAdapter()
    port = tracker_root.narrow(adapter, frozenset({Operation.LABELS_DEFINE}))
    project = ProjectRef("github", "acme/widget")

    with pytest.raises(OperationNotEnabled):
        port.define_labels((), grant="approved")  # type: ignore[attr-defined]
    with pytest.raises(OperationNotEnabled):
        port.define_labels(  # type: ignore[attr-defined]
            (), grant=WriteGrant("g-1", Operation.LABELS_CHANGE, project)
        )
    assert adapter.calls == []
    grant = WriteGrant("g-2", Operation.LABELS_DEFINE, project)
    assert port.define_labels((), grant=grant) is grant  # type: ignore[attr-defined]
    assert adapter.calls == ["define"]


def test_a_requested_but_disabled_label_operation_names_its_flag():
    adapters = root({"tracker-github": enabled_plugin()})
    error = refusal(
        adapters,
        entry(),
        requested=READ | {Operation.LABELS_READ, Operation.LABELS_DEFINE},
    )

    assert error.rule is TrackerRule.WRITE_NOT_ENABLED
    assert "plugins.tracker-github.read.labels.enabled" in error.message
    assert "plugins.tracker-github.write.label_definitions.enabled" in error.message


def test_the_issue_close_flag_binds_the_closer_in_the_root_view_and_sends_nothing(loads):
    plugins = {"tracker-github": enabled_plugin(write={"issue_close": {"enabled": True}})}
    adapters, token_calls = counting_root(plugins)
    bound = adapters.for_store(entry(), requested=READ | {Operation.ISSUE_CLOSE})

    assert Operation.ISSUE_CLOSE in bound.enabled  # enabled is necessary, never sufficient
    assert isinstance(bound.port, IssueCloser)
    assert not isinstance(bound.port, CommentWriter) and not isinstance(bound.port, LabelWriter)
    assert token_calls == []

    off = root({"tracker-github": enabled_plugin()}).for_store(entry(), requested=READ)
    assert not isinstance(off.port, IssueCloser) and not hasattr(off.port, "close_issue")
    error = refusal(
        root({"tracker-github": enabled_plugin()}),
        entry(),
        requested=READ | {Operation.ISSUE_CLOSE},
    )
    assert "plugins.tracker-github.write.issue_close.enabled" in error.message


def test_a_close_through_the_root_view_needs_a_close_grant():
    class _Closer:
        def __init__(self):
            self.calls: list[str] = []

        def close_issue(self, issue, *, reason, grant):
            self.calls.append(reason.value)
            return grant

    adapter = _Closer()
    port = tracker_root.narrow(adapter, frozenset({Operation.ISSUE_CLOSE}))
    issue = IssueKey("#3", "https://github.com/acme/widget/issues/3")
    with pytest.raises(OperationNotEnabled):
        port.close_issue(  # type: ignore[attr-defined]
            issue,
            reason=CloseReason.COMPLETED,
            grant=WriteGrant("g", Operation.LABELS_CHANGE, issue),
        )
    assert adapter.calls == []
    grant = WriteGrant("g-1", Operation.ISSUE_CLOSE, issue)
    assert port.close_issue(issue, reason=CloseReason.DUPLICATE, grant=grant) is grant  # type: ignore[attr-defined]
    assert adapter.calls == ["duplicate"]
    assert not isinstance(tracker_root.narrow(adapter, frozenset()), IssueCloser)
