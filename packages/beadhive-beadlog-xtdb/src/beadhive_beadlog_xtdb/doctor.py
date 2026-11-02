"""``bh plugin beadlog-xtdb doctor``: the checks, each failure mode with its remedy.

Read-only and loopback-only (bh-cntr0 recommendation 2). Doctor never starts, stops or restarts
the node and never writes to it. Every probe is injected through :class:`Probes` so the checks
are tested with fakes; the defaults use the standard library, and ``beadlog_xtdb`` (psycopg) only
inside :func:`default_connect`, imported when a probe actually connects.

The checks, in order (each one is a :class:`Check` row):

``image``      the configured image is digest-pinned (config enforces it); warn when it is not the
               validated 2.2.0 digest.
``data-dir``   the node's host data directory is on real disk: ``tmpfs`` / ``ramfs`` is an
               error (RAM-backed: lost on reboot, competes with the node's 2 GiB cap); unset or
               missing is a warning (cannot check).
``password``   a fnox credential is configured and resolves (the value is never shown).
``bind``       the DSN and healthz URL are loopback, and the pgwire port does not also answer on
               a non-loopback address of this host (``-p 0.0.0.0:...`` publishes it off-host).
``health``     ``GET <health_url>`` answers 200. Not the image's stock HEALTHCHECK: that sends
               HEAD, which XTDB answers ``405``, so ``docker ps`` health is not evidence.
``version``    the node is XTDB ``>=2.2.0,<2.3`` (``SELECT version()``).
``auth``       the node refuses a connection without the password (no-password TRUST mode maps
               any user and any password to the superuser).
"""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .settings import VALIDATED_IMAGE, XtdbSettings, is_loopback
from .store import CONNECT_TIMEOUT_SECONDS, default_connect, scrub

#: File systems doctor refuses for the node's data directory (RAM-backed).
REFUSED_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})
PROBE_TIMEOUT_SECONDS = 5.0
_MOUNTINFO = Path("/proc/self/mountinfo")


class Severity(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class Check:
    """One doctor row. ``remedy`` is what to do (empty for ``ok``); no field holds a secret."""

    check: str
    severity: Severity
    detail: str
    remedy: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "detail": self.detail,
            "remedy": self.remedy,
        }


def filesystem_type(path: str) -> str | None:
    """The file-system type of the mount holding ``path`` (Linux ``/proc/self/mountinfo``), or
    ``None`` when it cannot be read (another OS, or no such path)."""
    try:
        target = os.path.realpath(path)
        lines = _MOUNTINFO.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        mount = fields[4].replace("\\040", " ")
        inside = target == mount or target.startswith(mount.rstrip("/") + "/")
        if inside and (best is None or len(mount) > best[0]):
            best = (len(mount), right.split()[0])
    return best[1] if best else None


