"""Measured SQL frame reports. Generation is read-only; sending is explicit.

Never use a caller-supplied release, epoch, profile, or sequence. These belong to
protected operator authority, not the runtime sender.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime

import typer
from ruamel.yaml import YAML

from . import config, heartbeat_conformance, host, hosts, registry
from .config_validate import validate_config
from .hq_framelease_contracts import DOMAIN_V2, HeartbeatError, HeartbeatLease
from .release_measurement import installed_release
from .release_policy import admits

_UNSET = object()


def config_valid() -> bool:
    config.load_host()  # Includes raw schema and HOST/FLEET ownership validation.
    effective = config.load()
    return not any(item["level"] == "error" for item in validate_config(effective))


#: Local, never-published failure log (relative to :func:`bh_home`). Holds hive prefix, sub-check
#: and a fixed reason code only -- never command output, paths or credentials.
FAILURE_LOG_RELATIVE_PATH = "logs/conformance-failures.log"
RETRIES_ENV = "BH_CONFORMANCE_RETRIES"
RETRY_DELAY_ENV = "BH_CONFORMANCE_RETRY_DELAY"
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY = 30.0


def _env_number(name: str, default, cast):
    try:
        value = cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _log_failure(entry: dict, subcheck: str, reason: str, *, clock=time.time) -> None:
    """Append one local line naming the failing hive and sub-check. Best effort, never raises."""
    try:
        path = bh_home() / FAILURE_LOG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(clock(), UTC).isoformat(timespec="seconds")
        prefix = str(entry.get("prefix", "?")).replace("\n", " ")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} hives-ready hive={prefix} subcheck={subcheck} reason={reason}\n")
    except Exception:  # noqa: S110 - diagnostics must never break measurement
        pass


def hive_ready(entry: dict) -> bool:
    """Measure readiness and database reachability without retaining command output.

    A failure is recorded in the local failure log (hive + sub-check only), not in evidence."""
    from . import bd
    from .hive_ready import probe_readiness

    directory = registry.hive_dir(entry)
    if not directory.is_dir():
        _log_failure(entry, "hive_dir", "missing")
        return False
    if not probe_readiness(cwd=directory).ready:
        _log_failure(entry, "probe_readiness", "not_ready")
        return False
    try:
        result = bd.routes(directory).database_ping(timeout=20)
    except Exception as exc:
        _log_failure(entry, "database_ping", type(exc).__name__)
        return False
    if result.returncode != 0:
        _log_failure(entry, "database_ping", f"exit_{result.returncode}")
        return False
    try:
        ok = json.loads(result.stdout).get("status") == "ok"
    except Exception:
        _log_failure(entry, "database_ping", "unparseable")
        return False
    if not ok:
        _log_failure(entry, "database_ping", "status_not_ok")
    return ok


def hive_ready_settled(entry: dict, *, sleep=time.sleep) -> bool:
    """:func:`hive_ready` with bounded remeasure, so one post-wake blip is not published.

    ``BH_CONFORMANCE_RETRIES`` extra attempts (default 2), ``BH_CONFORMANCE_RETRY_DELAY``
    seconds apart (default 30). ``sleep`` is injectable for tests."""
    retries = _env_number(RETRIES_ENV, DEFAULT_RETRIES, int)
    delay = _env_number(RETRY_DELAY_ENV, DEFAULT_RETRY_DELAY, float)
    for attempt in range(retries + 1):
        if hive_ready(entry):
            return True
        if attempt < retries:
            sleep(delay)
    return False


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


def _local_caps() -> dict | None:
    """This frame's committed capabilities from the local manifest, or ``None`` when unreadable."""
    try:
        manifest = hosts.load(config.hq_dir(), host.host_id())
        return manifest.capabilities.model_dump() if manifest.capabilities else None
    except Exception:
        return None


