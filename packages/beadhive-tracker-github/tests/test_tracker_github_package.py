"""The package skeleton: packaged manifest, resources and the (not yet implemented) factory."""

from __future__ import annotations

import json
from importlib import resources

import pytest

import beadhive_tracker_github
from beadhive.modules.tracker.contracts import ProjectRef
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


def test_the_skeleton_factory_refuses_to_build_and_echoes_no_input() -> None:
    secret = "ghp_" + "A" * 36

    with pytest.raises(factory.TrackerAdapterNotImplemented) as caught:
        factory.build(
            {"base_url": f"https://{secret}@example.test"},
            lambda: None,
            project=ProjectRef("github", "acme/widget"),
            sleep=lambda _seconds: None,
        )

    assert secret not in str(caught.value)
