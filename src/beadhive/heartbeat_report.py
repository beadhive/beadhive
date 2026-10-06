"""Measured SQL frame reports. Generation is read-only; sending is explicit.

Never use a caller-supplied release, epoch, profile, or sequence. These belong to
protected operator authority, not the runtime sender.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import typer
from ruamel.yaml import YAML

from . import config, heartbeat_conformance, host, hosts, registry
from .config_validate import validate_config
from .hq_framelease_contracts import DOMAIN_V2, HeartbeatError, HeartbeatLease
from .release_measurement import installed_release

_UNSET = object()


def config_valid() -> bool:
    config.load_host()  # Includes raw schema and HOST/FLEET ownership validation.
    effective = config.load()
    return not any(item["level"] == "error" for item in validate_config(effective))


def hive_ready(entry: dict) -> bool:
    """Measure readiness and database reachability without retaining command output."""
    from . import bd
    from .hive_ready import probe_readiness

    directory = registry.hive_dir(entry)
    if not directory.is_dir() or not probe_readiness(cwd=directory).ready:
        return False
    result = bd.routes(directory).database_ping(timeout=20)
    return result.returncode == 0 and json.loads(result.stdout).get("status") == "ok"


def _check(name, predicate) -> dict:
    try:
        passed = bool(predicate())
    except Exception:
        passed = False
    # Evidence contains fixed text only: errors and subprocess output can contain secrets.
    return {
        "id": name,
        "status": "pass" if passed else "fail",
        "evidence": "measured check passed" if passed else "measured check failed",
    }


def _load_config():
    try:
        return config.load()
    except Exception:
        return None


def measure_conformance(cfg=_UNSET) -> list[dict]:
    """The slow host-local conformance checks (:data:`heartbeat_conformance.CACHED_CHECK_IDS`).

    Reads no authority, so the conformance job can run it on its own timer.
    """
    config_check = _check("host-config-partition", config_valid)
    if cfg is _UNSET:
        cfg = _load_config()
    return [
        config_check,
        _check(
            "hives-ready",
            lambda: (
                cfg is not None
                and bool(registry.hives(cfg))
                and all(hive_ready(entry) for entry in registry.hives(cfg))
            ),
        ),
    ]


def refresh_conformance():
    """One conformance-job run: measure and atomically publish the local cache."""
    return heartbeat_conformance.refresh(measure_conformance)


def generate(
    plane, *, free_sessions: int = 0, cached: bool = False, now: float | None = None
) -> HeartbeatLease:
    """Read verified authority and report actual checks; never publish or admit.

    ``cached=True`` is the decoupled beat: conformance comes from the local cache written by
    the conformance job (:func:`refresh_conformance`) instead of being measured inline.
    ``now`` (epoch seconds) overrides the beat clock for tests.
    """
    if type(free_sessions) is not int or not 0 <= free_sessions <= 1024:
        raise HeartbeatError("free sessions must be between zero and 1024")
    beat_at = datetime.now(UTC).timestamp() if now is None else now
    try:
        # ``row`` is the receiver's accepted observation, or in ``hq.sql.liveness: signed``
        # mode the newest verified heartbeat in this frame's own inbox — so the next seq
        # follows the last beat actually sent and never stalls behind a lagging receiver.
        _, _, route, _, record, snapshot, _, row, _ = (
            plane._runtime_authority().read_frame_composite()
        )
        authority, desired = record["authority"], record["desired"]
        local_id = host.host_id()
        if local_id != route.holder_identity:
            raise HeartbeatError("local host identity differs from current grant")
        measured = installed_release()
        release = desired["release"]
        if measured["digest"] != release["digest"]:
            raise HeartbeatError(
                "installed release differs from reviewed grant; operator upgrade required"
            )
        documents = [item for item in snapshot.documents if item.path == f"hosts/{local_id}.yaml"]
        if len(documents) != 1:
            raise HeartbeatError("committed host manifest unavailable")
        manifest = hosts.HostManifest.model_validate(YAML(typ="safe").load(documents[0].content))
        if (
            manifest.host_id != local_id
            or manifest.frame_id != route.frame_id
            or manifest.instance_ref != route.instance_ref
            or manifest.beadyard_id != authority.get("beadyard_id")
            or not manifest.release
            or manifest.release.model_dump() != release
        ):
            raise HeartbeatError(
                "committed manifest differs from current grant or installed release"
            )
        if free_sessions and (
            manifest.capabilities is None or free_sessions > manifest.capabilities.max_sessions
        ):
            raise HeartbeatError("free sessions exceed committed frame capacity")
        cfg = _load_config()
        identity = _check(
            "host-identity",
            lambda: cfg is not None and cfg.get("host", {}).get("frame_id") == route.frame_id,
        )
        if cached:
            # Never measure here: sign the newest cached result with its measured_at, and fail
            # by bound once it is too old (bh-i6ggn). The beat cannot block on hive_ready.
            stored = heartbeat_conformance.beat_checks(
                heartbeat_conformance.read(),
                now=beat_at,
            )
            checks = [
                _check("installed-release", lambda: True),
                stored["host-config-partition"],
                identity,
                stored["hives-ready"],
                stored[heartbeat_conformance.AGE_CHECK_ID],
            ]
        else:
            measured_checks = {item["id"]: item for item in measure_conformance(cfg)}
            checks = [
                _check("installed-release", lambda: True),
                measured_checks["host-config-partition"],
                identity,
                measured_checks["hives-ready"],
            ]
        conformant = all(item["status"] == "pass" for item in checks)
        report = {
            "method": "installed-package-v1",
            "checks": checks,
            "free_sessions": free_sessions if conformant else 0,
        }
        return HeartbeatLease.model_validate(
            {
                "domain": DOMAIN_V2,
                "audience": authority["audience"],
                "beadyard_id": authority.get("beadyard_id"),
                "frame_id": route.frame_id,
                "holderIdentity": local_id,
                "instance_ref": route.instance_ref,
                "key_id": route.signer_fingerprint,
                "epoch": route.epoch,
                "config_revision": authority["config_revision"],
                "seq": 1 if row is None else int(row[0]) + 1,
                "renewTime": datetime.fromtimestamp(beat_at, UTC).isoformat(),
                "leaseDurationSeconds": heartbeat_conformance.LEASE_DURATION_SECONDS,
                "intervalSeconds": heartbeat_conformance.INTERVAL_SECONDS,
                "release": release,
                "state_seen": record["state"],
                "conformance": {
                    "profile": desired["profile"],
                    "status": "conformant" if conformant else "non-conformant",
                    "checks": checks,
                },
                "free_sessions": report["free_sessions"],
                "report_digest": "sha256:"
                + hashlib.sha256(
                    json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )
    except HeartbeatError:
        raise
    except Exception:
        raise HeartbeatError(
            "measured heartbeat unavailable; verify local configuration and SQL authority"
        ) from None


def send(plane, *, free_sessions: int = 0, cached: bool = False) -> str:
    """Measure again immediately before explicit signed publication.

    With ``cached=True`` conformance is read from the local cache, never measured inline.
    """
    lease = generate(plane, free_sessions=free_sessions, cached=cached)
    key = host.signing_key()
    if not key:
        raise HeartbeatError("no recorded host signing key")
    return plane.heartbeat(lease, signing_key=key)


def bh_home():
    """The bh home the heartbeat sender keeps its local cache under."""
    return config.home()


def send_cached_beat(*, free_sessions: int = 0) -> str:
    """One decoupled beat against this host's SQL frame authority (``heartbeat_sender beat``)."""
    from .hq_control_plane import SqlControlPlane, control_plane

    plane = control_plane(config.hq_dir())
    if not isinstance(plane, SqlControlPlane):
        raise HeartbeatError("measured heartbeat requires SQL frame authority")
    return send(plane, free_sessions=free_sessions, cached=True)


def register(app: typer.Typer) -> None:
    def execute(free_sessions, publish):
        from .hq_control_plane import SqlControlPlane, control_plane

        try:
            plane = control_plane(config.hq_dir())
            if not isinstance(plane, SqlControlPlane):
                raise HeartbeatError("measured heartbeat requires SQL frame authority")
            if publish:
                typer.echo(json.dumps({"digest": send(plane, free_sessions=free_sessions)}))
            else:
                typer.echo(generate(plane, free_sessions=free_sessions).model_dump_json())
        except Exception:
            # Transport, signing and config exceptions may contain credential references.
            typer.echo(
                "measured heartbeat refused; verify release, grant, configuration and signer",
                err=True,
            )
            raise typer.Exit(1) from None

    @app.command("heartbeat-report", help="generate a measured JSON report without publishing")
    def report_cmd(free_sessions: int = typer.Option(0, min=0, max=1024)):
        execute(free_sessions, False)

    @app.command(
        "heartbeat-send", help="remeasure and explicitly publish one signed heartbeat; never admit"
    )
    def send_cmd(free_sessions: int = typer.Option(0, min=0, max=1024)):
        execute(free_sessions, True)
