"""Shared daemon configuration and authenticated loopback transport acceptance."""

import asyncio

import httpx
import pytest

from beadhive import daemon_auth
from beadhive.frame_bridge_daemon import configured_daemon_origin, validate_daemon_origin
from beadhive.frame_bridge_upstream import HostDaemonFrameBridgeSource, RegisteredInstance


@pytest.mark.parametrize("port", [8737, 9742])
def test_configured_daemon_health_transport(port: int) -> None:
    origin = configured_daemon_origin({"host": {"daemon": {"port": port}}})
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "live", "ready": True})

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url=origin, transport=httpx.MockTransport(handler)
        ) as client:
            source = HostDaemonFrameBridgeSource(
                daemon_bearer=daemon_auth.SecretBearer("test-daemon-bearer"),
                instance=RegisteredInstance(),
                client=client,
                daemon_origin=origin,
            )
            assert await source.online()

    asyncio.run(run())
    assert requests[0].url.port == port
    assert requests[0].url.path == "/health"


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com:8737",
        "http://192.0.2.1:8737",
        "http://127.0.0.1",
        "http://user:secret@127.0.0.1:8737",
        "http://127.0.0.1:8737/path",
        "http://127.0.0.1:8737?x=1",
        "http://127.0.0.1:8737#fragment",
    ],
)
def test_daemon_origin_rejects_nonlocal_or_ambiguous_destinations(origin: str) -> None:
    with pytest.raises(ValueError, match="literal loopback"):
        validate_daemon_origin(origin)


def test_ipv6_daemon_origin() -> None:
    assert configured_daemon_origin({"host": {"daemon": {"bind": "::1"}}}) == "http://[::1]:8737"


def test_managed_home_reads_enrollment_and_local_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from beadhive import config, host
    from beadhive.config_schema import BeadhiveConfig

    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.delenv("BH_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text(
        "host:\n  daemon:\n    port: 9742\n  frame_bridge:\n"
        "    primary_hive_id: github/acme/frame\n"
    )
    (tmp_path / "host.yaml").write_text("host_id: frame-local\nlabel: Local frame\n")
    assert config.home() == tmp_path
    raw = config.load()
    assert configured_daemon_origin(raw) == "http://127.0.0.1:9742"
    assert (
        BeadhiveConfig.model_validate(raw).host.frame_bridge.primary_hive_id == "github/acme/frame"
    )
    assert host.host_id() == "frame-local"


def test_enrollment_identity_is_host_owned() -> None:
    from beadhive.modules.config.application.partition import HOST, HOST_KEYS, partition_of

    for field in ("host_id", "instance_id", "factory_id", "primary_hive_id"):
        path = f"host.frame_bridge.{field}"
        assert partition_of(path) == HOST
        assert path in HOST_KEYS
