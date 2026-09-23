from __future__ import annotations

import json
from importlib import resources

from beadhive.kernel.plugins.contracts import BuildVerifier
from beadhive.modules.work.contracts.impact import ImpactBackend
from beadhive_pants.impact import PantsImpactBackend
from beadhive_pants.verify import PantsBuildVerifier


def test_packaged_plugin_manifest_declares_build_impact() -> None:
    manifest = json.loads(
        (resources.files("beadhive_pants") / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["plugin_id"] == "pants"
    assert manifest["capabilities"]["provides"] == [
        {"api_version": 1, "id": "build.impact"},
        {"api_version": 1, "id": "build.verify"},
    ]


def test_backend_implements_the_public_impact_contract(tmp_path) -> None:
    (tmp_path / "pants.toml").write_text('[GLOBAL]\npants_version = "2.32.1"\n')

    backend = PantsImpactBackend(tmp_path)

    assert isinstance(backend, ImpactBackend)
    assert backend.name == "pants"
    assert backend.version == "2.32.1"


def test_verifier_implements_the_public_build_verify_contract() -> None:
    assert isinstance(PantsBuildVerifier(), BuildVerifier)


def test_proven_manifest_explicitly_lists_every_package_test() -> None:
    manifest = json.loads(
        (resources.files("beadhive_pants.data") / "proven_tests.json").read_text(encoding="utf-8")
    )
    package_tests = {
        path for path in manifest["tests"] if path.startswith("packages/beadhive-pants/tests/")
    }

    assert package_tests == {
        "packages/beadhive-pants/tests/test_impact_conformance.py",
        "packages/beadhive-pants/tests/test_package.py",
    }
