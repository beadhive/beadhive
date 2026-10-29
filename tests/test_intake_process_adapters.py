"""``intake_services.process_adapters``: the production wiring of the process ``TrackerAdapters``.

Two host-owned switches, both off by default, build the adapters: ``intake.external.enabled`` and
``plugins.tracker-github.enabled`` (folded with ``plugin_kernel`` like the tracker composition
root). Everything else (the binding, the per-operation enables) is still that root's per-store
decision. What is pinned here:

* off by default: any configuration short of both switches returns ``None`` and builds nothing;
* on: a real ``TrackerAdapters`` whose credential port is ``run.github_token_source`` over the
  configured ``credential.sources``; composing it, and binding a store, read no credential;
* the verbs refuse at the right layer (an unbound store, a read that is not enabled) with a message
  naming the key to set.

Offline: the token source is a recorder that fails the test if it is ever called, the
environment's GitHub variables are cleared, and no ``gh`` process or network is reached.
"""

from __future__ import annotations

import pytest

from beadhive import intake_services, run
from beadhive.bootstrap.tracker import TrackerAdapters
from beadhive.modules.tracker.contracts import Operation

ON_INTAKE = {"intake": {"external": {"enabled": True}}}
PLUGIN = {"enabled": True, "read": {"issues": {"enabled": True}}}
ON = {**ON_INTAKE, "plugins": {"tracker-github": PLUGIN}}


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


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch):
    """Never read the developer's token or spawn ``gh``: clear the variables and fail on a call."""
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        run, "_derive_gh_auth_token", lambda: pytest.fail("gh auth token must not run")
    )


class Sources:
    """A stand-in for ``run.github_token_source`` recording the names it was built with."""

    def __init__(self) -> None:
        self.names: list[tuple[str, ...]] = []
        self.calls = 0

    def __call__(self, environ=None, *, names=("env", "gh"), gh_auth_token=None):
        self.names.append(tuple(names))

        def source():
            self.calls += 1
            pytest.fail("the token source must not be called")

        return source


@pytest.fixture
def sources(monkeypatch) -> Sources:
    recorder = Sources()
    monkeypatch.setattr(run, "github_token_source", recorder)
    return recorder


# -- off by default ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"plugins": {"tracker-github": PLUGIN}},
        ON_INTAKE,
        {**ON_INTAKE, "plugins": {"tracker-github": {"enabled": False}}},
        {**ON_INTAKE, "plugins": {"tracker-github": {"enabled": "true"}}},
        {**ON_INTAKE, "plugins": {"tracker-github": {"read": {"issues": {"enabled": True}}}}},
        {"intake": {"external": {"enabled": False}}, "plugins": {"tracker-github": PLUGIN}},
        {"intake": {"external": {"enabled": "true"}}, "plugins": {"tracker-github": PLUGIN}},
        {**ON, "plugin_kernel": {"enabled": {"tracker-github": False}}},
        {**ON_INTAKE, "plugins": {"tracker-github": {"enabled": True, "bogus": 1}}},
    ],
    ids=[
        "empty",
        "plugin-only",
        "intake-only",
        "plugin-off",
        "plugin-string-true",
        "plugin-absent-flag",
        "intake-off",
        "intake-string-true",
        "kernel-disables",
        "invalid-fragment",
    ],
)
def test_no_adapters_unless_both_switches_are_on(cfg, sources) -> None:
    assert intake_services.process_adapters(cfg) is None
    assert sources.names == []  # not even the credential port is built


# -- on ---------------------------------------------------------------------------------------


def test_both_switches_build_the_adapters_without_reading_a_credential(sources) -> None:
    adapters = intake_services.process_adapters(ON)

    assert isinstance(adapters, TrackerAdapters)
    assert sources.names == [("env", "gh")] and sources.calls == 0


def test_binding_a_store_builds_the_adapter_but_still_reads_no_credential(sources) -> None:
    adapters = intake_services.process_adapters(ON)

    bound = adapters.for_store(entry(), requested=frozenset({Operation.ISSUES_READ}))

    assert bound.adapter_id == "tracker-github" and bound.port is not None
    assert Operation.ISSUES_READ in bound.enabled
    assert sources.calls == 0


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(["gh", "env"], ("gh", "env")), (["env"], ("env",)), ([], ())],
)
def test_the_configured_credential_sources_are_forwarded(sources, configured, expected) -> None:
    plugin = {**PLUGIN, "credential": {"sources": configured}}
    cfg = {**ON_INTAKE, "plugins": {"tracker-github": plugin}}

    assert intake_services.process_adapters(cfg) is not None
    assert sources.names == [expected]


def test_the_real_token_source_is_the_one_wired_and_is_lazy() -> None:
    # the autouse fixture cleared the environment and made `gh` fail: building must not touch them
    adapters = intake_services.process_adapters(ON)

    assert isinstance(adapters, TrackerAdapters)
    assert adapters.for_store(entry(), requested=frozenset({Operation.ISSUES_READ})) is not None


def test_nothing_is_cached_across_calls(sources) -> None:
    first = intake_services.process_adapters(ON)
    second = intake_services.process_adapters(ON)

    assert first is not second  # a configuration snapshot per command, never process-global


# -- the verbs refuse at the right layer ------------------------------------------------------


def test_import_with_the_tracker_off_names_both_switches(tmp_path, sources) -> None:
    result = intake_services.import_issue(
        ON_INTAKE, entry(), tmp_path, "https://github.com/acme/widget/issues/1", "dev/x"
    )

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "plugins.tracker-github.enabled" in result.error
    assert "intake.external.enabled" in result.error
    assert "bh-kzs0b" not in result.error


def test_import_on_an_unbound_store_names_the_binding_key(tmp_path, sources) -> None:
    result = intake_services.import_issue(
        ON, entry(adapter=None), tmp_path, "https://github.com/acme/widget/issues/1", "dev/x"
    )

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "stores.public.adapter" in result.error and sources.calls == 0


def test_import_with_the_read_not_enabled_names_the_read_key(tmp_path, sources) -> None:
    cfg = {**ON_INTAKE, "plugins": {"tracker-github": {"enabled": True}}}

    result = intake_services.import_issue(
        cfg, entry(), tmp_path, "https://github.com/acme/widget/issues/1", "dev/x"
    )

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "issues" in result.error and sources.calls == 0
