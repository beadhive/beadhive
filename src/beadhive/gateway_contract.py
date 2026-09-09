"""Deterministic catalog projection contract for the Development gateway.

The gateway continues to own authentication, CORS, admission, rate limits, streaming, runtime
calls, and its two existing wire families.  This checked document adds the catalog relationship
without turning the operation catalog into a runtime dispatcher or copying gateway policy into it.
"""

from __future__ import annotations

import argparse
import json
from importlib import resources
from pathlib import Path
from typing import Any

from .transport_inventory import catalog_projection_extension, projections

CONTRACT_ARTIFACT = "beadhive-gateway-projection-v1.json"
SCHEMA_ARTIFACT = "beadhive-gateway-projection-v1.schema.json"
CONTRACT_ID = "urn:beadhive:gateway-projection-contract:1"


def contract_path() -> Path:
    return Path(resources.files("beadhive").joinpath("schemas", CONTRACT_ARTIFACT))


def schema_path() -> Path:
    return Path(resources.files("beadhive").joinpath("schemas", SCHEMA_ARTIFACT))


def _operation(row) -> dict[str, Any]:
    method, path = row.identifier.split(" ", 1)
    catalog = catalog_projection_extension(row)
    return {
        "identifier": row.identifier,
        "method": method,
        "path": path,
        "classification": catalog["classification"],
        "shape": catalog["shape"],
        "canonicalContracts": catalog["canonicalContracts"],
        "wireRequestSchema": row.request_schema,
        "wireResultSchema": row.result_schema,
        "transportOwner": catalog["transportOwner"],
        "transportPrivilege": catalog["transportPrivilege"],
        "sideEffects": catalog["sideEffects"],
        "streaming": row.streaming,
        "availability": row.availability,
        "compatibility": row.compatibility,
        "reason": catalog["reason"],
    }


def generate_document() -> dict[str, Any]:
    """Generate from declarations only; never consume the artifact being checked."""

    rows = sorted(
        (row for row in projections() if row.surface == "gateway"),
        key=lambda row: row.identifier,
    )
    return {
        "$id": CONTRACT_ID,
        "formatVersion": 1,
        "contractVersion": "1.0.0",
        "profile": "Development",
        "policy": {
            "catalogRole": "operation identity, request/result contracts, privilege, side effects",
            "wireAuthority": "gateway.v1 and gateway.read.v1 remain gateway-owned",
            "runtimeAuthority": (
                "authentication, CORS, admission, streaming, rate limits, status, and runtime "
                "calls remain gateway-owned"
            ),
            "localCompatibility": "CLI and MCP stdio do not require the gateway or host daemon",
        },
        "operations": [_operation(row) for row in rows],
    }


def render_document(document: dict[str, Any] | None = None) -> str:
    return json.dumps(document or generate_document(), indent=2, ensure_ascii=False) + "\n"


def checked_document() -> dict[str, Any]:
    value = json.loads(contract_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("gateway projection contract root must be an object")
    return value


def check_artifact() -> bool:
    return render_document() == contract_path().read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail if the artifact drifted")
    mode.add_argument("--write", action="store_true", help="rewrite the checked artifact")
    args = parser.parse_args(argv)
    if args.check:
        if check_artifact():
            return 0
        print(f"Gateway projection drift: regenerate {contract_path()} with --write")
        return 1
    contract_path().write_text(render_document(), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a CI command
    raise SystemExit(main())
