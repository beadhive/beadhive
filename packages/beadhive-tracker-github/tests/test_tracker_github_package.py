"""The package: packaged manifest, resources and the factory root's lazy row calls."""

from __future__ import annotations

import json
from importlib import resources

import pytest

import beadhive_tracker_github
from beadhive.modules.tracker.contracts import IssueResolver, ProjectRef, TrackerAdapter
from beadhive_tracker_github import factory


def _manifest() -> dict:
    return json.loads(
        (resources.files("beadhive_tracker_github") / "plugin.json").read_text(encoding="utf-8")
    )


def test_packaged_plugin_manifest_declares_the_tracker_adapter_capability() -> None:
    manifest = _manifest()

    assert manifest["plugin_id"] == beadhive_tracker_github.PLUGIN_ID == "tracker-github"
    assert manifest["capabilities"]["provides"] == [{"api_version": 1, "id": "tracker.adapter"}]
    assert manifest["configuration"]["namespace"] == "plugins.tracker-github"
    assert manifest["lifecycle"]["subscriptions"] == []
    assert [item["name"] for item in manifest["security"]["executables"]] == ["gh"]
    assert all(not item["required"] for item in manifest["security"]["executables"])


def test_typed_marker_and_manifest_ship_as_resources() -> None:
    package = resources.files("beadhive_tracker_github")

    assert (package / "py.typed").is_file()
    assert (package / "plugin.json").is_file()


def test_the_factory_builds_the_read_path_adapter_without_resolving_a_credential() -> None:
    calls: list[str] = []

    def tokens():
        calls.append("token")
        return None

    adapter = factory.build(
        {}, tokens, project=ProjectRef("github", "acme/widget"), sleep=lambda _seconds: None
    )

    assert isinstance(adapter, TrackerAdapter) and isinstance(adapter, IssueResolver)
    assert adapter.describe().project == ProjectRef("github", "acme/widget")
    assert calls == []  # building resolves nothing and sends nothing


def test_a_refused_build_echoes_no_input() -> None:
    secret = "ghp_" + "A" * 36

    for settings, project in (
        ({"base_url": f"https://{secret}@example.test"}, ProjectRef("github", "acme/widget")),
        ({"read": {"page_size": secret}}, ProjectRef("github", "acme/widget")),
        ({}, ProjectRef("github", f"{secret}/x/y")),
        ({}, ProjectRef("jira", secret)),
    ):
        with pytest.raises((ValueError, TypeError)) as caught:
            factory.build(settings, lambda: None, project=project, sleep=lambda _seconds: None)
        assert secret not in str(caught.value)
