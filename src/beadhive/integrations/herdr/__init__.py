"""Provider-facing Herdr transport and live topology contracts.

The package intentionally contains no Typer or Beadhive lifecycle imports.
Callers can use the typed client and parsers in isolation, while the legacy
``beadhive.herdr_plugin`` module remains the presentation/composition adapter.
"""

from .identity import (
    IdentityExpectation,
    IdentityProof,
    OwnershipMarker,
    SessionSelection,
    collect_tokens,
    resolve_session,
    validate_generation,
    validate_identity,
    validate_live_identity,
)
from .topology import (
    AgentRecord,
    Coverage,
    TopologySnapshot,
    agent_records,
    join_snapshot_records,
    parse_roster,
    parse_snapshot,
)
from .transport import HerdrClient, HerdrTransport, decode_legacy, decode_protocol, decode_response
from .transport_types import FailureCode, HerdrFailure, HerdrResult

__all__ = [
    "AgentRecord",
    "Coverage",
    "FailureCode",
    "HerdrClient",
    "HerdrTransport",
    "HerdrFailure",
    "HerdrResult",
    "IdentityExpectation",
    "IdentityProof",
    "OwnershipMarker",
    "SessionSelection",
    "TopologySnapshot",
    "agent_records",
    "join_snapshot_records",
    "decode_protocol",
    "decode_response",
    "decode_legacy",
    "collect_tokens",
    "parse_roster",
    "parse_snapshot",
    "resolve_session",
    "validate_identity",
    "validate_generation",
    "validate_live_identity",
]