def _excluded_here(entry: dict, caps: dict | None) -> bool:
    """Whether the hive's ``frame_policy.requires`` rules this frame out (same matcher as leasing).

    Unknown capabilities or a hive without a policy never exclude: the hive stays required.
    """
    policy = entry.get("frame_policy")
    if caps is None or not isinstance(policy, dict):
        return False
    from .hq_authority_guard import requirements

    try:
        requirements(policy.get("requires", {}), caps)
    except ValueError:
        return True
    return False


def _local_only_elsewhere(entry: dict, host_id: str | None) -> bool:
    """Whether the hive is local-only and owned by ANOTHER host (bh-sj2bf R4).

    Owned by this host, or no marker: required as before. A host with no readable host_id
    cannot prove it is not the owner, so the hive stays required (fail closed)."""
    marker = registry.local_only_of(entry)
    if marker is None or not host_id:
        return False
    return marker["owner_host"] != host_id


#: hives-ready evidence cap (``ConformanceCheck.evidence`` max_length).
EVIDENCE_CAP = 1024


def _skip_evidence(groups: list[tuple[str, list[str]]], cap: int = EVIDENCE_CAP) -> str:
    """``measured check passed; skipped (<why>): a, b; ...`` naming prefixes only — never host
    ids or labels. Over ``cap``, the longest prefix list is truncated and ends ``, … (+N)``."""
    shown = [list(names) for _, names in groups]

    def render() -> str:
        parts = ["measured check passed"]
        for (why, names), keep in zip(groups, shown, strict=True):
            if not names:
                continue
            text = ", ".join(keep)
            if len(keep) < len(names):
                text += f", … (+{len(names) - len(keep)})"
            parts.append(f"skipped ({why}): {text}")
        return "; ".join(parts)

    text = render()
    while len(text) > cap:
        longest = max(range(len(shown)), key=lambda i: len(shown[i]))
        if len(shown[longest]) <= 1:
            return text[:cap]
        shown[longest].pop()
        text = render()
    return text


def measure_conformance(cfg=_UNSET, caps=_UNSET, host_id=_UNSET) -> list[dict]:
    """The slow host-local conformance checks (:data:`heartbeat_conformance.CACHED_CHECK_IDS`).

    Reads no authority, so the conformance job can run it on its own timer. Hives whose
    ``frame_policy`` excludes this frame (by ``caps``, default the local manifest), and
    local-only hives owned by another host (by ``host_id``, default this host's), are skipped
    and named in the ``hives-ready`` evidence rather than required ready. A hive that is both
    is named once, under local-only.
    """
    config_check = _check("host-config-partition", config_valid)
    if cfg is _UNSET:
        cfg = _load_config()
    if caps is _UNSET:
        caps = _local_caps()
    if host_id is _UNSET:
        host_id = host.host_id_or_none()
    hives = registry.hives(cfg) if cfg is not None else []
    local_only = [e for e in hives if _local_only_elsewhere(e, host_id)]
    skipped = [e for e in hives if e not in local_only and _excluded_here(e, caps)]
    required = [e for e in hives if e not in skipped and e not in local_only]
    ready = _check(
        "hives-ready",
        lambda: cfg is not None and bool(hives) and all(hive_ready_settled(e) for e in required),
    )
    if (skipped or local_only) and ready["status"] == "pass":
        ready["evidence"] = _skip_evidence(
            [
                ("frame_policy excludes this frame", [str(e.get("prefix", "?")) for e in skipped]),
                ("local-only on another host", [str(e.get("prefix", "?")) for e in local_only]),
            ]
        )
    return [config_check, ready]


def refresh_conformance():
    """One conformance-job run: measure and atomically publish the local cache."""
    return heartbeat_conformance.refresh(measure_conformance, path=conformance_cache_path())


