"""The package: manifest, resources, the factory and the beadlog.store contract it implements."""

from __future__ import annotations

import json
from importlib import resources

import pytest

import beadhive_beadlog_xtdb
from beadhive.modules.beadlog_store.contracts import (
    BEADLOG_STORE_API_VERSION,
    BEADLOG_STORE_CAPABILITY_ID,
    BeadlogStore,
)
from beadhive_beadlog_xtdb import factory
from xtdb_plugin_fakes import SECRET


def _manifest() -> dict:
    return json.loads(
        (resources.files("beadhive_beadlog_xtdb") / "plugin.json").read_text(encoding="utf-8")
    )


def test_packaged_manifest_declares_the_beadlog_store_capability() -> None:
    manifest = _manifest()

    assert manifest["plugin_id"] == beadhive_beadlog_xtdb.PLUGIN_ID == "beadlog-xtdb"
    assert manifest["capabilities"]["provides"] == [
        {"api_version": BEADLOG_STORE_API_VERSION, "id": BEADLOG_STORE_CAPABILITY_ID}
    ]
    assert manifest["configuration"]["namespace"] == "plugins.beadlog-xtdb"
    assert manifest["configuration"]["schema_artifact"] == (
        "urn:beadhive:wire-schema:plugin-config:beadlog-xtdb:1"
    )
    assert {row["command"] for row in manifest["presentation"]["cli"]} == {
        "plugin beadlog-xtdb doctor",
        "plugin beadlog-xtdb status",
    }
    assert manifest["lifecycle"]["subscriptions"] == []
    assert manifest["security"]["executables"] == []  # bh never starts or stops the node
    assert all(not item["required"] for item in manifest["security"]["credentials"])


def test_typed_marker_and_manifest_ship_as_resources() -> None:
    package = resources.files("beadhive_beadlog_xtdb")

    assert (package / "py.typed").is_file()
    assert (package / "plugin.json").is_file()


def test_the_factory_builds_a_store_without_connecting_or_resolving_the_password() -> None:
    calls: list[str] = []

    def password() -> str:
        calls.append("password")
        return SECRET

    store = factory.build({"enabled": True, "dsn": "host=127.0.0.1 port=15470"}, password)

    assert isinstance(store, BeadlogStore)
    assert store.store == beadhive_beadlog_xtdb.STORE_NAME == "xtdb"
    assert store.target == "xtdb://xtdb@127.0.0.1:15470/xtdb"
    assert calls == []
    store.close()  # idempotent with nothing open
    store.close()


@pytest.mark.parametrize(
    ("settings", "source"),
    [
        ({"dsn": 7}, lambda: None),
        ("not a mapping", lambda: None),
        ({}, "not callable"),
    ],
)
def test_a_refused_build_raises_a_static_type_error(settings, source) -> None:
    with pytest.raises(TypeError) as caught:
        factory.build(settings, source)  # type: ignore[arg-type]
    assert SECRET not in str(caught.value)
