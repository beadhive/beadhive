"""Doctor: every failure mode (tmpfs/ramfs data dir, wrong version, unhealthy, no password,
non-loopback bind) is reported with a remedy, through injected probes only."""

from __future__ import annotations

from dataclasses import replace

import pytest

from beadhive_beadlog_xtdb import doctor
from beadhive_beadlog_xtdb.doctor import Probes, Severity, run_checks
from beadhive_beadlog_xtdb.settings import XtdbSettings
from xtdb_plugin_fakes import SECRET, Connector

HEALTHY = XtdbSettings(
    enabled=True,
    dsn="host=127.0.0.1 port=15470 user=xtdb dbname=xtdb",
    data_dir="/data/xtdb",
)


class Node:
    """A fake node: refuses password-less connections unless ``trust``."""

    def __init__(self, *, trust: bool = False, version: str = "2.2.0", up: bool = True) -> None:
        self.trust, self.version, self.up = trust, version, up
        self.connector = Connector()

    def connect(self, dsn: str, password: str | None):
        if not self.up:
            raise ConnectionRefusedError("connection refused")
        if password is None and not self.trust:
            raise PermissionError("password authentication failed for user xtdb")
        return self.connector(dsn, password)

    def server_version(self, _connection) -> str:
        from beadlog_xtdb import check_version

        return str(check_version(f"PostgreSQL 16 (XTDB {self.version})"))


def probes(node: Node | None = None, **overrides) -> Probes:
    node = node or Node()
    base = Probes(
        filesystem_type=lambda path: "ext4",
        path_exists=lambda path: True,
        http_get_status=lambda url, timeout: 200,
        tcp_open=lambda host, port, timeout: False,
        local_addresses=lambda: ("192.168.1.20",),
        connect=node.connect,
        server_version=node.server_version,
    )
    return replace(base, **overrides)


def rows(settings=HEALTHY, *, configured=True, password=SECRET, **kwargs):
    result = run_checks(
        settings,
        credential_configured=configured,
        password_source=lambda: password,
        probes=kwargs.pop("probes", None) or probes(),
        **kwargs,
    )
    return {row.check: row for row in result}


def assert_only_error(found, check: str, text: str) -> None:
    errors = {name for name, row in found.items() if row.severity is Severity.ERROR}
    assert errors == {check}, {name: row.detail for name, row in found.items()}
    assert text in found[check].detail
    assert found[check].remedy


def test_a_healthy_node_passes_every_check_and_never_renders_the_password() -> None:
    found = rows()
    assert list(found) == ["image", "data-dir", "password", "bind", "health", "version", "auth"]
    assert {row.severity for row in found.values()} == {Severity.OK}
    assert all(SECRET not in str(row.to_json()) for row in found.values())
    assert "XTDB 2.2.0" in found["version"].detail


@pytest.mark.parametrize("filesystem", ["tmpfs", "ramfs"])
def test_a_ram_backed_data_dir_is_refused(filesystem) -> None:
    found = rows(probes=probes(filesystem_type=lambda path: filesystem))
    assert_only_error(found, "data-dir", f"is on {filesystem}: refused")
    assert "real-disk" in found["data-dir"].remedy


def test_an_unset_or_missing_data_dir_is_a_warning() -> None:
    assert rows(replace(HEALTHY, data_dir=""))["data-dir"].severity is Severity.WARN
    missing = rows(probes=probes(path_exists=lambda path: False))["data-dir"]
    assert missing.severity is Severity.WARN and "does not exist" in missing.detail


@pytest.mark.parametrize("version", ["2.1.0", "2.3.0", "2.2.0-beta1"])
def test_a_wrong_xtdb_version_is_an_error(version) -> None:
    found = rows(probes=probes(Node(version=version)))
    assert_only_error(found, "version", "wrong XTDB version")


@pytest.mark.parametrize(
    ("status", "text"),
    [(503, "answered 503"), (405, "answered 405")],
)
def test_an_unhealthy_node_is_an_error_naming_the_get_probe(status, text) -> None:
    found = rows(probes=probes(http_get_status=lambda url, timeout: status))
    assert_only_error(found, "health", text)
    assert "HEAD" in found["health"].remedy and "OutOfMemoryError" in found["health"].remedy