class CategorisedHeartbeatError(HeartbeatError):
    """A refusal that names a redacted cause category and never the underlying detail."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


REFUSAL_CATEGORIES = ("release", "grant", "configuration", "signer", "transport")


def refusal_category(exc: BaseException) -> str:
    """The redacted cause category of a beat failure: one of :data:`REFUSAL_CATEGORIES`.

    Only the exception class and fixed bh-authored messages are inspected; the result is a
    fixed word, so no credential, host or account name can leak through it.
    """
    from .hq_control_plane import ControlPlaneError
    from .hq_sql_runtime import SqlRuntimeError

    category = getattr(exc, "category", None)
    if category in REFUSAL_CATEGORIES:
        return category
    if isinstance(exc, ControlPlaneError):
        # Publication: the plane re-checks the lease against the granted incarnation.
        text = str(exc)
        return "grant" if "grant" in text or "incarnation" in text else "configuration"
    if isinstance(exc, HeartbeatError):
        text = str(exc)
        if "signing key" in text:
            return "signer"
        if "release" in text:
            return "release"
        if "grant" in text or "identity" in text:
            return "grant"
        return "configuration"
    from .hq_port import HqDeadline, HqOutcomeUnknown, HqRefused, HqUnavailable

    if isinstance(exc, HqRefused):
        # A definite hq-service verdict on this frame's grant or route (bh-65s71).
        return "grant"
    if isinstance(
        exc,
        SqlRuntimeError | OSError | TimeoutError | HqUnavailable | HqDeadline | HqOutcomeUnknown,
    ):
        return "transport"
    return "configuration"


def _manifest_release_ok(plane, head, manifest_release, measured, desired) -> bool:
    """The committed manifest's release: the grant's reference exactly (an exact pin), or under
    a ``release_policy`` the reference or a release that is itself admitted (ADR section 4)."""
    if manifest_release == desired["release"]:
        return True
    policy = desired.get("release_policy")
    if policy is None:
        return False
    row = plane._runtime_authority().read_release_catalog_row(
        head, policy["catalog"], manifest_release
    )
    return admits(manifest_release, desired, row)


REPORT_METHOD = "installed-package-v1"


def report_digest(report: dict) -> str:
    """The canonical ``sha256:`` digest of a conformance report ``{method, checks,
    free_sessions}``: what a beat signs as ``report_digest`` and the HQ service recomputes."""
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def lease_report(lease: HeartbeatLease) -> dict:
    """The exact report *lease* digested, rebuilt from the lease itself.

    :func:`generate` derives the beat's ``conformance.checks`` and ``free_sessions`` from this
    very report, so rebuilding it from them is lossless. The digest is re-checked: a lease
    whose report cannot be rebuilt byte-for-byte raises rather than posting a different
    report than the one the beat bound."""
    report = {
        "method": REPORT_METHOD,
        "checks": [check.model_dump() for check in lease.conformance.checks],
        "free_sessions": lease.free_sessions,
    }
    if report_digest(report) != lease.report_digest:
        raise HeartbeatError("beat report cannot be rebuilt from its lease")
    return report


def generate(
    plane,
    *,
    free_sessions: int = 0,
    cached: bool = False,
    now: float | None = None,
    require_inbox: bool = False,
) -> HeartbeatLease:
    """Read verified authority and report actual checks; never publish or admit.

    ``cached=True`` is the decoupled beat: conformance comes from the local cache written by
    the conformance job (:func:`refresh_conformance`) instead of being measured inline.
    ``now`` (epoch seconds) overrides the beat clock for tests. ``require_inbox`` raises
    :class:`~beadhive.hq_sql_runtime.SessionOnlyIncarnation` as soon as the frame route shows
    no signed inbox, before any release, grant, configuration or signer step can refuse first.
    """
    if type(free_sessions) is not int or not 0 <= free_sessions <= 1024:
        raise HeartbeatError("free sessions must be between zero and 1024")
    beat_at = datetime.now(UTC).timestamp() if now is None else now
    try:
        # ``row`` is the receiver's accepted observation, or in ``hq.sql.liveness: signed``
        # mode the newest verified heartbeat in this frame's own inbox — so the next seq
        # follows the last beat actually sent and never stalls behind a lagging receiver.
        head, _, route, _, record, snapshot, _, row, _ = (
            plane._runtime_authority().read_frame_composite()
        )
        if require_inbox:
            from .hq_sql_runtime import SessionOnlyIncarnation
            from .hq_sql_runtime_schema import routed_to_inbox

            if not routed_to_inbox(route.inbox_table, route.principal, route.epoch):
                raise SessionOnlyIncarnation("session-only incarnation has no signed inbox")
        authority, desired = record["authority"], record["desired"]
        local_id = host.host_id()
        if local_id != route.holder_identity:
            raise HeartbeatError("local host identity differs from current grant")
        measured = installed_release()
        release = desired["release"]
        policy = desired.get("release_policy")
        if policy is None:
            if measured["digest"] != release["digest"]:
                raise HeartbeatError(
                    "installed release differs from reviewed grant; operator upgrade required"
                )
        else:
            # Under a release range the grant admits a catalogued version set, not one digest:
            # refuse only when the measured release is outside it (catalog AS OF the verified head).
            catalog_row = plane._runtime_authority().read_release_catalog_row(
                head, policy["catalog"], measured
            )
            if not admits(measured, desired, catalog_row):
                raise HeartbeatError(
                    f"installed release {measured['id']} is outside the granted release policy "
                    f"{policy['spec']} or not an active catalogued release; "
                    "operator upgrade required"
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
            or not _manifest_release_ok(
                plane, head, manifest.release.model_dump(), measured, desired
            )
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
                heartbeat_conformance.read(conformance_cache_path()),
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
            measured_checks = {
                item["id"]: item
                for item in measure_conformance(
                    cfg,
                    manifest.capabilities.model_dump() if manifest.capabilities else None,
                    local_id,
                )
            }
            checks = [
                _check("installed-release", lambda: True),
                measured_checks["host-config-partition"],
                identity,
                measured_checks["hives-ready"],
            ]
        conformant = all(item["status"] == "pass" for item in checks)
        report = {
            "method": REPORT_METHOD,
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
                # An exact pin reports the grant's release (digest-equal to the measurement);
                # under a policy the beat carries the measured release itself.
                "release": measured if policy is not None else release,
                "state_seen": record["state"],
                "conformance": {
                    "profile": desired["profile"],
                    "status": "conformant" if conformant else "non-conformant",
                    "checks": checks,
                },
                "free_sessions": report["free_sessions"],
                "report_digest": report_digest(report),
            }
        )
    except HeartbeatError:
        raise
    except Exception as exc:
        from .hq_sql_runtime import SessionOnlyIncarnation

        if isinstance(exc, SessionOnlyIncarnation):
            raise
        raise CategorisedHeartbeatError(
            refusal_category(exc),
            "measured heartbeat unavailable; verify local configuration and SQL authority",
        ) from None


def session_only_route(plane) -> bool:
    """Whether the authenticated runtime principal's committed route has no signed inbox.

    Asked first, on one routing read alone, so a session-only incarnation is recognised
    before the composite authority read (whose receiver observation a session-only principal
    may not be granted), the release, grant and manifest checks, or the signer. ``False``
    when the route cannot be read: the full beat then names its own redacted refusal.
    """
    from .hq_sql_runtime_schema import routed_to_inbox

    try:
        route = plane._runtime_authority().load_frame_binding(expected_head=None)
        return not routed_to_inbox(route.inbox_table, route.principal, route.epoch)
    except Exception:
        return False


def send(plane, *, free_sessions: int = 0, cached: bool = False) -> str:
    """Measure again immediately before explicit signed publication.

    With ``cached=True`` (the decoupled beat) conformance is read from the local cache, never
    measured inline, and a session-only incarnation raises
    :class:`~beadhive.hq_sql_runtime.SessionOnlyIncarnation` before anything else can refuse.
    """
    if cached and session_only_route(plane):
        from .hq_sql_runtime import SessionOnlyIncarnation

        raise SessionOnlyIncarnation("session-only incarnation has no signed inbox")
    lease = generate(plane, free_sessions=free_sessions, cached=cached, require_inbox=cached)
    key = host.signing_key()
    if not key:
        raise HeartbeatError("no recorded host signing key")
    return plane.heartbeat(lease, signing_key=key)


def bh_home():
    """The bh home the heartbeat sender keeps its local cache under."""
    return config.home()


def conformance_cache_path():
    """The sender's local conformance cache under :func:`bh_home` (heartbeat_conformance I/O)."""
    return bh_home() / heartbeat_conformance.CACHE_RELATIVE_PATH