def http_get_status(url: str, timeout: float) -> int:
    """The HTTP status of a plain ``GET`` (never ``HEAD``); raises ``OSError`` when unreachable."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def tcp_open(host: str, port: int, timeout: float) -> bool:
    """Whether a TCP connect to ``host:port`` succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def local_addresses() -> tuple[str, ...]:
    """This host's non-loopback IPv4 addresses (best effort; nothing is sent anywhere)."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: a UDP connect only picks a route
            found.add(str(probe.getsockname()[0]))
    except OSError:
        pass
    return tuple(sorted(address for address in found if not is_loopback(address)))


def server_version(connection: Any) -> str:
    """``SELECT version()`` checked against ``>=2.2.0,<2.3``; returns the parsed version."""
    from beadlog_xtdb import require_supported_version  # lazy: psycopg

    return str(require_supported_version(connection))


@dataclass(frozen=True)
class Probes:
    """The injectable side effects of doctor (the defaults touch only this host and loopback)."""

    filesystem_type: Callable[[str], str | None] = filesystem_type
    path_exists: Callable[[str], bool] = os.path.isdir
    http_get_status: Callable[[str, float], int] = http_get_status
    tcp_open: Callable[[str, int, float], bool] = tcp_open
    local_addresses: Callable[[], Sequence[str]] = local_addresses
    connect: Callable[[str, str | None], Any] = default_connect
    server_version: Callable[[Any], str] = server_version
    timeout: float = field(default=PROBE_TIMEOUT_SECONDS)


# -- the checks ---------------------------------------------------------------------------------


def check_image(settings: XtdbSettings) -> Check:
    if settings.image == VALIDATED_IMAGE:
        return Check(
            "image", Severity.OK, f"pinned to the validated XTDB 2.2.0 digest ({settings.image})"
        )
    return Check(
        "image",
        Severity.WARN,
        f"pinned to {settings.image}, not the validated XTDB 2.2.0 digest",
        "before switching digests, copy the data dir, start the new image against the copy and "
        "run the beadlog loader-conformance kit and integration tests against it (bh-cntr0)",
    )


def check_data_dir(settings: XtdbSettings, probes: Probes) -> Check:
    path = settings.data_dir
    if not path:
        return Check(
            "data-dir",
            Severity.WARN,
            "plugins.beadlog-xtdb.data_dir is unset: the node's data directory is not checked",
            "set it to the host directory mounted at /var/lib/xtdb (`bh config set "
            "plugins.beadlog-xtdb.data_dir /data/<real-disk>/xtdb`)",
        )
    if not probes.path_exists(path):
        return Check(
            "data-dir",
            Severity.WARN,
            f"{path} does not exist on this host",
            "create it on a real-disk file system and mount it with -v <dir>:/var/lib/xtdb",
        )
    kind = probes.filesystem_type(path)
    if kind in REFUSED_FILESYSTEMS:
        return Check(
            "data-dir",
            Severity.ERROR,
            f"{path} is on {kind}: refused (RAM-backed; the store is lost on reboot and its pages "
            "compete with the node's memory cap)",
            "stop the node, move the directory to a real-disk path (never /tmp on this host), "
            "start it again with -v <real-disk dir>:/var/lib/xtdb and update "
            "plugins.beadlog-xtdb.data_dir",
        )
    if kind is None:
        return Check(
            "data-dir",
            Severity.WARN,
            f"cannot read the file-system type of {path} on this OS",
            "confirm by hand that it is not tmpfs or ramfs",
        )
    return Check("data-dir", Severity.OK, f"{path} is on {kind}")


def check_password(configured: bool, resolve: Callable[[], str | None]) -> tuple[Check, str | None]:
    """The password row and the resolved value (returned to the caller, never rendered)."""
    remedy = (
        "store XTDB_PASSWORD in fnox and set plugins.beadlog-xtdb.credential.config_path, "
        ".profile and .key to that entry; start the node with the same XTDB_PASSWORD "
        "(--env-file <operator-only file>)"
    )
    if not configured:
        return (
            Check(
                "password",
                Severity.ERROR,
                "no password: plugins.beadlog-xtdb.credential names no fnox entry",
                remedy,
            ),
            None,
        )
    try:
        value = resolve()
    except Exception as exc:  # noqa: BLE001 - the broker's message is static, never a value
        return Check(
            "password", Severity.ERROR, f"the fnox lookup failed: {scrub(str(exc), None)}", remedy
        ), None
    if not value:
        return Check(
            "password", Severity.ERROR, "no password: the fnox entry is empty", remedy
        ), None
    return Check("password", Severity.OK, "resolved from fnox (value not shown)"), value


def check_bind(settings: XtdbSettings, probes: Probes) -> Check:
    endpoint = settings.endpoint()
    remedy = (
        "publish the node on loopback only (-p 127.0.0.1:<port>:5432, and 127.0.0.1 for 8080 if "
        "published) and point plugins.beadlog-xtdb.dsn and .health_url at 127.0.0.1; never "
        "publish 5432, 3000 or 8080 off-host"
    )
    health_host = urlsplit(settings.health_url).hostname or ""
    off = [
        name
        for name, host in (("dsn", endpoint.host), ("health_url", health_host))
        if not is_loopback(host)
    ]
    if off:
        return Check(
            "bind",
            Severity.ERROR,
            f"non-loopback: plugins.beadlog-xtdb.{' and .'.join(off)} point off this host",
            remedy,
        )
    exposed = [
        address
        for address in probes.local_addresses()
        if probes.tcp_open(address, endpoint.port, probes.timeout)
    ]
    if exposed:
        where = ", ".join(f"{address}:{endpoint.port}" for address in exposed)
        return Check(
            "bind",
            Severity.ERROR,
            f"non-loopback bind: the pgwire port also answers on {where}",
            remedy,
        )
    return Check("bind", Severity.OK, f"loopback only ({endpoint.host}:{endpoint.port})")


def check_health(settings: XtdbSettings, probes: Probes) -> Check:
    url = settings.health_url
    remedy = (
        "the stock image HEALTHCHECK sends HEAD (XTDB answers 405), so `docker ps` health is not "
        "evidence: run the node with --health-cmd 'wget -q -O /dev/null "
        "http://127.0.0.1:8080/healthz/alive', check `docker logs` for OutOfMemoryError (a heap "
        "OOM leaves the process up but not ingesting) and restart the node if so"
    )
    try:
        status = probes.http_get_status(url, probes.timeout)
    except Exception as exc:  # noqa: BLE001 - unreachable is the finding
        return Check(
            "health", Severity.ERROR, f"unhealthy: GET {url} failed ({type(exc).__name__})", remedy
        )
    if status != 200:
        return Check("health", Severity.ERROR, f"unhealthy: GET {url} answered {status}", remedy)
    return Check("health", Severity.OK, f"GET {url} answered 200")


def _connect_rows(
    settings: XtdbSettings, password: str | None, probes: Probes
) -> tuple[Check, Check | None]:
    """The ``version`` row and (when a password is set) the ``auth`` row."""
    target = settings.endpoint().label
    try:
        connection = probes.connect(settings.dsn, password)
    except Exception as exc:  # noqa: BLE001 - not reachable is the finding
        return (
            Check(
                "version",
                Severity.ERROR,
                f"cannot connect to {target} ({type(exc).__name__}: {scrub(str(exc), password)})",
                "start the node (bh never starts it), check the dsn's port and the password",
            ),
            None,
        )
    try:
        version = probes.server_version(connection)
        version_row = Check("version", Severity.OK, f"XTDB {version} (supported >=2.2.0,<2.3)")
    except Exception as exc:  # noqa: BLE001 - the gate's message says what to do
        version_row = Check(
            "version",
            Severity.ERROR,
            f"wrong XTDB version: {scrub(str(exc), password)}",
            "run the pinned XTDB 2.2.x image (plugins.beadlog-xtdb.image)",
        )
    finally:
        _close(connection)
    if not password:
        return version_row, None
    try:
        open_connection = probes.connect(settings.dsn, None)
    except Exception:  # noqa: BLE001 - refused without a password is the good outcome
        return version_row, Check(
            "auth", Severity.OK, "the node refuses a connection without the password"
        )
    _close(open_connection)
    return version_row, Check(
        "auth",
        Severity.ERROR,
        "the node accepts a connection WITHOUT a password (TRUST: any user maps to the superuser)",
        "restart the node with XTDB_PASSWORD set (--env-file <operator-only file>)",
    )


def _close(connection: Any) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - best effort
        pass


def run_checks(
    settings: XtdbSettings,
    *,
    credential_configured: bool,
    password_source: Callable[[], str | None],
    probes: Probes | None = None,
    network: bool = True,
) -> tuple[Check, ...]:
    """Every check, in order. Network probes go to loopback only: when the DSN is not loopback
    the connection checks are skipped (the ``bind`` row already reports it). With ``network``
    false (the plugin is disabled) only the configuration and this host's file system are
    checked: no password lookup, no connection, no request."""
    probes = probes or Probes()
    rows = [check_image(settings), check_data_dir(settings, probes)]
    if not network:
        if credential_configured:
            rows.append(Check("password", Severity.OK, "a fnox entry is configured (not resolved)"))
        else:
            rows.append(check_password(False, password_source)[0])
        rows.append(check_bind(settings, replace(probes, local_addresses=lambda: ())))
        return tuple(rows)
    password_row, password = check_password(credential_configured, password_source)
    rows.append(password_row)
    rows.append(check_bind(settings, probes))
    if is_loopback(urlsplit(settings.health_url).hostname or ""):
        rows.append(check_health(settings, probes))
    if is_loopback(settings.endpoint().host):
        version_row, auth_row = _connect_rows(settings, password, probes)
        rows.append(version_row)
        if auth_row is not None:
            rows.append(auth_row)
    return tuple(rows)


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "REFUSED_FILESYSTEMS",
    "Check",
    "Probes",
    "Severity",
    "check_bind",
    "check_data_dir",
    "check_health",
    "check_image",
    "check_password",
    "filesystem_type",
    "http_get_status",
    "local_addresses",
    "run_checks",
    "server_version",
    "tcp_open",
]
