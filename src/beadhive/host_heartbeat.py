"""Compatible heartbeat API with the real protected HQ authority composition."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import host_heartbeat_core as _implementation
from . import hosts
from .host_heartbeat_core import (
    AuthoritySnapshot as AuthoritySnapshot,
)
from .host_heartbeat_core import (
    ConformanceCheck as ConformanceCheck,
)
from .host_heartbeat_core import (
    HeartbeatConformance as HeartbeatConformance,
)
from .host_heartbeat_core import (
    HeartbeatError as HeartbeatError,
)
from .host_heartbeat_core import (
    HeartbeatLease as HeartbeatLease,
)
from .host_heartbeat_core import (
    ObservationAuthority as ObservationAuthority,
)
from .host_heartbeat_core import (
    VerifiedObservation as VerifiedObservation,
)
from .host_heartbeat_core import (
    _authority_matches as _authority_matches,
)
from .host_heartbeat_core import (
    _binding as _binding,
)
from .host_heartbeat_core import (
    _diagnostic as _diagnostic,
)
from .host_heartbeat_core import (
    _fingerprint as _fingerprint,
)
from .host_heartbeat_core import (
    _git as _git,
)
from .host_heartbeat_core import (
    _observation_store as _observation_store,
)
from .host_heartbeat_core import (
    _required as _required,
)
from .host_heartbeat_core import (
    _trust_path as _trust_path,
)
from .host_heartbeat_core import (
    framelease_envelope as framelease_envelope,
)
from .host_heartbeat_core import (
    load_authority as load_authority,
)
from .host_heartbeat_core import (
    ref_name as ref_name,
)
from .host_heartbeat_core import (
    verify_framelease_envelope as verify_framelease_envelope,
)
from .hq_control_plane import ControlPlaneError, control_plane


def __getattr__(name):
    return getattr(_implementation, name)


def load_trusted_authority(hq_dir: Path, identity: str) -> AuthoritySnapshot | None:
    """Read protected remote authority and durable observer state atomically."""
    try:
        return control_plane(hq_dir).watch_state(identity)
    except ControlPlaneError:
        return None


def publish(
    hq_dir: Path,
    lease: HeartbeatLease,
    *,
    signing_key: str,
    remote: str = "origin",
    reference: str | None = None,
    transport_options: list[str] | None = None,
    now: float | None = None,
    trusted_authority_lookup: Callable[[Path, str], AuthoritySnapshot | None] | None = None,
) -> str:
    return _implementation.publish(
        hq_dir,
        lease,
        signing_key=signing_key,
        remote=remote,
        reference=reference,
        transport_options=transport_options,
        now=now,
        trusted_authority_lookup=trusted_authority_lookup,
        _default_authority_lookup=load_trusted_authority,
    )


def observe(
    hq_dir: Path,
    manifest: hosts.HostManifest,
    *,
    now: float | None = None,
    observer_dir: Path | None = None,
    authority_lookup: Callable[[Path, str], ObservationAuthority | None] = load_authority,
    remote: str = "origin",
    trusted_authority_lookup: Callable[[Path, str], AuthoritySnapshot | None] | None = None,
) -> VerifiedObservation:
    return _implementation.observe(
        hq_dir,
        manifest,
        now=now,
        observer_dir=observer_dir,
        authority_lookup=authority_lookup,
        remote=remote,
        trusted_authority_lookup=trusted_authority_lookup,
        _default_authority_lookup=load_trusted_authority,
    )
