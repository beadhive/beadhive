"""Immutable Development gateway handoff guardrails."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
HANDOFF = ROOT / "docs/proof/development-gateway-v1-handoff.json"


def test_gateway_handoff_is_immutable_exact_and_infra_targeted() -> None:
    record = json.loads(HANDOFF.read_text(encoding="utf-8"))
    assert record["schemaVersion"] == record["conformanceVersion"] == 1
    assert record["handoff"] == "bh-infra-lum.3"
    assert record["contractVersion"] == "gateway.v1"
    assert record["instanceId"] == "dev/demo"
    candidate = record["candidate"]
    assert re.fullmatch(r"[0-9a-f]{40}", candidate["commit"])
    assert re.fullmatch(r"[0-9a-f]{40}", candidate["tree"])
    assert re.fullmatch(r"[0-9a-f]{64}", candidate["sha256"])
    assert candidate["artifact"] == "beadhive-0.15.1-py3-none-any.whl"
    assert candidate["bytes"] == 1_372_257
    assert record["conformance"]["failed"] == 0
    assert record["scans"] == {
        "encryptedDevelopmentValuesChecked": 3,
        "encryptedDevelopmentValueMatches": 0,
        "forbiddenStructuralMatches": 0,
        "externalMutations": 0,
    }


def test_gateway_handoff_contains_no_mutable_or_deferred_environment_reference() -> None:
    text = HANDOFF.read_text(encoding="utf-8")
    forbidden = (
        "refs/heads/",
        '"branch"',
        "app.prod.",
        "gateway.prod.",
        "beadhive-gateway-prod",
        "prod/demo",
        "gateway.beadhive.cloud",
        "app.beadhive.ai",
    )
    assert not [value for value in forbidden if value in text]
