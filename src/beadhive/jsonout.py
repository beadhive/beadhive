"""The `--json` envelope: one schema-version convention for every machine-readable payload.

WHY THIS MODULE EXISTS AT ALL (bh-0olv9.2). `bh` already had a `--json` convention and it is
already written down — `docs/design/cli-mcp-naming-conventions-adr.md` convention 3: a boolean
``--json`` (never ``--format json``), bound to the canonical parameter name ``as_json``, with the
data assembled by a pure ``*_payload()`` builder that the HUMAN rendering also consumes
(`hive.status_payload`, `doctor.doctor_payload`, `toolchain.list_payload`, `host_cli.list_payload`
are all this shape). That convention is matched here verbatim and this module does not restate it.

What was NOT established anywhere is a **versioned envelope**: every existing bh payload is bare,
so a consumer has no way to tell schema v1 from schema v2 except by guessing from the keys. The
moment a bundled Guide parses one of these — which is exactly what `assets/guides/setup/` does —
the shape becomes a contract, and adding a version to a contract after the fact is the breaking
change. So this is a deliberate, recorded divergence from the bare-payload precedent, and it is
kept as small as possible:

* the key is ``schema_version``, an integer, at the TOP LEVEL, merged flat into the payload —
  which is not invented here either: it is the shape `bd`'s own ``--json`` output already uses
  (see `engine.py`'s recorded observations of ``bd dolt status --json`` /
  ``bd federation status --json``, both ``{…, "schema_version": 1}``), and bd is the tool bh
  wraps. Copying it means an agent reading a bh payload and a bd payload applies one rule.
* ``command`` names WHICH contract the version refers to. ``schema_version`` is per-command —
  ``setup check``'s v1 and ``doctor``'s v1 are unrelated — so a bare integer with no subject is
  ambiguous the moment a second command adopts it.

Bumping a version is a per-command decision: add a field → same version (consumers ignore unknown
keys); remove/retype/re-mean a field → bump. There is intentionally no global registry of
versions, because a single number shared across commands forces an unrelated bump on every
consumer.
"""

from __future__ import annotations

import json
from typing import Any

import typer

#: Version of the `bh setup check --json` contract. See `setup.check_payload`.
SETUP_CHECK_SCHEMA = 1

#: Version of the `bh doctor --json` contract (and of the ``beadhive://doctor`` MCP resource,
#: which is the same object — see `doctor.doctor_payload`).
DOCTOR_SCHEMA = 1


def envelope(command: str, schema_version: int, payload: dict[str, Any]) -> dict[str, Any]:
    """*payload* with the version envelope merged in FRONT of it.

    Flat rather than nested (``{"schema_version": …, "data": {…}}``) on purpose: nesting would
    re-shape every existing bare payload for consumers that already read them — `doctor`'s
    payload is a live MCP resource — whereas two additional top-level keys are additive. The
    envelope leads so it is the first thing visible in a truncated read; ``**payload`` after it
    means a payload key of the same name would win, which is why neither name is reused below.
    """
    return {"schema_version": schema_version, "command": command, **payload}


def emit(payload: dict[str, Any]) -> None:
    """Write *payload* to stdout as the sole thing on it.

    ``indent=2`` + ``default=str`` matches every other ``--json`` emitter in bh
    (`hive.status`, `toolchain.list_`, `backup usage`), so machine output is one format and not
    one-per-command. Callers must not echo anything else on stdout in ``--json`` mode: a
    progress line interleaved with the document is exactly the un-parseable output the flag
    exists to replace.
    """
    typer.echo(json.dumps(payload, indent=2, default=str))