def sql_plane():
    """The SQL control plane when this host has a frame runtime, else ``None``."""
    return _sql_plane()


def _sql_plane():
    from .hq_control_plane import SqlControlPlane, control_plane, host_route

    if host_route() == "api":
        # An api host reads its frame grant through hq-service (bh-65s71): no SQL frame runtime.
        return None
    plane = control_plane(config.hq_dir())
    if not isinstance(plane, SqlControlPlane) or plane.settings.get("runtime") is None:
        return None
    return plane


def _session_opener(plane):
    """A per-call connection factory on the frame's own runtime principal (strict transport)."""
    import time

    from .hq_sql_transport import FnoxBroker, connect

    binding = plane.settings["runtime"]
    broker = plane.broker or FnoxBroker()

    def opener():
        return connect(binding, broker, deadline=time.monotonic() + binding["operation_timeout"])

    return opener, binding["database"]


def session_renewer(select_port=None):
    """The frame's :class:`~beadhive.hq_sql_session.SessionRenewer`, or ``None`` when this host
    has no SQL frame runtime (git HQ keeps its signed beat and has no session row).

    On an ``hq.route: api`` host every tick goes through the port (:func:`port_session`) and no
    SQL plane is read."""
    from .hq_sql_session import SessionRenewer

    if api_route():
        return _port_renewer(None, select_port)
    plane = _sql_plane()
    if session_via_port():
        return _port_renewer(plane, select_port)
    if plane is None:
        return None
    opener, database = _session_opener(plane)
    return SessionRenewer(opener, database=database)


