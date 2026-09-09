"""Authenticated HTTP boundary for durable exact-run activity."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from beadhive import (
    daemon_auth,
    daemon_state_broker,
    host_daemon,
    operator_api,
    operator_feed,
    operator_sources,
    operator_sse,
    public_readers,
)
from beadhive.agent_run_summary import Freshness
from beadhive.daemon_config import HostDaemonConfig
from beadhive.daemon_contract import AuthScope

HIVE = "github/beadhive/beadhive"
DIGEST = "sha256:" + "a" * 64


_raw_build_product_application = host_daemon.build_product_application


def _build_product_application(**kwargs):
    return _raw_build_product_application(
        state_broker_factory=daemon_state_broker.DaemonStateBroker.for_host,
        **kwargs,
    )


def _settings(
    path: Path,
    *,
    activity_bytes: int = 1_024,
    activity_records: int = 100,
    rate: int = 600,
) -> HostDaemonConfig:
    return HostDaemonConfig(
        enabled=True,
        auth={"credential_file": path, "token_rate_limit_per_minute": rate},
        http={
            "allowed_hosts": ["127.0.0.1"],
            "max_request_body_bytes": 8_192,
        },
        activity={
            "max_body_bytes": activity_bytes,
            "max_records_per_read": activity_records,
            "idempotency_retention_seconds": 120,
        },
    )


def _record(run_id: str, *, writer: str = "baml.provider") -> dict[str, object]:
    return {
        "version": "beadhive.run-journal/v1",
        "source_revision": "opaque:journal-1",
        "timestamp_ms": 1_000,
        "run_id": run_id,
        "hive": HIVE,
        "bead": "bh-q0lol.11",
        "driver": "baml",
        "provider": "codex",
        "manifest_digest": DIGEST,
        "provider_continuation": None,
        "writer": writer,
        "activity": {"kind": "run.created", "phase": "planned"},
    }


def _install_exact_journal(
    app, run_id: str, *, writer: str = "baml.provider", unavailable: bool = False
) -> None:
    source = public_readers.RunDirectoryEntry(
        hive_id=HIVE,
        run_id=run_id,
        path=Path("/not-disclosed") / f"{run_id}.jsonl",
        modified_at=1.0,
        device=1,
        inode=2,
        root_device=1,
        root_inode=1,
    )
    hive = operator_sources.ExactHive(
        HIVE, {"provider": "github", "org": "beadhive", "repo": "beadhive"}
    )

    def locate(candidate: str):
        if candidate != run_id:
            raise operator_sources.OperatorSourceError(
                "run_not_found", "The exact outer run was not found.", status_code=404
            )
        return hive, source

    def read(_hive, _source, candidate: str):
        if unavailable:
            raise operator_sources.OperatorSourceError(
                "activity_source_unavailable",
                "The authoritative run activity source is unavailable.",
                status_code=503,
                retryable=True,
            )
        return public_readers.RunJournalFrame(
            frame=public_readers.JournalFrameKind.SNAPSHOT,
            host_id="host-test",
            source_id="opaque:source",
            run_id=candidate,
            source_revision="opaque:journal-1",
            since_revision=None,
            records=(_record(run_id, writer=writer),),
            coverage=public_readers.Coverage.COMPLETE,
            coverage_reason=None,
            freshness=Freshness(state="fresh", as_of=1.0),
        )

    app.state.operator_sources.locate_run = locate
    app.state.operator_sources.read_run = read
    app.state.operator_sources.resolve_hive = lambda identity: (
        hive
        if identity == HIVE
        else (_ for _ in ()).throw(
            operator_sources.OperatorSourceError(
                "hive_not_found", "The exact hive was not found.", status_code=404
            )
        )
    )
    app.state.operator_sources.registered_hives = lambda: (hive,)


def _app(
    tmp_path: Path,
    *,
    activity_bytes: int = 1_024,
    activity_records: int = 100,
    rate: int = 600,
):
    credential_path = (tmp_path / "credentials.json").absolute()
    publisher = daemon_auth.provision_credential_file(
        credential_path,
        credential_id="publisher",
        audience="beadhive-host",
        principal="service:baml:developer",
        scopes=(AuthScope.ACTIVITY_PUBLISH,),
        expires_at=int(time.time()) + 3_600,
    )
    reader = daemon_auth.add_credential(
        credential_path,
        credential_id="reader",
        audience="beadhive-host",
        principal="operator:test",
        scopes=(AuthScope.OPERATOR_READ,),
        expires_at=int(time.time()) + 3_600,
    )
    record = host_daemon.ControlRecord(
        contract=host_daemon.CONTRACT_VERSION,
        account_id="uid:test",
        bh_home=str(tmp_path),
        host_id="host-test",
        instance_id="instance-test",
        pid=os.getpid(),
        process_start="test:1",
        listener_host="127.0.0.1",
        listener_port=8737,
        started_at="2026-09-03T00:00:00+00:00",
    )
    app = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=record,
        cfg={"managed_repos": []},
        settings=_settings(
            credential_path,
            activity_bytes=activity_bytes,
            activity_records=activity_records,
            rate=rate,
        ),
    )
    tokens = {
        "publish": publisher.bearer.reveal_for_authority(),
        "read": reader.bearer.reveal_for_authority(),
    }
    for source in ("hitch", "beadhive"):
        credential = daemon_auth.add_credential(
            credential_path,
            credential_id=f"publisher-{source}",
            audience="beadhive-host",
            principal=f"service:{source}:developer",
            scopes=(AuthScope.ACTIVITY_PUBLISH,),
            expires_at=int(time.time()) + 3_600,
        )
        tokens[source] = credential.bearer.reveal_for_authority()
    return app, tokens, record


def _request(run_id: str, *, source: str = "baml", writer: str = "baml.provider"):
    now = time.time_ns() // 1_000_000
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "idempotencyKey": f"{source}:event-1",
        "source": source,
        "kind": "provider.progress",
        "occurredAt": now,
        "expiresAt": now + 60_000,
        "payload": {"phase": "running", "seat": "developer", "writer": writer},
    }


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _principal(source: str = "baml") -> daemon_auth.AuthenticatedPrincipal:
    return daemon_auth.AuthenticatedPrincipal(
        credential_id=f"publisher-{source}",
        principal=f"service:{source}:developer",
        audience="beadhive-host",
        scopes=frozenset({AuthScope.ACTIVITY_PUBLISH}),
        expires_at=int(time.time()) + 3_600,
        generation=1,
    )


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", "baml.provider"),
        ("hitch", "agent-hitch.direct"),
        ("beadhive", "beadhive.role"),
    ],
)
def test_publish_scope_durable_ack_duplicate_and_restart(
    tmp_path: Path, source: str, writer: str
) -> None:
    run_id = f"run-http-{source}"
    app, tokens, record = _app(tmp_path / source)
    _install_exact_journal(app, run_id, writer=writer)
    payload = _request(run_id, source=source, writer=writer)
    publish_token = tokens.get(source, tokens["publish"])

    async def first_process():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                denied_write = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["read"]),
                    json=payload,
                )
                denied_read = await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(publish_token),
                )
                created = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(publish_token),
                    json=payload,
                )
                duplicate = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(publish_token),
                    json=payload,
                )
                visible = await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["read"]),
                )
                return denied_write, denied_read, created, duplicate, visible

    denied_write, denied_read, created, duplicate, visible = asyncio.run(first_process())
    assert (denied_write.status_code, denied_read.status_code) == (403, 403)
    assert created.status_code == 201
    assert duplicate.status_code == 200
    assert created.json()["status"] == "created"
    assert duplicate.json() == {**created.json(), "status": "duplicate"}
    assert len(app.state.activity_store.read(run_id).activities) == 1
    assert visible.status_code == 200
    assert visible.json()["sequence"] == 2
    assert [item["payload"]["name"] for item in visible.json()["activities"]] == [
        "run.created",
        "provider.progress",
    ]

    restarted = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=record,
        cfg={"managed_repos": []},
        settings=_settings(Path(app.state.auth_authority.path)),
    )
    _install_exact_journal(restarted, run_id, writer=writer)

    async def second_process():
        async with restarted.router.lifespan_context(restarted):
            transport = httpx.ASGITransport(app=restarted, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                duplicate = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(publish_token),
                    json=payload,
                )
                visible = await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["read"]),
                )
                return duplicate, visible

    restarted_duplicate, restarted_visible = asyncio.run(second_process())
    assert restarted_duplicate.json() == duplicate.json()
    assert restarted_visible.json()["sequence"] == 2
    assert (
        restarted_visible.json()["activities"][-1]["payload"]
        == visible.json()["activities"][-1]["payload"]
    )


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", "baml.provider"),
        ("hitch", "agent-hitch.direct"),
        ("beadhive", "beadhive.role"),
    ],
)
def test_publish_sources_use_exact_journal_and_canonical_payload_identity(
    tmp_path: Path, source: str, writer: str
) -> None:
    run_id = f"run-{source}"
    app, tokens, _record = _app(tmp_path / source)
    _install_exact_journal(app, run_id, writer=writer)
    payload = _request(run_id, source=source, writer=writer)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                created = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens.get(source, tokens["publish"])),
                    json=payload,
                )
                duplicate = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens.get(source, tokens["publish"])),
                    json=payload,
                )
                return created, duplicate

    response, duplicate = asyncio.run(exercise())
    assert (response.status_code, duplicate.status_code) == (201, 200)
    assert duplicate.json() == {**response.json(), "status": "duplicate"}
    activity = app.state.activity_store.read(run_id).activities[0]
    identity, _view = app.state.activity_store.read_registered(run_id)
    assert activity.run_id == run_id
    assert activity.source == source
    assert identity.seat == "developer"
    assert identity.writer == writer


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", "baml.provider"),
        ("hitch", "agent-hitch.direct"),
        ("beadhive", "beadhive.role"),
    ],
)
def test_every_publisher_source_exposes_expiry_drop_without_registering_run(
    tmp_path: Path, source: str, writer: str
) -> None:
    run_id = f"run-expired-{source}"
    app, tokens, _record = _app(tmp_path / source)
    _install_exact_journal(app, run_id, writer=writer)
    payload = _request(run_id, source=source, writer=writer)
    payload["occurredAt"] = 1
    payload["expiresAt"] = 1

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens.get(source, tokens["publish"])),
                    json=payload,
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "activity_expired",
    )
    with pytest.raises(Exception, match="run_unknown"):
        app.state.activity_store.read(run_id)


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", "baml.provider"),
        ("hitch", "agent-hitch.direct"),
        ("beadhive", "beadhive.role"),
    ],
)
def test_every_publisher_source_refuses_journal_outage_without_persistence(
    tmp_path: Path, source: str, writer: str
) -> None:
    run_id = f"run-outage-{source}"
    app, tokens, _record = _app(tmp_path / source)
    _install_exact_journal(app, run_id, writer=writer, unavailable=True)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens.get(source, tokens["publish"])),
                    json=_request(run_id, source=source, writer=writer),
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "activity_source_unavailable",
    )
    with pytest.raises(Exception, match="run_unknown"):
        app.state.activity_store.read(run_id)


@pytest.mark.parametrize(
    ("trusted_source", "trusted_writer", "spoofed_source", "spoofed_writer"),
    [
        ("baml", "baml.provider", "hitch", "agent-hitch.direct"),
        ("hitch", "agent-hitch.direct", "baml", "baml.provider"),
        ("beadhive", "beadhive.role", "baml", "baml.provider"),
    ],
)
def test_authenticated_publisher_cannot_spoof_other_provider_provenance(
    tmp_path: Path,
    trusted_source: str,
    trusted_writer: str,
    spoofed_source: str,
    spoofed_writer: str,
) -> None:
    run_id = f"run-spoof-{trusted_source}"
    app, tokens, _record = _app(tmp_path / trusted_source)
    _install_exact_journal(app, run_id, writer=trusted_writer)
    payload = _request(run_id, source=spoofed_source, writer=spoofed_writer)
    payload["payload"]["seat"] = "reviewer"

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens.get(trusted_source, tokens["publish"])),
                    json=payload,
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "source_identity_mismatch",
    )
    with pytest.raises(Exception, match="run_unknown"):
        app.state.activity_store.read(run_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seat", "reviewer"),
        ("writer", "agent-hitch.direct"),
        ("source", "hitch"),
    ],
)
def test_baml_payload_provenance_assertions_cannot_override_authenticated_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    run_id = f"run-baml-spoof-{field}"
    app, tokens, _record = _app(tmp_path / field)
    _install_exact_journal(app, run_id, writer="baml.provider")
    payload = _request(run_id)
    payload["payload"][field] = value

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=payload,
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        409,
        "payload_identity_mismatch",
    )
    with pytest.raises(Exception, match="run_unknown"):
        app.state.activity_store.read(run_id)


@pytest.mark.parametrize("missing", ["seat", "writer"])
def test_runtime_and_openapi_both_make_publisher_identity_assertions_optional(
    tmp_path: Path, missing: str
) -> None:
    run_id = f"run-missing-{missing}"
    app, tokens, _record = _app(tmp_path / missing)
    _install_exact_journal(app, run_id)
    payload = _request(run_id)
    del payload["payload"][missing]
    schema = operator_api.openapi_document()["components"]["schemas"]["ActivityAppendRequest"]
    assert "required" not in schema["properties"]["payload"]

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=payload,
                )

    response = asyncio.run(exercise())
    assert response.status_code == 201
    identity, view = app.state.activity_store.read_registered(run_id)
    assert (identity.seat, identity.writer) == ("developer", "baml.provider")
    assert len(view.activities) == 1


def test_publish_refuses_when_daemon_is_not_accepting_without_persistence(
    tmp_path: Path,
) -> None:
    run_id = "run-draining"
    app, tokens, _record = _app(tmp_path)
    _install_exact_journal(app, run_id)
    app.state.operator_api.accepting = lambda: False

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=_request(run_id),
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        503,
        "activity_publish_unavailable",
    )
    with pytest.raises(Exception, match="run_unknown"):
        app.state.activity_store.read(run_id)


def test_activity_is_installed_only_after_durable_append_finishes(tmp_path: Path) -> None:
    run_id = "run-visible-order"
    app, tokens, _record = _app(tmp_path)
    _install_exact_journal(app, run_id)
    entered = threading.Event()
    release = threading.Event()
    actual_append = app.state.activity_store.append
    installed_lengths: list[int] = []
    app.state.operator_feed.register_activity_observer(
        lambda install: installed_lengths.append(
            install.sequence_offset + len(install.added_records or install.current_records)
        )
    )
    hive_snapshot = {
        "revision": "opaque:hive-1",
        "cursor": {
            "subscriptionId": f"hive:{HIVE}",
            "producerEpoch": "hive-epoch",
            "sequence": 0,
            "observedAt": 1,
        },
    }
    feed_state = app.state.operator_feed._hive_state(HIVE)
    feed_state.snapshot = hive_snapshot
    feed_state.producer_epoch = "hive-epoch"
    feed_state.sequence = 0
    app.state.operator_sse._on_install(
        operator_feed.FeedInstall(
            hive_id=HIVE,
            previous=None,
            current=hive_snapshot,
            source_revision="opaque:hive-1",
        )
    )

    def delayed_append(identity, request, *, register_unknown=False):
        entered.set()
        release.wait(timeout=2)
        return actual_append(identity, request, register_unknown=register_unknown)

    app.state.activity_store.append = delayed_append

    async def exercise():
        async with app.router.lifespan_context(app):
            sse_client = app.state.operator_sse.subscribe(
                HIVE,
                subscription_id=f"hive:{HIVE}",
                cursor=operator_sse.EventCursor("hive-epoch", 0),
                loop=asyncio.get_running_loop(),
            )
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                post = asyncio.create_task(
                    client.post(
                        f"/api/v1/runs/{run_id}/activity",
                        headers=_authorization(tokens["publish"]),
                        json=_request(run_id),
                    )
                )
                assert await asyncio.to_thread(entered.wait, 2)
                before = await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["read"]),
                )
                assert installed_lengths == [1]
                assert app.state.operator_sse.retained_state()["events"] == 1
                release.set()
                acknowledged = await post
                after = await client.get(
                    f"/api/v1/runs/{run_id}/activity?after={before.json()['producerEpoch']}:1",
                    headers=_authorization(tokens["read"]),
                )
                sse_events = tuple(app.state.operator_sse._hives[HIVE].history)
                sse_client.close()
                return before, acknowledged, after, sse_events

    before, acknowledged, after, sse_events = asyncio.run(exercise())
    assert before.json()["sequence"] == 1
    assert acknowledged.status_code == 201
    assert after.json()["sequence"] == 2
    assert (after.json()["kind"], after.json()["baseSequence"]) == ("delta", 1)
    assert len(after.json()["activities"]) == 1
    assert after.json()["activities"][-1]["payload"]["name"] == "provider.progress"
    assert installed_lengths == [1, 2]
    assert [event.payload["payload"]["kind"] for event in sse_events] == [
        "activity",
        "activity",
    ]
    assert sse_events[-1].payload["payload"]["activity"]["payload"]["name"] == ("provider.progress")
    assert sse_events[-1].frame.startswith(b"event: operator-event\n")


def test_tight_read_limit_keeps_second_commit_visible_and_pages_restart_hydration(
    tmp_path: Path,
) -> None:
    run_id = "run-tight-limit"
    app, tokens, record = _app(tmp_path, activity_records=1)
    _install_exact_journal(app, run_id)
    first_payload = _request(run_id)
    second_payload = _request(run_id)
    second_payload["idempotencyKey"] = "baml:event-2"
    second_payload["occurredAt"] += 1
    second_payload["expiresAt"] += 1

    async def publish_both():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                first = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=first_payload,
                )
                second = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=second_payload,
                )
                visible = await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["read"]),
                )
                cursor = f"{visible.json()['producerEpoch']}:{visible.json()['sequence']}"
                visible_tail = await client.get(
                    f"/api/v1/runs/{run_id}/activity?after={cursor}",
                    headers=_authorization(tokens["read"]),
                )
                return first, second, visible, visible_tail

    first, second, visible, visible_tail = asyncio.run(publish_both())
    assert (first.status_code, second.status_code) == (201, 201)
    assert visible.status_code == 200
    assert visible.json()["sequence"] == 2
    assert [item["payload"]["name"] for item in visible.json()["activities"]] == [
        "run.created",
        "provider.progress",
    ]
    assert visible.json()["coverage"] == {
        "state": "partial",
        "detail": "durable_activity_page_truncated",
    }
    assert visible_tail.status_code == 200
    assert visible_tail.json()["baseSequence"] == 2
    assert visible_tail.json()["sequence"] == 3
    assert [item["payload"]["name"] for item in visible_tail.json()["activities"]] == [
        "provider.progress"
    ]

    restarted = _build_product_application(
        runtime=host_daemon.DaemonRuntime(),
        control_record=record,
        cfg={"managed_repos": []},
        settings=_settings(
            Path(app.state.auth_authority.path),
            activity_records=1,
        ),
    )
    _install_exact_journal(restarted, run_id)

    async def hydrate_pages():
        async with restarted.router.lifespan_context(restarted):
            transport = httpx.ASGITransport(app=restarted, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                page_one = await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["read"]),
                )
                cursor = f"{page_one.json()['producerEpoch']}:{page_one.json()['sequence']}"
                page_two = await client.get(
                    f"/api/v1/runs/{run_id}/activity?after={cursor}",
                    headers=_authorization(tokens["read"]),
                )
                return page_one, page_two

    page_one, page_two = asyncio.run(hydrate_pages())
    assert page_one.status_code == 200
    assert page_one.json()["coverage"] == {
        "state": "partial",
        "detail": "durable_activity_page_truncated",
    }
    assert page_one.json()["sequence"] == 2
    assert page_two.status_code == 200
    assert page_two.json()["kind"] == "delta"
    assert page_two.json()["baseSequence"] == 2
    assert page_two.json()["sequence"] == 3
    assert len(page_two.json()["activities"]) == 1
    assert page_two.json()["coverage"] == {"state": "complete", "detail": None}


@pytest.mark.parametrize(
    "after",
    [
        "epoch:" + "9" * 5_000,
        "epoch:+1",
        "epoch:01",
        "epoch:١",
        "e" * 65 + ":1",
    ],
)
def test_activity_cursor_is_bounded_and_canonical_before_integer_conversion(
    tmp_path: Path, after: str
) -> None:
    run_id = "run-bounded-cursor"
    app, tokens, _record = _app(tmp_path)
    _install_exact_journal(app, run_id)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.get(
                    f"/api/v1/runs/{run_id}/activity",
                    params={"after": after},
                    headers=_authorization(tokens["read"]),
                )

    response = asyncio.run(exercise())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_activity_cursor"


def test_payload_path_conflict_and_unavailable_journal_never_persist(tmp_path: Path) -> None:
    run_id = "run-refuse"
    app, tokens, _record = _app(tmp_path)
    _install_exact_journal(app, run_id, unavailable=True)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                mismatch = _request("run-another")
                mismatch["idempotencyKey"] = "baml:mismatch"
                return (
                    await client.post(
                        f"/api/v1/runs/{run_id}/activity",
                        headers=_authorization(tokens["publish"]),
                        json=mismatch,
                    ),
                    await client.post(
                        f"/api/v1/runs/{run_id}/activity",
                        headers=_authorization(tokens["publish"]),
                        json=_request(run_id),
                    ),
                )

    mismatch, unavailable = asyncio.run(exercise())
    assert (mismatch.status_code, mismatch.json()["error"]["code"]) == (
        409,
        "run_id_payload_mismatch",
    )
    assert (unavailable.status_code, unavailable.json()["error"]["code"]) == (
        503,
        "activity_source_unavailable",
    )
    with pytest.raises(Exception, match="run_unknown"):
        app.state.activity_store.read(run_id)


def test_activity_specific_body_limit_precedes_json_parse(tmp_path: Path) -> None:
    run_id = "run-large"
    app, tokens, _record = _app(tmp_path, activity_bytes=1_024)
    _install_exact_journal(app, run_id)
    body = b"{" + b"x" * 1_024

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                return await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers={
                        **_authorization(tokens["publish"]),
                        "Content-Type": "application/json",
                    },
                    content=body,
                )

    response = asyncio.run(exercise())
    assert (response.status_code, response.json()["error"]["code"]) == (
        413,
        "activity_body_too_large",
    )


def test_activity_publish_uses_the_shared_token_rate_limit(tmp_path: Path) -> None:
    run_id = "run-rate"
    app, tokens, _record = _app(tmp_path, rate=1)
    _install_exact_journal(app, run_id)

    async def exercise():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                first = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=_request(run_id),
                )
                second_payload = _request(run_id)
                second_payload["idempotencyKey"] = "baml:event-2"
                second = await client.post(
                    f"/api/v1/runs/{run_id}/activity",
                    headers=_authorization(tokens["publish"]),
                    json=second_payload,
                )
                return first, second

    first, second = asyncio.run(exercise())
    assert first.status_code == 201
    assert (second.status_code, second.json()["error"]["code"]) == (
        429,
        "token_rate_limited",
    )
    assert len(app.state.activity_store.read(run_id).activities) == 1


@pytest.mark.parametrize(
    ("source", "writer"),
    [
        ("baml", "baml.provider"),
        ("hitch", "agent-hitch.direct"),
        ("beadhive", "beadhive.role"),
    ],
)
def test_cancelled_request_cannot_observe_or_ack_before_durable_append(
    tmp_path: Path, source: str, writer: str
) -> None:
    run_id = f"run-cancel-{source}"
    app, _tokens, _record = _app(tmp_path / source)
    _install_exact_journal(app, run_id, writer=writer)
    service = app.state.activity_publication
    payload = json.dumps(
        _request(run_id, source=source, writer=writer), separators=(",", ":")
    ).encode()
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    actual_append = app.state.activity_store.append

    def delayed_append(identity, request, *, register_unknown=False):
        entered.set()
        release.wait(timeout=2)
        try:
            return actual_append(identity, request, register_unknown=register_unknown)
        finally:
            completed.set()

    app.state.activity_store.append = delayed_append

    async def exercise():
        task = asyncio.create_task(service.publish(run_id, payload, _principal(source)))
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(Exception, match="run_unknown"):
            app.state.activity_store.read(run_id)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(completed.wait, 2)
        return await service.publish(run_id, payload, _principal(source))

    retry = asyncio.run(exercise())
    assert retry.status == "duplicate"
    assert len(app.state.activity_store.read(run_id).activities) == 1


def test_openapi_declares_publish_scope_and_durable_response_contract() -> None:
    from beadhive import operator_api

    document = operator_api.openapi_document()
    activity_get = document["paths"]["/api/v1/runs/{run_id}/activity"]["get"]
    after = next(item for item in activity_get["parameters"] if item["name"] == "after")
    assert after["schema"] == {
        "type": "string",
        "maxLength": 85,
        "pattern": "^[A-Za-z0-9._~-]{1,64}:(0|[1-9][0-9]{0,19})$",
    }
    post = document["paths"]["/api/v1/runs/{run_id}/activity"]["post"]
    assert post["x-beadhive-required-scope"] == "activity:publish"
    assert post["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActivityAppendRequest"
    }
    assert post["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ActivityAppendResponse"
    }
