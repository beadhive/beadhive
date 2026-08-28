"""Owned-host Development launcher backed by the loopback Beadhive operator daemon."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
from joserfc.jwk import KeySet

from . import gateway_read
from .remote_gateway import (
    DEVELOPMENT_INSTANCE_ID,
    DEVELOPMENT_ISSUER,
    ClerkTokenVerifier,
    DevelopmentGatewayConfig,
    DevelopmentInstanceRegistry,
    RemoteInstance,
    StaleCommandScope,
    StaleEventCursor,
    build_development_gateway_application,
)

APP_ORIGIN = "https://app-dev.beadhive.cloud"
GATEWAY_ORIGIN = "https://gateway-dev.beadhive.cloud"
AUDIENCE = "beadhive-gateway-dev"
LOOPBACK_ORIGIN = "http://127.0.0.1:8420"
HIVE_ID = "github/beadhive/beadhive"
_HIVE_PATH = "/api/v1/hives/github%2Fbeadhive%2Fbeadhive"
_SUBJECT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DEMO_STATUSES = frozenset({"open", "in_progress", "blocked"})
_INTERNAL_WORK_ITEM_TYPES = frozenset({"event", "gate"})


def _remote_cursor(local: Mapping[str, object]) -> str:
    epoch = local.get("producerEpoch")
    sequence = local.get("sequence")
    if not isinstance(epoch, str) or type(sequence) is not int or sequence < 0:
        raise RuntimeError("operator cursor is incompatible")
    return f"{uuid.UUID(hex=epoch)}:{sequence}"


def _local_cursor(remote: str) -> str:
    epoch, sequence = remote.rsplit(":", 1)
    return f"{uuid.UUID(epoch).hex}:{int(sequence)}"


def _development_work_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise RuntimeError("operator work items are incompatible")
    selected: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get("record"), Mapping):
            raise RuntimeError("operator work item is incompatible")
        record = item["record"]
        status = record.get("status")
        issue_type = record.get("issueType")
        if not isinstance(status, str) or not isinstance(issue_type, str):
            raise RuntimeError("operator work item is incompatible")
        if status in _DEMO_STATUSES and issue_type not in _INTERNAL_WORK_ITEM_TYPES:
            selected.append(item)
    return selected


class LoopbackDemoRuntime:
    """Redacted async adapter over the real host daemon's exact registered hive."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=LOOPBACK_ORIGIN,
            timeout=httpx.Timeout(5.0, read=None),
            trust_env=False,
        )

    async def online(self) -> bool:
        response = await self._client.get("/health")
        if response.status_code != 200:
            return False
        value = response.json()
        return isinstance(value, dict) and value.get("live") is True and value.get("ready") is True

    async def snapshot(self) -> Mapping[str, object]:
        response = await self._client.get(f"{_HIVE_PATH}/snapshot")
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("cursor"), dict):
            raise RuntimeError("operator snapshot is incompatible")
        return {
            "schemaVersion": value.get("schemaVersion"),
            "revision": value.get("revision"),
            "generatedAt": value.get("generatedAt"),
            "workItems": _development_work_items(value.get("workItems")),
            "agents": value.get("agents"),
            "eventCursor": _remote_cursor(value["cursor"]),
        }

    async def refresh(self, expected_revision: str, _correlation_id: str) -> Mapping[str, object]:
        current = await self.snapshot()
        revision = current.get("revision")
        if revision != expected_revision:
            raise StaleCommandScope
        return {"status": "completed", "revision": revision}

    async def events(self, cursor: str) -> AsyncIterator[Mapping[str, object]]:
        context = self._client.stream(
            "GET",
            f"{_HIVE_PATH}/events",
            params={"cursor": _local_cursor(cursor), "subscription": f"hive:{HIVE_ID}"},
        )
        response = await context.__aenter__()
        if response.status_code == 409:
            await context.__aexit__(None, None, None)
            raise StaleEventCursor
        if response.status_code != 200:
            await context.__aexit__(None, None, None)
            raise RuntimeError("operator event source is unavailable")

        async def stream() -> AsyncIterator[Mapping[str, object]]:
            event_id: str | None = None
            try:
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        event_id = line.removeprefix("id: ")
                    elif line.startswith("data: "):
                        payload = json.loads(line.removeprefix("data: "))
                        if not isinstance(payload, dict) or event_id is None:
                            raise RuntimeError("operator event is incompatible")
                        current = await self.snapshot()
                        revision = current.get("revision")
                        yield {
                            "cursor": _remote_cursor(
                                {
                                    "producerEpoch": event_id.rsplit(":", 1)[0],
                                    "sequence": int(event_id.rsplit(":", 1)[1]),
                                }
                            ),
                            "revision": revision,
                        }
                        event_id = None
            finally:
                await context.__aexit__(None, None, None)

        return stream()

    async def close(self) -> None:
        await self._client.aclose()


def _read_json(path: Path) -> Any:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("gateway credential files must exist with mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def _authorized_subjects(path: Path) -> frozenset[str]:
    value = _read_json(path)
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or any(not isinstance(item, str) or _SUBJECT.fullmatch(item) is None for item in value)
    ):
        raise RuntimeError("gateway subject policy is incompatible")
    return frozenset(value)


def create_application():
    """Uvicorn factory that verifies all immutable inputs before opening the listener."""
    credentials = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/run/credentials"))
    jwks_path = Path(os.environ.get("BEADHIVE_GATEWAY_JWKS_FILE", credentials / "clerk-jwks.json"))
    subjects_path = Path(
        os.environ.get("BEADHIVE_GATEWAY_SUBJECTS_FILE", credentials / "authorized-subjects.json")
    )
    config = DevelopmentGatewayConfig(
        issuer=DEVELOPMENT_ISSUER,
        audience=AUDIENCE,
        app_origin=APP_ORIGIN,
        gateway_origin=GATEWAY_ORIGIN,
    )
    authorized_subjects = _authorized_subjects(subjects_path)
    read_source = gateway_read.load_packaged_development_source(
        authorized_subjects=authorized_subjects
    )
    key_set = KeySet.import_key_set(_read_json(jwks_path))
    runtime = LoopbackDemoRuntime()
    instance = RemoteInstance(
        display_name="Development demo",
        authorized_subjects=authorized_subjects,
        snapshot=runtime.snapshot,
        online=runtime.online,
        refresh=runtime.refresh,
        events=runtime.events,
        close=runtime.close,
    )
    return build_development_gateway_application(
        config=config,
        verifier=ClerkTokenVerifier(config=config, key=key_set),
        registry=DevelopmentInstanceRegistry(instances={DEVELOPMENT_INSTANCE_ID: instance}),
        read_source=read_source,
    )


def main() -> None:
    """Serve only on loopback; Cloudflared is the sole external transport."""
    import uvicorn

    uvicorn.run(
        "beadhive.remote_gateway_runtime:create_application",
        factory=True,
        host="127.0.0.1",
        port=8787,
        access_log=False,
        server_header=False,
    )