def session_evidence(plane, route):
    """What the conformance job reports for `route`, from the cache it just refreshed.

    The decoupled beat's own projection (:func:`generate` with ``cached=True``) supplies the
    release, profile, status and report digest; when the frame cannot build one (release
    drift, unreadable grant) the row says ``unavailable`` with the installed release, so stale
    ``conformant`` evidence is overwritten rather than left to age out."""
    from .hq_sql_session import EvidenceReport

    try:
        lease = generate(plane, cached=True)
        release = lease.release.model_dump()
        return EvidenceReport(
            route.epoch,
            str(release["id"]),
            str(release["digest"]),
            lease.conformance.profile,
            lease.conformance.status,
            lease.report_digest,
        )
    except HeartbeatError:
        measured = installed_release()
        return EvidenceReport(
            route.epoch, str(measured["id"]), str(measured["digest"]), "", "unavailable", ""
        )


def publish_session_evidence(select_port=None, *, renew_first: bool = False):
    """One evidence-row UPDATE after a conformance run, when the data switched this frame on.

    Returns the route written, or ``None`` when there is no SQL runtime; raises
    :class:`~beadhive.hq_sql_session.NotSwitched` on a legacy incarnation.

    ``renew_first`` is the sender's port-path ordering (bh-a3369, see
    :func:`_publish_evidence_via_port`); it has no effect on the direct SQL path (route ``sql``
    with ``hq.session_via_port`` off), which is unchanged. An ``hq.route: api`` host always
    posts through the port and reads no SQL plane."""
    if api_route():
        return _publish_evidence_via_port(None, select_port, renew_first=renew_first)
    plane = _sql_plane()
    if session_via_port():
        return _publish_evidence_via_port(plane, select_port, renew_first=renew_first)
    if plane is None:
        return None
    return _direct_evidence(plane)


