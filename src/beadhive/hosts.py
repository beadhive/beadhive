"""``hosts/<host_id>.yaml`` — the fleet's roster in Factory HQ (bh-ytbb.3).

The manifest side of the multi-host model (``docs/design/multi-host-model-adr.md``,
Amendment 1 §3): one file per host, in :func:`beadhive.hq.scaffold_layout`'s ``hosts/``
directory, keyed by the SAME ``host_id`` :mod:`beadhive.host` mints locally
(``~/.beadhive/host.yaml``'s ``host_id()``) — NOT that file itself, which is host-local and
never synced (see its module docstring). This module reuses that accessor rather than
re-deriving host identity; it never mints or reads ``host.yaml`` directly.

Carries the ``role`` that makes asymmetric TTL renewal answerable (a later bead, ``bh-ytbb.6``
and on, reads it to pick renew/TTL defaults — ``executor`` machines get long tenure,
``transient`` machines get short explicit adoptions, ``viewer`` never becomes primary),
and the ``identity`` mechanism a host's clones use to resolve remote URLs (ssh alias /
``insteadOf`` rewrite / per-repo ``core.sshCommand``) — the fact ``bh-fry5``'s cross-host
identity-drift check wants to diff against instead of investigate by hand.

Schema + read/write/validate ONLY — no ``bh host`` CLI (that's ``bh-ytbb.5``, which consumes
:func:`load`/:func:`save`/:func:`remove`) and no lease/epoch logic (``bh-ytbb.6`` and on).
``capacity`` and ``harnesses`` are deliberately open (free-form dict) placeholders: the plan
doc previews a future ``harness:`` block and a capacity/budget shape, but neither is filed as
a concrete bead in this molecule yet — a later bead can flesh either out without a schema
rewrite here.

Validation follows the same pydantic convention as :mod:`beadhive.config_schema`
(``extra="forbid"`` at every level, closed ``Literal`` sets): :func:`load` raises
:class:`ManifestError` naming the offending key(s) on a malformed manifest — "fails loudly",
never a silent partial read.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML

from .host_manifest_contracts import (
    DEPRECATED_ROLE_ALIASES as DEPRECATED_ROLE_ALIASES,
)
from .host_manifest_contracts import (
    FRAME_ISOLATIONS as FRAME_ISOLATIONS,
)
from .host_manifest_contracts import (
    FRAME_STATES as FRAME_STATES,
)
from .host_manifest_contracts import (
    FRAME_TRUST_ZONES as FRAME_TRUST_ZONES,
)
from .host_manifest_contracts import (
    HOST_ROLES as HOST_ROLES,
)
from .host_manifest_contracts import (
    IDENTITY_MECHANISM_KINDS as IDENTITY_MECHANISM_KINDS,
)
from .host_manifest_contracts import (
    FrameCapabilities as FrameCapabilities,
)
from .host_manifest_contracts import (
    FrameRelease as FrameRelease,
)
from .host_manifest_contracts import (
    HostManifest as HostManifest,
)
from .host_manifest_contracts import (
    IdentityMechanism as IdentityMechanism,
)
from .host_manifest_contracts import (
    canonical_role as canonical_role,
)

# Same round-trip settings as host.py/config.py's writers. No comment/flow-style preservation
# needed here — unlike config.yaml, this file is written wholesale by `save()`, never
# hand-edited-and-merged.
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)


class ManifestError(ValueError):
    """A ``hosts/<host_id>.yaml`` manifest failed schema validation on read — the message
    names the offending key(s), per this bead's "fails loudly" acceptance bar."""


def hosts_dir(hq_dir: Path) -> Path:
    """The ``hosts/`` directory under a given HQ store root. Purely a path computation — does
    NOT create it; :func:`beadhive.hq.scaffold_layout` is what creates it (+ its README)."""
    return hq_dir / "hosts"


def manifest_path(hq_dir: Path, host_id: str) -> Path:
    """Where one host's manifest lives under a given HQ store root."""
    return hosts_dir(hq_dir) / f"{host_id}.yaml"


def save(hq_dir: Path, manifest: HostManifest) -> Path:
    """Write ``manifest`` to ``hosts/<host_id>.yaml`` under ``hq_dir``, creating the ``hosts/``
    directory if needed. ``manifest`` is already-validated — a :class:`HostManifest` instance
    cannot exist in an invalid shape — so this never writes something :func:`load` would then
    reject."""
    from .hq_document_validation import DocumentValidationError, validate_document

    p = manifest_path(hq_dir, manifest.host_id)
    stream = StringIO()
    try:
        _yaml.dump(manifest.model_dump(mode="json", warnings=False), stream)
    except Exception:
        raise ManifestError("host manifest candidate serialization invalid") from None
    try:
        validate_document(f"hosts/{manifest.host_id}.yaml", stream.getvalue())
    except DocumentValidationError as exc:
        raise ManifestError(str(exc)) from None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(stream.getvalue())
    return p


def remove(hq_dir: Path, host_id: str) -> Path:
    """Delete ``hosts/<host_id>.yaml`` from ``hq_dir`` — the manifest-removal half of
    ``bh host rm`` (bh-salu: a rebuilt/wiped host mints a NEW ``host_id``, so its old
    manifest never goes away on its own — see :mod:`beadhive.host`'s module docstring).

    Raises ``FileNotFoundError`` when no manifest exists for ``host_id`` — mirrors
    :func:`load`'s own contract rather than silently no-op'ing on an already-gone entry. Every
    GATE (live leases, self-removal, staleness) is the CLI layer's job
    (:mod:`beadhive.host_cli`) — this is schema-agnostic file removal only, same split as
    :func:`save`/:func:`load`."""
    p = manifest_path(hq_dir, host_id)
    if not p.exists():
        raise FileNotFoundError(f"no host manifest for {host_id!r} at {p}")
    p.unlink()
    return p


def load(hq_dir: Path, host_id: str) -> HostManifest:
    """Read + VALIDATE ``hosts/<host_id>.yaml``.

    Raises ``FileNotFoundError`` when no manifest exists yet for ``host_id``. Raises
    :class:`ManifestError` — naming the offending key(s) — when the file exists but fails
    schema validation (unknown key, wrong type, a ``role``/identity ``kind`` outside its
    closed set, ...): never a silent partial/best-effort read.
    """
    p = manifest_path(hq_dir, host_id)
    if not p.exists():
        raise FileNotFoundError(f"no host manifest for {host_id!r} at {p}")
    raw = _yaml.load(p.read_text()) or {}
    if isinstance(raw, dict) and "state" not in raw:
        raw = {**raw, "state": "active"}
    try:
        manifest = HostManifest.model_validate(raw)
        if manifest.host_id != host_id:
            raise ManifestError("host manifest path identity mismatch")
        return manifest
    except ValidationError as exc:
        raise ManifestError(_format_error(p, exc)) from exc


def _format_error(path: Path, exc: ValidationError) -> str:
    """Render a pydantic ``ValidationError`` as a loud, specific message naming each offending
    dotted key — the same ``loc``-joining convention :mod:`beadhive.config_validate` uses."""
    lines = [f"malformed host manifest at {path}:"]
    for err in exc.errors():
        dotted = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  `{dotted}`: {err['msg']}")
    return "\n".join(lines)
