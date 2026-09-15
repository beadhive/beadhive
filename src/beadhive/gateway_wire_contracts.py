"""Digested references to the gateway-owned wire-family declarations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from . import frame_bridge, gateway_read

WireFamily = Literal["gateway.v1", "gateway.read.v1", "gateway.experience.v1"]
_CONTRACT_ID_PREFIX = "urn:beadhive:gateway-wire-contract:"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _declarations() -> dict[WireFamily, dict[str, Any]]:
    return {
        "gateway.read.v1": {
            "contractVersion": gateway_read.CONTRACT_VERSION,
            "schemaVersion": gateway_read.SCHEMA_VERSION,
            "schemas": gateway_read.gateway_wire_schemas(),
        },
        "gateway.v1": {
            "contractVersion": frame_bridge.CONTRACT_VERSION,
            "schemaVersion": frame_bridge.SCHEMA_VERSION,
            "schemas": frame_bridge.gateway_wire_schemas(),
        },
        "gateway.experience.v1": {
            "contractVersion": "gateway.experience.v1",
            "schemaVersion": gateway_read.SCHEMA_VERSION,
            "schemas": gateway_read.experience_wire_schemas(),
        },
    }


def documents() -> tuple[dict[str, Any], ...]:
    """Return deterministic, content-addressed wire contracts from gateway authorities."""

    rendered = []
    for declaration in _declarations().values():
        digest = f"sha256:{hashlib.sha256(_canonical_bytes(declaration)).hexdigest()}"
        contract_id = (
            f"{_CONTRACT_ID_PREFIX}{declaration['contractVersion']}:"
            f"{declaration['schemaVersion']}:{digest}"
        )
        rendered.append({"$id": contract_id, "digest": digest, **declaration})
    return tuple(rendered)


def schema_ref(family: WireFamily, schema: str) -> str:
    """Return a resolvable reference pinned to the current owned contract digest."""

    declaration = _declarations()[family]
    if schema not in declaration["schemas"]:
        raise KeyError(f"unknown {family} wire schema: {schema}")
    digest = f"sha256:{hashlib.sha256(_canonical_bytes(declaration)).hexdigest()}"
    contract_id = (
        f"{_CONTRACT_ID_PREFIX}{declaration['contractVersion']}:"
        f"{declaration['schemaVersion']}:{digest}"
    )
    return f"{contract_id}#/schemas/{schema}"