def _direct_evidence(plane):
    """The direct SQL evidence-row UPDATE (resolve the route, then write the report)."""
    from .hq_sql_session import publish_evidence, resolve_route

    opener, database = _session_opener(plane)
    connection = opener()
    try:
        with connection.cursor() as cursor:
            route = resolve_route(cursor, database=database)
        connection.rollback()
    finally:
        connection.close()
    report = session_evidence(plane, route)
    connection = opener()
    try:
        return publish_evidence(connection, report, database=database)
    finally:
        connection.close()


# ---- session liveness through the HQ port (bh-osom7) ---------------------------------------

SESSION_VIA_PORT_ENV = "BH_HQ_SESSION_VIA_PORT"


def api_route() -> bool:
    """Whether this host's ``hq.route`` is ``api`` (``BH_HQ_ROUTE`` > ``hq.route`` >
    ``hq.mode: api``). An unreadable host config is not api: the SQL path then reports its own
    failure. An invalid route raises :class:`~beadhive.hq_port.HqRouteError`, never a default."""
    from .hq_port import configured_route

    try:
        host_config = config.load_host()
    except Exception:  # noqa: BLE001 - not api: the SQL path reads the config again and fails
        return False
    return configured_route(host_config) == "api"


def port_session() -> bool:
    """Whether session renew and conformance evidence go through the HQ port: always on an
    ``hq.route: api`` host (bh-65s71), else as ``hq.session_via_port`` says."""
    return api_route() or session_via_port()


def session_via_port() -> bool:
    """Whether a ``route: sql`` host's session renew and conformance evidence go through the HQ
    port (the SQL adapter). An ``hq.route: api`` host ignores it: it always uses the port
    (:func:`port_session`).

    ``BH_HQ_SESSION_VIA_PORT`` (``1``/``true``/``yes``/``on``; any other set value is off)
    overrides the host ``hq.session_via_port`` setting, which defaults off. An unreadable
    host config is off, so the direct SQL path never depends on this read."""
    raw = os.environ.get(SESSION_VIA_PORT_ENV)
    if raw:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        hq = config.load_host().get("hq")
        return bool(hq.get("session_via_port")) if hasattr(hq, "get") else False
    except Exception:  # noqa: BLE001 - off: the unchanged SQL path
        return False


def _port_deadline(plane) -> float:
    timeout = getattr(plane, "operation_timeout", None)
    if timeout is None:
        timeout = plane.settings["runtime"]["operation_timeout"] if plane is not None else 30.0
    return time.monotonic() + timeout


def _port_anchor(plane, port=None, route: str = "sql"):
    """This frame's route as a port anchor.

    api: the port's ``read_frame_anchor`` (``GET /eligibility/{frame}`` bound to the verified
    authority, bh-65s71); no SQL frame runtime is read. sql: this host's own SQL runtime
    principal; without one nothing is sent (``HqUnavailable``)."""
    from .hq_port import FrameAnchor, HqUnavailable

    if route == "api":
        return port.read_frame_anchor(expected_head=None, deadline=_port_deadline(plane))
    if plane is None:
        raise HqUnavailable("no SQL frame runtime to resolve this frame's route")
    binding = plane._runtime_authority().load_frame_binding(expected_head=None)
    return FrameAnchor(
        principal=binding.principal,
        frame=binding.frame_id,
        epoch=binding.epoch,
        holder=binding.holder_identity,
        instance=binding.instance_ref,
        carrier=binding.inbox_table,
    )


def _select_port(select):
    """``(port, route)``: *select* is the sender's port factory (``hq.route`` -> adapter),
    injected so this module never imports the adapter that imports it back."""
    from .hq_port import HqRouteError, configured_route

    if select is None:
        raise HqRouteError("session calls go through the HQ port but no selector was supplied")
    return select(), configured_route(config.load_host())


