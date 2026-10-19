"""An S3-compatible endpoint for server-mode Dolt remote proofs: moto in SERVER mode (bh-82gpi).

One fixture serves both proofs that need an object store — the HQ Dolt remote lifecycle
(``tests/test_hq_dolt_remotes_e2e.py``) and the two-store repository proof
(``tests/test_two_store_e2e_int.py``, bh-lniwf.8). Operator decisions (2026-10-17) it encodes:

* **moto server mode on loopback**, never MinIO (end of life), never real S3, no scratch bucket.
  The hermetic fence (``scripts/hermetic.sh``) keeps loopback up, so the server works inside it.
* **Dummy credentials only** (``testing``/``testing``). They are handed to the store's process
  environment for the test's duration and nowhere else — no credentials file is ever written.
* **Skip, never fail, without moto.** ``moto[server]`` is a dev-only dependency; a checkout
  without it skips every test that asks for :func:`moto_s3` with :data:`MOTO_MISSING`.

Measured against the pinned Dolt 2.3.5 / bd 1.3.0 (2026-10-17), which is why the fixture looks
the way it does:

* Dolt's S3 client speaks to moto through ``s3://<bucket>/<db>?endpoint=<url>&path-style=true
  &region=<r>`` (the S3-compatible form, dolthub/dolt#11571) — push, clone and full history.
* bd's EMBEDDED engine does not speak ``s3://`` at all (``beadhive.dolt_remote.S3_EMBEDDED``),
  so these proofs run bd in server mode against an isolated shared server.
* In server mode the S3 transfer runs inside the **dolt sql-server**, not the bd client: AWS
  credentials set only on the bd child (a per-call env) never reach it and the push fails with
  ``get credentials: failed to refresh cached credentials, no EC2 IMDS role found``. The
  credentials must be in the server's environment when it starts — :meth:`MotoS3.server_env`
  (bh-pphik).
  Either the process environment carries them (ambient, ``server_env``), or bh injects a
  configured ``dolt.server_credentials`` source when it starts the server (bh-pphik; the
  non-ambient proof in ``test_hq_dolt_remotes_e2e``).

:meth:`MotoS3.pause` / :meth:`MotoS3.resume` stop and restart the HTTP listener on the same port;
moto keeps its state in-process, so the bucket's objects survive — an honest "endpoint went
away mid-sync" fault with nothing simulated about the retry that follows.
"""

from __future__ import annotations

import importlib.util
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

MOTO_MISSING = (
    "moto[server] is not installed: the S3-compatible fixture (moto server mode) is a dev-only "
    "dependency — `uv sync` installs it (bh-82gpi)"
)
REGION = "us-east-1"
#: moto's documented dummy credentials. Never real; never written to a file.
DUMMY_CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_REGION": REGION,
    # Fail fast instead of waiting on an instance-metadata endpoint that does not exist here.
    "AWS_EC2_METADATA_DISABLED": "true",
}


def moto_available() -> bool:
    """moto plus its server-mode extra (flask) are importable."""
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("moto", "flask"))
    except (ImportError, ValueError):
        return False


@dataclass
class MotoS3:
    """A running moto S3 endpoint with one bucket."""

    bucket: str
    port: int
    _server: Any = field(repr=False, default=None)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        """The Dolt ``s3://`` URL of database ``path`` in the bucket (S3-compatible form)."""
        return (
            f"s3://{self.bucket}/{path.strip('/')}?endpoint={self.endpoint}"
            f"&path-style=true&region={REGION}"
        )

    def server_env(self) -> dict[str, str]:
        """Variables the dolt sql-server (and any bd child) needs to reach this endpoint."""
        return dict(DUMMY_CREDENTIALS)

    def client(self):
        import boto3  # lazy: moto[server] brings boto3; only these proofs pay the import

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=REGION,
            aws_access_key_id=DUMMY_CREDENTIALS["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=DUMMY_CREDENTIALS["AWS_SECRET_ACCESS_KEY"],
        )

    def keys(self, prefix: str = "") -> list[str]:
        listed = self.client().list_objects_v2(Bucket=self.bucket, Prefix=prefix.strip("/"))
        return sorted(obj["Key"] for obj in listed.get("Contents", []))

    def pause(self) -> None:
        """Take the endpoint off the network (connection refused); state is kept."""
        if self._server is not None:
            self._server.stop()
            self._server = None

    def resume(self) -> None:
        """Bring the endpoint back on the same port with the same objects."""
        if self._server is None:
            self._server = _start(self.port)

    def stop(self) -> None:
        self.pause()


def _start(port: int):
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    return server


def start_moto_s3(bucket: str | None = None) -> MotoS3:
    """Start moto on a free loopback port and create ``bucket`` (default: a unique name)."""
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = _start(0)
    _, port = server.get_host_and_port()
    s3 = MotoS3(bucket=bucket or f"bh-e2e-{uuid.uuid4().hex[:10]}", port=int(port), _server=server)
    s3.client().create_bucket(Bucket=s3.bucket)
    return s3


@pytest.fixture
def moto_s3() -> Iterator[MotoS3]:
    """A per-test moto S3 endpoint and bucket; skips (never fails) without ``moto[server]``."""
    if not moto_available():
        pytest.skip(MOTO_MISSING)
    s3 = start_moto_s3()
    try:
        yield s3
    finally:
        s3.stop()
