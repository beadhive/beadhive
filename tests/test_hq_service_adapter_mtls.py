"""mTLS client-cert presentation in ``adapter_from_config`` (bh-wq2a5).

httpx 0.28 ignores ``cert=`` when ``verify`` is a CA path, so the client cert was never sent.
"""

from __future__ import annotations

import datetime as dt
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from beadhive.hq_service_adapter import adapter_from_config


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _issue(cn: str, issuer: x509.Name, issuer_key: Any, *, ca: bool = False, san: bool = False):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
    return key, builder.sign(issuer_key, hashes.SHA256())


def _write(path: Path, key: Any, cert: x509.Certificate) -> tuple[str, str]:
    crt, pem = path.with_suffix(".crt"), path.with_suffix(".key")
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(crt), str(pem)


def test_adapter_presents_client_cert_over_mtls(tmp_path: Path) -> None:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("test-ca"))
        .issuer_name(_name("test-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    srv_key, srv_cert = _issue("localhost", ca_cert.subject, ca_key, san=True)
    cli_key, cli_cert = _issue("gateway-client", ca_cert.subject, ca_key)
    srv_crt, srv_pem = _write(tmp_path / "server", srv_key, srv_cert)
    cli_crt, cli_pem = _write(tmp_path / "client", cli_key, cli_cert)

    peers: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            peer = self.connection.getpeercert()  # type: ignore[attr-defined]
            peers.append(dict(x[0] for x in peer["subject"])["commonName"])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: Any) -> None:
            pass

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(srv_crt, srv_pem)
    server_ctx.load_verify_locations(cafile=str(ca_path))
    server_ctx.verify_mode = ssl.CERT_REQUIRED
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = server_ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = adapter_from_config(
            {
                "hq": {
                    "route": "api",
                    "api": {
                        "url": f"https://localhost:{httpd.server_address[1]}",
                        "ca_cert": str(ca_path),
                        "client_cert": cli_crt,
                        "client_key": cli_pem,
                        "floor_path": str(tmp_path / "floor"),
                    },
                }
            },
            verifier=lambda *a, **k: None,
        )
        response = adapter._client.get("/ping")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
    assert response.status_code == 200
    assert peers == ["gateway-client"]


def test_client_kwargs_use_one_ssl_context_without_cert(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class Spy(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("beadhive.hq_service_adapter.httpx.Client", Spy)
    adapter_from_config(
        {"hq": {"route": "api", "api": {"url": "https://localhost:1"}}},
        verifier=lambda *a, **k: None,
    )
    assert isinstance(seen["verify"], ssl.SSLContext)
    assert "cert" not in seen