def test_an_unreachable_health_endpoint_is_an_error() -> None:
    def refused(url, timeout):
        raise ConnectionRefusedError("refused")

    found = rows(probes=probes(http_get_status=refused))
    assert_only_error(found, "health", "failed (ConnectionRefusedError)")


def test_no_password_configured_is_an_error() -> None:
    found = rows(configured=False, password=None, probes=probes(Node(trust=True)))
    assert_only_error(found, "password", "no password")
    assert "fnox" in found["password"].remedy
    assert "auth" not in found  # nothing to compare a password-less login against


def test_a_failing_or_empty_fnox_lookup_is_an_error() -> None:
    def broken():
        raise RuntimeError("fnox credential unavailable")

    found = run_checks(
        HEALTHY, credential_configured=True, password_source=broken, probes=probes(Node(trust=True))
    )
    assert {row.check: row.severity for row in found}["password"] is Severity.ERROR
    assert rows(password="")["password"].detail == "no password: the fnox entry is empty"


def test_a_node_that_accepts_no_password_is_an_auth_error() -> None:
    found = rows(probes=probes(Node(trust=True)))
    assert_only_error(found, "auth", "WITHOUT a password")
    assert "XTDB_PASSWORD" in found["auth"].remedy


@pytest.mark.parametrize(
    ("settings", "text", "forbidden"),
    [
        (replace(HEALTHY, dsn="host=10.0.0.5 port=5432"), "plugins.beadlog-xtdb.dsn", "connect"),
        (
            replace(HEALTHY, health_url="http://10.0.0.5:8080/healthz/alive"),
            ".health_url",
            "http_get_status",
        ),
    ],
)
def test_a_non_loopback_configuration_is_a_bind_error_and_is_not_probed(
    settings, text, forbidden
) -> None:
    called: list[str] = []

    def never(*args):
        called.append("probe")
        raise AssertionError("no probe may leave loopback")

    found = rows(settings, probes=probes(**{forbidden: never}))
    assert found["bind"].severity is Severity.ERROR
    assert "non-loopback" in found["bind"].detail and text in found["bind"].detail
    assert "127.0.0.1" in found["bind"].remedy
    assert called == []


def test_a_port_that_also_answers_off_loopback_is_a_bind_error() -> None:
    seen: list[tuple[str, int]] = []

    def tcp_open(host, port, timeout):
        seen.append((host, port))
        return True

    found = rows(probes=probes(tcp_open=tcp_open))
    assert_only_error(found, "bind", "non-loopback bind: the pgwire port also answers on")
    assert seen == [("192.168.1.20", 15470)]


def test_a_node_that_is_down_is_a_version_error() -> None:
    found = rows(probes=probes(Node(up=False)))
    assert found["version"].severity is Severity.ERROR
    assert "cannot connect" in found["version"].detail


def test_an_image_that_is_not_the_validated_digest_is_a_warning() -> None:
    other = "ghcr.io/xtdb/xtdb@sha256:" + "0" * 64
    found = rows(replace(HEALTHY, image=other))
    assert found["image"].severity is Severity.WARN and "conformance" in found["image"].remedy


def test_without_network_only_configuration_and_the_host_are_checked() -> None:
    def never(*args):
        raise AssertionError("no lookup, connection or request while disabled")

    found = run_checks(
        HEALTHY,
        credential_configured=True,
        password_source=never,
        probes=probes(http_get_status=never, connect=never, tcp_open=never, local_addresses=never),
        network=False,
    )
    assert [row.check for row in found] == ["image", "data-dir", "password", "bind"]
    assert {row.severity for row in found} == {Severity.OK}


def test_filesystem_type_reads_the_longest_matching_mount(tmp_path, monkeypatch) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n"
        "30 22 0:25 / /tmp rw - tmpfs tmpfs rw\n"
        "31 22 0:26 / /data/my\\040disk rw - xfs /dev/sdb1 rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "_MOUNTINFO", mountinfo)
    monkeypatch.setattr(doctor.os.path, "realpath", lambda path: path)
    assert doctor.filesystem_type("/tmp/xtdb") == "tmpfs"
    assert doctor.filesystem_type("/tmpfoo") == "ext4"
    assert doctor.filesystem_type("/data/my disk/xtdb") == "xfs"
    monkeypatch.setattr(doctor, "_MOUNTINFO", tmp_path / "absent")
    assert doctor.filesystem_type("/tmp") is None