def host_config():
    """The loaded host config, for the sender's port selector."""
    return config.load_host()


def _unswitched(exc: BaseException) -> BaseException:
    """The SQL session ``NotSwitched`` behind a port refusal, so a legacy incarnation stays a
    skip on the SQL route; every other port error is returned unchanged."""
    from .hq_sql_session import NotSwitched

    cause = exc.__cause__
    return cause if isinstance(cause, NotSwitched) else exc


def _port_renewer(plane, select):
    """A renewer whose every tick is one ``HqPort.renew_session`` on the ``hq.route`` adapter.

    ``None`` only where the direct path is also ``None`` (route ``sql``, no frame runtime).
    On the api route the anchor is the port's (:func:`_port_anchor`) and *plane* is unused: no
    SQL frame runtime is needed or read. No tick falls back to the SQL session path."""
    from .hq_sql_session import SessionRenewer, SessionRoute, evidence_table, session_table

    port, route = _select_port(select)
    if plane is None and route == "sql":
        return None

    class PortRenewer(SessionRenewer):
        def __init__(self):
            super().__init__(lambda: None)
            self._anchor = None

        def tick(self):
            with _ORDER_LOCK:  # one renew at a time in this process (timer vs evidence)
                return self._tick()

        def _tick(self):
            try:
                if self._anchor is None:
                    self._anchor = _port_anchor(plane, port, route)
                result = _renew_recorded(port, self._anchor, plane)
            except BaseException as exc:
                self._anchor = self.route = None
                unwrapped = _unswitched(exc)
                if unwrapped is exc:
                    raise
                raise unwrapped from exc
            self.route = SessionRoute(
                result.principal,
                result.epoch,
                session_table(result.principal, result.epoch),
                evidence_table(result.principal, result.epoch),
            )
            self.renewals += 1
            return self.route

    return PortRenewer()


def port_evidence(plane, anchor, route: str, lease=None):
    """The :class:`~beadhive.hq_port.EvidenceReport` to post on *route* for *anchor*.

    api: the exact ``{method, checks, free_sessions}`` report the beat digested, rebuilt from
    the beat's lease (:func:`lease_report`, digest re-checked). *lease* is the beat the service
    accepted (bh-a3369); without one it is rebuilt from the cached conformance. sql: the
    evidence-row fields. A frame that cannot build a beat has nothing to post on api
    (``HqUnavailable``); on sql the row says ``unavailable``, as the direct path does."""
    from .hq_port import EvidenceReport, HqUnavailable

    try:
        if lease is None:
            lease = generate(plane, cached=True)
        if route == "api":
            return EvidenceReport(lease_report(lease))
        release = lease.release.model_dump()
        return EvidenceReport(
            {
                "epoch": anchor.epoch,
                "release_id": str(release["id"]),
                "release_digest": str(release["digest"]),
                "profile": lease.conformance.profile,
                "status": lease.conformance.status,
                "report_digest": lease.report_digest,
            }
        )
    except HeartbeatError as exc:
        if route == "api":
            raise HqUnavailable(f"no beat report to post ({refusal_category(exc)})") from None
        measured = installed_release()
        return EvidenceReport(
            {
                "epoch": anchor.epoch,
                "release_id": str(measured["id"]),
                "release_digest": str(measured["digest"]),
                "profile": "",
                "status": "unavailable",
                "report_digest": "",
            }
        )


