"""Shared provenance vocabulary for daemon activity publication."""

WRITER_LOCAL_LOOP = "beadhive.local-loop"
WRITER_ROLE = "beadhive.role"
WRITER_HITCH = "agent-hitch.direct"
WRITER_BAML = "baml.provider"
WRITERS = frozenset({WRITER_LOCAL_LOOP, WRITER_ROLE, WRITER_HITCH, WRITER_BAML})

__all__ = (
    "WRITER_BAML",
    "WRITER_HITCH",
    "WRITER_LOCAL_LOOP",
    "WRITER_ROLE",
    "WRITERS",
)