# Cutover ordering (bh-a3369). A cut-over incarnation must have an ACCEPTED renew before its first
# evidence post: the service refuses evidence it cannot bind to a current beat (no_current_beat).
# "Accepted" is the adapter's ``accepted`` or ``replayed`` outcome (a replay is the service
# confirming it holds that very beat); a refusal, an unknown outcome or a transport failure is not.
# The ordering state is IN PROCESS MEMORY, keyed by the incarnation ``(frame, holder, epoch)``: the
# seq state file (hq_renew_seq) cannot say "accepted" (it also advances on refusals) and is left
# alone. The value is the beat the service accepted (``None`` when the port carries none). Each
# sender run is its own process, so a conformance run renews once before its first post and a
# long-lived process never re-renews before every post; if the held beat has lapsed meanwhile, the
# service's no_current_beat triggers one bounded renew-and-retry. The lock serialises renews
# within this process (the timer loop and an evidence post); cross-process flock is bh-ktd2x.
_ORDER_LOCK = threading.RLock()
_ACCEPTED: dict[tuple, object] = {}


def _incarnation(anchor) -> tuple:
    return (anchor.frame, anchor.holder, anchor.epoch)


def _renew_recorded(port, anchor, plane):
    """One ``port.renew_session``; an accepted result is recorded (with its beat), a failure
    forgets the incarnation so nothing is posted on a stale acceptance. Hold ``_ORDER_LOCK``."""
    key = _incarnation(anchor)
    try:
        result = port.renew_session(anchor, deadline=_port_deadline(plane))
    except BaseException:
        _ACCEPTED.pop(key, None)
        raise
    _ACCEPTED[key] = result.beat
    return result


def _post_evidence(port, plane, anchor, route):
    """Post the evidence of the beat the service last accepted for this incarnation, so the
    report digest always matches the held beat even if the conformance cache changed after the
    renew (or a resent pending beat digested an older report)."""
    report = port_evidence(plane, anchor, route, lease=_ACCEPTED.get(_incarnation(anchor)))
    return port.publish_evidence(anchor, report, deadline=_port_deadline(plane))


def _publish_evidence_via_port(plane, select, *, renew_first: bool = False):
    """:func:`publish_session_evidence` through ``HqPort.publish_evidence``; any refusal
    (``HqNoCurrentBeat`` included) is raised and nothing is re-routed to SQL.

    ``renew_first`` (the sender): evidence is never posted before an accepted renew. If this
    process has none yet for the incarnation it renews first; any renew failure is raised and no
    evidence is posted. On ``HqNoCurrentBeat`` it renews once more and posts once more, no loop,
    no wait; any failure of that renew or post is raised. Without it a single post is made, as
    before. On the api route the anchor and any beat rebuilt for the report come from the port
    (:class:`~beadhive.hq_frame_grant.ApiFrameAuthority`), never from a SQL plane."""
    port, route = _select_port(select)
    if plane is None and route == "sql":
        return None
    try:
        anchor = _port_anchor(plane, port, route)
        if route == "api":
            from .hq_frame_grant import ApiFrameAuthority

            plane = ApiFrameAuthority(port, anchor.frame)
        if not renew_first:
            result = _post_evidence_unordered(port, plane, anchor, route)
        else:
            with _ORDER_LOCK:
                if _incarnation(anchor) not in _ACCEPTED:
                    _renew_recorded(port, anchor, plane)
                try:
                    result = _post_evidence(port, plane, anchor, route)
                except _no_current_beat():
                    _renew_recorded(port, anchor, plane)
                    result = _post_evidence(port, plane, anchor, route)
        return result
    except Exception as exc:
        unwrapped = _unswitched(exc)
        if unwrapped is exc:
            raise
        raise unwrapped from exc


def _no_current_beat():
    from .hq_port import HqNoCurrentBeat

    return HqNoCurrentBeat


def _post_evidence_unordered(port, plane, anchor, route):
    report = port_evidence(plane, anchor, route)
    return port.publish_evidence(anchor, report, deadline=_port_deadline(plane))


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
        except Exception as exc:
            # Transport, signing and config exceptions may contain credential references:
            # print only a fixed cause category, never the exception text.
            typer.echo(
                f"measured heartbeat refused ({refusal_category(exc)}); verify release, grant, "
                "configuration, signer and transport",
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
