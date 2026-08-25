"""End-to-end provider-substitution contract for state stream v1 (bh-jksq.6).

The same behavioral suite runs against two deliberately different providers:

* ``DoubleHarness`` yields canonical provider events directly.
* ``BdPollingHarness`` crosses the concrete ``BdEngine`` argv seam, a real subprocess/process
  scope, JSONL export normalization, polling recovery, and the common consumer reducer.

Revision bytes are intentionally opaque, so assertions alias tokens by identity while retaining
their reference relationships.  Everything else is compared exactly.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from beadhive import engine, state_stream, state_stream_polling
from beadhive.state_stream_process import StreamProcessScope

NOW = datetime(2026, 8, 24, tzinfo=UTC)
AS_OF = NOW.isoformat().replace("+00:00", "Z")
UNKNOWN_REVISION = "opaque::never/decode?this=true"


def _raw_issue(
    issue_id: str,
    *,
    status: str = "open",
    labels=(),
    issue_type: str = "task",
    assignee: str | None = None,
    parent_id: str | None = None,
    dependencies=(),
) -> dict:
    return {
        "_type": "issue",
        "id": issue_id,
        "title": f"Issue {issue_id}",
        "issue_type": issue_type,
        "status": status,
        "priority": 1,
        "updated_at": AS_OF,
        "labels": list(labels),
        "assignee": assignee,
        "parent_id": parent_id,
        "dependencies": list(dependencies),
    }


def _issue(
    issue_id: str,
    *,
    status: str = "open",
    hive: str = "alpha",
    issue_type: str = "task",
    assignee: str | None = None,
    parent_id: str | None = None,
    dependencies: tuple[state_stream.StreamDependency, ...] = (),
):
    return state_stream.StreamIssue(
        id=issue_id,
        hive=hive,
        issue_type=issue_type,
        status=status,
        priority="P1",
        title=f"Issue {issue_id}",
        updated_at=AS_OF,
        assignee=assignee,
        parent_id=parent_id,
        dependencies=dependencies,
    )


def _snapshot(scope, revision, issues, **operators):
    return state_stream.ProviderSnapshot(scope, revision, AS_OF, tuple(issues), **operators)


def _dependency(issue_id: str, depends_on_id: str, dep_type: str = "blocks", **provenance):
    return {
        "issue_id": issue_id,
        "depends_on_id": depends_on_id,
        "type": dep_type,
        **provenance,
    }


def _raw_gate(gate_id: str, reason: str, **overrides) -> dict:
    row = {
        "id": gate_id,
        "issue_type": "gate",
        "status": "open",
        "await_type": "human",
        "description": f"Reason: {reason}",
        "created_at": "2026-08-24T00:00:00Z",
    }
    row.update(overrides)
    return row


def _operator_state():
    """Canonical before/after state used unchanged by both provider implementations."""

    dependency_parent = state_stream.WorkDependency(
        id=state_stream.projection_id(
            "work-dependency", ("alpha", "child-a", "external-dep", "blocks")
        ),
        hive="alpha",
        issue_id="child-a",
        depends_on_id="external-dep",
        type="blocks",
        created_at="2026-08-24T00:00:00Z",
        created_by="planner/one",
    )
    dependency_gate = state_stream.WorkDependency(
        id=state_stream.projection_id(
            "work-dependency", ("alpha", "child-a", "gate-unknown", "blocks")
        ),
        hive="alpha",
        issue_id="child-a",
        depends_on_id="gate-unknown",
        type="blocks",
        created_at=None,
        created_by=None,
    )
    assignment_a = state_stream.Assignment(
        id=state_stream.projection_id("assignment", ("alpha", "child-a")),
        hive="alpha",
        issue_id="child-a",
        seat="dev/one",
    )
    assignment_b = state_stream.Assignment(
        id=state_stream.projection_id("assignment", ("alpha", "child-b")),
        hive="alpha",
        issue_id="child-b",
        seat="reviewer/two",
    )
    unknown_gate = state_stream.GateRequest(
        id=state_stream.projection_id("gate-request", ("alpha", "gate-unknown")),
        hive="alpha",
        gate_id="gate-unknown",
        blocks=("child-a",),
        gate_type="human",
        gate_kind="other",
        status="open",
        reason="alien: inspect manually",
        opened_at="2026-08-24T00:00:00Z",
        resolved_at=None,
    )
    release_gate = state_stream.GateRequest(
        id=state_stream.projection_id("gate-request", ("alpha", "gate-release")),
        hive="alpha",
        gate_id="gate-release",
        blocks=(),
        gate_type="human",
        gate_kind="other",
        status="open",
        reason="release-hold: keep stable",
        opened_at="2026-08-24T00:00:00Z",
        resolved_at=None,
    )
    root_schedule = state_stream.EpicSchedule(
        id=state_stream.projection_id("epic-schedule", ("alpha", "epic-root")),
        hive="alpha",
        epic_id="epic-root",
        groups=(),
        singletons=("child-a", "child-b", "prereq"),
        coordinators=(),
    )
    empty_schedule = state_stream.EpicSchedule(
        id=state_stream.projection_id("epic-schedule", ("alpha", "epic-empty")),
        hive="alpha",
        epic_id="epic-empty",
        groups=(),
        singletons=(),
        coordinators=(),
    )
    initial = {
        "issues": (
            _issue(
                "child-a",
                assignee="dev/one",
                parent_id="epic-root",
                dependencies=(
                    state_stream.StreamDependency("child-a", "external-dep", "blocks"),
                    state_stream.StreamDependency("child-a", "gate-unknown", "blocks"),
                ),
            ),
            _issue("child-b", assignee="reviewer/two", parent_id="epic-root"),
            _issue("epic-empty", issue_type="epic"),
            _issue("epic-root", issue_type="epic"),
            _issue("prereq", parent_id="epic-root"),
        ),
        "work_dependencies": (dependency_parent, dependency_gate),
        "gate_requests": (unknown_gate, release_gate),
        "epic_schedules": (root_schedule, empty_schedule),
        "assignments": (assignment_a, assignment_b),
    }
    current = {
        "issues": (
            _issue(
                "child-a",
                assignee="dev/three",
                parent_id="epic-root",
                dependencies=(state_stream.StreamDependency("child-a", "gate-unknown", "blocks"),),
            ),
            _issue("child-b", status="closed", parent_id="epic-root"),
            _issue("epic-root", issue_type="epic"),
            _issue("prereq", parent_id="epic-root"),
        ),
        "work_dependencies": (dependency_gate,),
        "gate_requests": (
            state_stream.GateRequest(
                **{
                    **unknown_gate.__dict__,
                    "status": "resolved",
                    "resolved_at": "2026-08-24T00:00:00Z",
                }
            ),
        ),
        "epic_schedules": (
            state_stream.EpicSchedule(
                **{
                    **root_schedule.__dict__,
                    "singletons": ("child-a", "prereq"),
                }
            ),
        ),
        "assignments": (state_stream.Assignment(**{**assignment_a.__dict__, "seat": "dev/three"}),),
    }
    return initial, current


def _operator_raw_steps():
    initial = [
        _raw_issue(
            "child-a",
            assignee="dev/one",
            parent_id="epic-root",
            dependencies=(
                _dependency(
                    "child-a",
                    "external-dep",
                    created_at="2026-08-24T00:00:00Z",
                    created_by="planner/one",
                ),
                _dependency("child-a", "gate-unknown"),
            ),
        ),
        _raw_issue("child-b", assignee="reviewer/two", parent_id="epic-root"),
        _raw_issue("epic-empty", issue_type="epic"),
        _raw_issue("epic-root", issue_type="epic"),
        _raw_issue("prereq", parent_id="epic-root"),
    ]
    current = [
        _raw_issue(
            "child-a",
            assignee="dev/three",
            parent_id="epic-root",
            dependencies=(_dependency("child-a", "gate-unknown"),),
        ),
        _raw_issue("child-b", status="closed", parent_id="epic-root"),
        _raw_issue("epic-root", issue_type="epic"),
        _raw_issue("prereq", parent_id="epic-root"),
    ]
    gates = [
        _raw_gate("gate-unknown", "alien: inspect manually"),
        _raw_gate("gate-release", "release-hold: keep stable"),
    ]
    changed_gates = [
        _raw_gate(
            "gate-unknown",
            "alien: inspect manually",
            status="closed",
            closed_at="2026-08-24T00:00:00Z",
        )
    ]
    return (initial, current), (gates, changed_gates)


def _take(provider, request, count: int):
    frames = state_stream.stream_frames(provider, request)
    return [next(frames) for _ in range(count)]


class _RevisionAliases:
    """Preserve token equality/references without asserting an adapter's token syntax."""

    def __init__(self) -> None:
        self._aliases: dict[str, str] = {}

    def __call__(self, revision: str | None) -> str | None:
        if revision is None:
            return None
        return self._aliases.setdefault(revision, f"revision-{len(self._aliases) + 1}")


def _observed(frames, aliases: _RevisionAliases):
    observed = []
    for frame in frames:
        payload = state_stream.frame_payload(frame)
        issues = payload.get("issues", payload.get("changed", []))
        observed.append(
            {
                "frame": payload["frame"],
                "scope": payload["scope"],
                "revision": aliases(payload.get("revision")),
                "since_revision": aliases(payload.get("since_revision")),
                "reason": payload.get("reason"),
                "issues": [(item["id"], item["hive"], item["status"]) for item in issues],
                "removed": payload.get("removed"),
            }
        )
    return observed


class DoubleProvider:
    name = "contract-double"

    def __init__(self, events):
        self.events = tuple(events)

    def updates(self, _request) -> Iterator[state_stream.ProviderEvent]:
        yield from self.events


class DoubleHarness:
    """Provider double with no polling, files, subprocesses, or backend knowledge."""

    name = "provider-double"

    def history_sessions(self):
        first = _snapshot("hive", "double::revision/one", [_issue("a")])
        current = _snapshot(
            "hive",
            "double::revision/two",
            [_issue("a", status="closed"), _issue("b")],
        )
        request = state_stream.StreamRequest("hive", hive="alpha")
        initial = _take(DoubleProvider([first, current]), request, 2)
        reconnect = _take(
            DoubleProvider([first, current]),
            state_stream.StreamRequest("hive", hive="alpha", since_revision="double::revision/one"),
            2,
        )
        unknown = _take(
            DoubleProvider([current]),
            state_stream.StreamRequest("hive", hive="alpha", since_revision=UNKNOWN_REVISION),
            1,
        )
        return initial, reconnect, unknown

    def resync_frames(self):
        events = [
            _snapshot("hive", "double::before", [_issue("a")]),
            state_stream.ProviderReset("hive", "adapter_error", AS_OF),
            _snapshot("hive", "double::after", [_issue("a", status="closed")]),
        ]
        return _take(DoubleProvider(events), state_stream.StreamRequest("hive", hive="alpha"), 3)

    def scope_frames(self):
        selections = {
            "factory": _issue("factory-only", hive="alpha"),
            "hub": _issue("hub-only", hive="beta"),
            "alpha": _issue("alpha-only", hive="alpha"),
            "beta": _issue("beta-only", hive="beta"),
        }
        requests = {
            "factory": state_stream.StreamRequest("factory"),
            "hub": state_stream.StreamRequest("hub"),
            "alpha": state_stream.StreamRequest("hive", hive="alpha"),
            "beta": state_stream.StreamRequest("hive", hive="beta"),
        }
        return {
            key: _take(
                DoubleProvider([_snapshot(request.scope, f"double::{key}", [selections[key]])]),
                request,
                1,
            )[0]
            for key, request in requests.items()
        }

    def operator_frames(self):
        initial, current = _operator_state()
        snapshots = (
            _snapshot("hive", "double::operators/one", **initial),
            _snapshot("hive", "double::operators/two", **current),
        )
        return _take(DoubleProvider(snapshots), state_stream.StreamRequest("hive", hive="alpha"), 2)

    def aggregate_orphan_frame(self):
        snapshot = _snapshot(
            "hub",
            "double::aggregate/orphan",
            [_issue("aggregate-issue")],
            partial=True,
            partial_reason="gate_hive_identity_unavailable",
        )
        return _take(DoubleProvider([snapshot]), state_stream.StreamRequest("hub"), 1)[0]


@dataclass
class _World:
    cfg: dict
    targets: dict[str, Path]
    state_path: Path

    def script(self, steps_by_target: dict[Path, list], gates_by_target=None) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "targets": {str(target): steps for target, steps in steps_by_target.items()},
                    "gates": {
                        str(target): steps for target, steps in (gates_by_target or {}).items()
                    },
                    "calls": {},
                    "gate_calls": {},
                }
            )
        )

    def calls(self) -> dict[str, int]:
        return json.loads(self.state_path.read_text())["calls"]

    def gate_calls(self) -> dict[str, int]:
        return json.loads(self.state_path.read_text())["gate_calls"]


class BdPollingHarness:
    """Concrete polling provider using ``BdEngine`` and a controlled bd executable."""

    name = "bd-polling"

    def __init__(self, world: _World):
        self.world = world

    def _provider(self):
        return state_stream_polling.PollingStateStreamProvider(
            self.world.cfg,
            backend=engine.BdEngine(),
            poll_interval=0,
            sleeper=lambda _seconds: None,
            now=lambda: NOW,
            process_scope=self._processes,
        )

    def __enter__(self):
        self._scope = StreamProcessScope(timeout=5, term_grace=0.1)
        self._processes = self._scope.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._scope.__exit__(exc_type, exc, tb)

    def history_sessions(self):
        hive = self.world.targets["alpha"]
        self.world.script(
            {
                hive: [
                    [_raw_issue("a")],
                    [_raw_issue("a", status="closed"), _raw_issue("b")],
                    [_raw_issue("a", status="closed"), _raw_issue("b")],
                    [_raw_issue("a", status="closed"), _raw_issue("b")],
                ]
            }
        )
        provider = self._provider()
        request = state_stream.StreamRequest("hive", hive="alpha")
        initial = _take(provider, request, 2)
        first_revision = initial[0].revision
        reconnect = _take(
            provider,
            state_stream.StreamRequest("hive", hive="alpha", since_revision=first_revision),
            2,
        )
        unknown = _take(
            provider,
            state_stream.StreamRequest("hive", hive="alpha", since_revision=UNKNOWN_REVISION),
            1,
        )
        return initial, reconnect, unknown

    def resync_frames(self):
        hive = self.world.targets["alpha"]
        self.world.script(
            {
                hive: [
                    [_raw_issue("a")],
                    {"error": "controlled backend interruption"},
                    [_raw_issue("a", status="closed")],
                ]
            }
        )
        return _take(self._provider(), state_stream.StreamRequest("hive", hive="alpha"), 3)

    def scope_frames(self):
        identity_alpha = ["provider:github", "org:beadhive", "repo:alpha"]
        identity_beta = ["provider:github", "org:beadhive", "repo:beta"]
        self.world.script(
            {
                self.world.targets["factory"]: [
                    [_raw_issue("factory-only", labels=identity_alpha)]
                ],
                self.world.targets["hub"]: [[_raw_issue("hub-only", labels=identity_beta)]],
                self.world.targets["alpha"]: [[_raw_issue("alpha-only")]],
                self.world.targets["beta"]: [[_raw_issue("beta-only")]],
            }
        )
        provider = self._provider()
        requests = {
            "factory": state_stream.StreamRequest("factory"),
            "hub": state_stream.StreamRequest("hub"),
            "alpha": state_stream.StreamRequest("hive", hive="alpha"),
            "beta": state_stream.StreamRequest("hive", hive="beta"),
        }
        frames = {key: _take(provider, request, 1)[0] for key, request in requests.items()}
        assert self.world.calls() == {
            str(self.world.targets[key]): 1 for key in ("factory", "hub", "alpha", "beta")
        }
        assert self.world.gate_calls() == {
            str(self.world.targets[key]): 1 for key in ("factory", "hub", "alpha", "beta")
        }
        return frames

    def operator_frames(self):
        hive = self.world.targets["alpha"]
        exports, gates = _operator_raw_steps()
        self.world.script({hive: list(exports)}, {hive: list(gates)})
        frames = _take(self._provider(), state_stream.StreamRequest("hive", hive="alpha"), 2)
        assert self.world.calls() == {str(hive): 2}
        assert self.world.gate_calls() == {str(hive): 2}
        return frames

    def aggregate_orphan_frame(self):
        hub = self.world.targets["hub"]
        identity = ["provider:github", "org:beadhive", "repo:alpha"]
        self.world.script(
            {hub: [[_raw_issue("aggregate-issue", labels=identity)]]},
            {hub: [[_raw_gate("orphan-gate", "kickoff bh-epic")]]},
        )
        frame = _take(self._provider(), state_stream.StreamRequest("hub"), 1)[0]
        assert self.world.calls() == {str(hub): 1}
        assert self.world.gate_calls() == {str(hub): 1}
        return frame


@pytest.fixture
def world(tmp_path, monkeypatch) -> _World:
    targets = {name: tmp_path / name for name in ("factory", "hub", "alpha", "beta")}
    for target in targets.values():
        target.mkdir()
    entries = [
        {"provider": "github", "org": "beadhive", "repo": name, "prefix": name[0]}
        for name in ("alpha", "beta")
    ]
    cfg = {"managed_repos": entries}

    monkeypatch.setattr(state_stream_polling.config, "hq_dir", lambda: targets["factory"])
    monkeypatch.setattr(state_stream_polling.config, "hub_dir", lambda: targets["hub"])
    monkeypatch.setattr(
        state_stream_polling.registry,
        "resolve_hive",
        lambda _cfg, slug: next(entry for entry in entries if entry["repo"] == slug),
    )
    monkeypatch.setattr(
        state_stream_polling.registry,
        "hive_dir",
        lambda entry: targets[entry["repo"]],
    )

    state_path = tmp_path / "bd-state.json"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "bd"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import json
            import os
            import pathlib
            import sys

            args = sys.argv[1:]
            target = args[args.index("-C") + 1]
            state_path = pathlib.Path(os.environ["BH_TEST_STREAM_STATE"])
            state = json.loads(state_path.read_text())
            if "gate" in args and "list" in args:
                state["gate_calls"][target] = state["gate_calls"].get(target, 0) + 1
                steps = state.get("gates", {}).get(target, [[]])
                index = state["gate_calls"][target] - 1
                step = steps[min(index, len(steps) - 1)]
                state_path.write_text(json.dumps(state))
                if isinstance(step, dict) and "error" in step:
                    print(step["error"], file=sys.stderr)
                    raise SystemExit(1)
                print(json.dumps(step))
                raise SystemExit(0)
            output = pathlib.Path(args[args.index("-o") + 1])
            index = state["calls"].get(target, 0)
            steps = state["targets"][target]
            step = steps[min(index, len(steps) - 1)]
            state["calls"][target] = index + 1
            state_path.write_text(json.dumps(state))
            if isinstance(step, dict) and "error" in step:
                print(step["error"], file=sys.stderr)
                raise SystemExit(1)
            output.write_text("".join(json.dumps(record) + "\\n" for record in step))
            """
        ).lstrip()
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BH_TEST_STREAM_STATE", str(state_path))
    return _World(cfg=cfg, targets=targets, state_path=state_path)


@pytest.fixture(params=("provider-double", "bd-polling"))
def harness(request, world):
    if request.param == "provider-double":
        yield DoubleHarness()
    else:
        with BdPollingHarness(world) as concrete:
            yield concrete


def test_every_provider_has_identical_snapshot_first_and_opaque_reconnect_contract(harness):
    initial, reconnect, unknown = harness.history_sessions()
    aliases = _RevisionAliases()

    assert _observed(initial, aliases) == [
        {
            "frame": "snapshot",
            "scope": "hive",
            "revision": "revision-1",
            "since_revision": None,
            "reason": "initial",
            "issues": [("a", "alpha", "open")],
            "removed": None,
        },
        {
            "frame": "delta",
            "scope": "hive",
            "revision": "revision-2",
            "since_revision": "revision-1",
            "reason": None,
            "issues": [("a", "alpha", "closed"), ("b", "alpha", "open")],
            "removed": [],
        },
    ]
    assert _observed(reconnect, aliases) == _observed(initial, aliases)
    assert _observed(unknown, aliases) == [
        {
            "frame": "snapshot",
            "scope": "hive",
            "revision": "revision-2",
            "since_revision": None,
            "reason": "initial",
            "issues": [("a", "alpha", "closed"), ("b", "alpha", "open")],
            "removed": None,
        }
    ]


def test_every_provider_has_identical_mid_session_resync_contract(harness):
    aliases = _RevisionAliases()
    frames = harness.resync_frames()

    assert _observed(frames, aliases) == [
        {
            "frame": "snapshot",
            "scope": "hive",
            "revision": "revision-1",
            "since_revision": None,
            "reason": "initial",
            "issues": [("a", "alpha", "open")],
            "removed": None,
        },
        {
            "frame": "resync",
            "scope": "hive",
            "revision": None,
            "since_revision": None,
            "reason": "adapter_error",
            "issues": [],
            "removed": None,
        },
        {
            "frame": "snapshot",
            "scope": "hive",
            "revision": "revision-2",
            "since_revision": None,
            "reason": "resync",
            "issues": [("a", "alpha", "closed")],
            "removed": None,
        },
    ]
    resync_payload = state_stream.frame_payload(frames[1])
    rebuilt_payload = state_stream.frame_payload(frames[2])
    assert not any(
        key.startswith(("work_dependencies", "gate_requests", "epic_schedules", "assignments"))
        for key in resync_payload
    )
    assert all(
        key in rebuilt_payload
        for key in ("work_dependencies", "gate_requests", "epic_schedules", "assignments")
    )


def test_every_provider_has_identical_factory_hub_and_hive_isolation(harness):
    frames = harness.scope_frames()

    assert {
        key: (
            frame.scope.value,
            [(item.id, item.hive) for item in frame.issues],
        )
        for key, frame in frames.items()
    } == {
        "factory": ("factory", [("factory-only", "alpha")]),
        "hub": ("hub", [("hub-only", "beta")]),
        "alpha": ("hive", [("alpha-only", "alpha")]),
        "beta": ("hive", [("beta-only", "beta")]),
    }


SNAPSHOT_OPERATOR_KEYS = (
    "work_dependencies",
    "gate_requests",
    "epic_schedules",
    "assignments",
)
DELTA_OPERATOR_KEYS = tuple(
    suffix for name in SNAPSHOT_OPERATOR_KEYS for suffix in (f"{name}_changed", f"{name}_removed")
)


def _assert_operator_payloads(snapshot, delta) -> None:
    assert snapshot["frame"] == "snapshot"
    assert snapshot["reason"] == "initial"
    assert all(key in snapshot and snapshot[key] for key in SNAPSHOT_OPERATOR_KEYS)
    assert delta["frame"] == "delta"
    assert all(key in delta for key in DELTA_OPERATOR_KEYS)
    assert snapshot["revision"] != delta["revision"]
    assert delta["since_revision"] == snapshot["revision"]

    expected_snapshot_ids = {
        "work_dependencies": {
            state_stream.projection_id(
                "work-dependency", ("alpha", "child-a", "external-dep", "blocks")
            ),
            state_stream.projection_id(
                "work-dependency", ("alpha", "child-a", "gate-unknown", "blocks")
            ),
        },
        "gate_requests": {
            state_stream.projection_id("gate-request", ("alpha", "gate-release")),
            state_stream.projection_id("gate-request", ("alpha", "gate-unknown")),
        },
        "epic_schedules": {
            state_stream.projection_id("epic-schedule", ("alpha", "epic-empty")),
            state_stream.projection_id("epic-schedule", ("alpha", "epic-root")),
        },
        "assignments": {
            state_stream.projection_id("assignment", ("alpha", "child-a")),
            state_stream.projection_id("assignment", ("alpha", "child-b")),
        },
    }
    for key, expected in expected_snapshot_ids.items():
        ids = [record["id"] for record in snapshot[key]]
        assert ids == sorted(ids)
        assert set(ids) == expected
        assert all("revision" not in record and "as_of" not in record for record in snapshot[key])

    gates = {record["gate_id"]: record for record in snapshot["gate_requests"]}
    assert gates["gate-unknown"]["gate_kind"] == "other"
    assert gates["gate-unknown"]["blocks"] == ["child-a"]
    assert gates["gate-release"]["gate_kind"] == "other"
    assert gates["gate-release"]["blocks"] == []
    schedules = {record["epic_id"]: record for record in snapshot["epic_schedules"]}
    assert schedules["epic-root"]["groups"] == []
    assert schedules["epic-root"]["singletons"] == ["child-a", "child-b", "prereq"]
    assert schedules["epic-empty"]["groups"] == []
    assert schedules["epic-empty"]["singletons"] == []

    assert delta["work_dependencies_changed"] == []
    assert delta["work_dependencies_removed"] == [
        state_stream.projection_id(
            "work-dependency", ("alpha", "child-a", "external-dep", "blocks")
        )
    ]
    assert [item["gate_id"] for item in delta["gate_requests_changed"]] == ["gate-unknown"]
    assert delta["gate_requests_changed"][0]["status"] == "resolved"
    assert delta["gate_requests_removed"] == [
        state_stream.projection_id("gate-request", ("alpha", "gate-release"))
    ]
    assert [item["epic_id"] for item in delta["epic_schedules_changed"]] == ["epic-root"]
    assert delta["epic_schedules_changed"][0]["singletons"] == ["child-a", "prereq"]
    assert delta["epic_schedules_removed"] == [
        state_stream.projection_id("epic-schedule", ("alpha", "epic-empty"))
    ]
    assert [item["seat"] for item in delta["assignments_changed"]] == ["dev/three"]
    assert delta["assignments_removed"] == [
        state_stream.projection_id("assignment", ("alpha", "child-b"))
    ]
    for key in (name for name in DELTA_OPERATOR_KEYS if name.endswith("_changed")):
        assert all("revision" not in record and "as_of" not in record for record in delta[key])


def _assert_operator_history(frames) -> None:
    _assert_operator_payloads(*(state_stream.frame_payload(frame) for frame in frames))


def test_every_provider_has_identical_full_operator_projection_and_delta_contract(harness):
    _assert_operator_history(harness.operator_frames())


def test_every_provider_fails_closed_for_aggregate_orphan_gate_provenance(harness):
    payload = state_stream.frame_payload(harness.aggregate_orphan_frame())

    assert payload["frame"] == "snapshot"
    assert payload["scope"] == "hub"
    assert payload["partial"] is True
    assert payload["partial_reason"] == "gate_hive_identity_unavailable"
    assert payload["gate_requests"] == []


def _atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)


def _read_frame(process: subprocess.Popen, timeout: float = 15.0) -> dict:
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise AssertionError("bh stream did not flush its next NDJSON frame")
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read()
        raise AssertionError(
            f"bh stream exited {process.poll()} before its next frame; stderr={stderr!r}"
        )
    return json.loads(line)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _wait_processes_gone(pids: list[int], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_process_alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="feature demo verifies Linux process groups")
def test_real_bh_stream_operator_feature_demo(tmp_path):
    """Bounded real command demo: seed, mutate, close the consumer, and prove clean teardown."""

    workspace = tmp_path / "workspace"
    hive = workspace / "github" / "beadhive" / "alpha"
    hive.mkdir(parents=True)
    home = tmp_path / "bh-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "managed_repos:\n"
        "  - provider: github\n"
        "    org: beadhive\n"
        "    repo: alpha\n"
        "    prefix: a\n"
        "beads:\n"
        "  engine: bd\n"
    )
    exports, gates = _operator_raw_steps()
    export_source = tmp_path / "export.json"
    gate_source = tmp_path / "gates.json"
    descendant_path = tmp_path / "descendants.log"
    ledger_path = tmp_path / "calls.log"
    _atomic_json(export_source, exports[0])
    _atomic_json(gate_source, gates[0])

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "bd"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.close(0)\n"
        "    os.close(1)\n"
        "    os.close(2)\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        "with open(os.environ['BH_DEMO_DESCENDANTS'], 'a') as stream:\n"
        "    stream.write(f'{child}\\n')\n"
        "args = sys.argv[1:]\n"
        "if 'gate' in args and 'list' in args:\n"
        "    with open(os.environ['BH_DEMO_LEDGER'], 'a') as stream:\n"
        "        stream.write('gate\\n')\n"
        "    source = pathlib.Path(os.environ['BH_DEMO_GATES'])\n"
        "    print(json.dumps(json.loads(source.read_text())))\n"
        "    raise SystemExit(0)\n"
        "with open(os.environ['BH_DEMO_LEDGER'], 'a') as stream:\n"
        "    stream.write('export\\n')\n"
        "source = pathlib.Path(os.environ['BH_DEMO_EXPORT'])\n"
        "output = pathlib.Path(args[args.index('-o') + 1])\n"
        "rows = json.loads(source.read_text())\n"
        "output.write_text(''.join(json.dumps(row) + '\\n' for row in rows))\n"
    )
    binary.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{binary_dir}{os.pathsep}{environment['PATH']}",
            "BH_HOME": str(home),
            "BH_CONFIG": str(config_path),
            "GIT_WORKSPACE": str(workspace),
            "BH_DEMO_EXPORT": str(export_source),
            "BH_DEMO_GATES": str(gate_source),
            "BH_DEMO_DESCENDANTS": str(descendant_path),
            "BH_DEMO_LEDGER": str(ledger_path),
            "OTEL_SDK_DISABLED": "true",
        }
    )
    command = Path(sys.executable).parent / "bh"
    process = subprocess.Popen(
        [str(command), "stream", "--scope", "hive", "--hive", "alpha", "--format", "ndjson"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        snapshot = _read_frame(process)
        _atomic_json(export_source, exports[1])
        gates[1][0]["closed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _atomic_json(gate_source, gates[1])
        delta = _read_frame(process)
        _assert_operator_payloads(snapshot, delta)

        # Close the consumer and force one more changed refresh.  The write must become a clean
        # command-boundary BrokenPipe exit after all successful export/gate groups are reaped.
        process.stdout.close()
        _atomic_json(export_source, [*exports[1], _raw_issue("force-broken-pipe")])
        process.wait(timeout=15)
        stderr = process.stderr.read()
        assert process.returncode == 0
        assert stderr == ""

        calls = ledger_path.read_text().splitlines()
        assert calls.count("export") == calls.count("gate")
        assert calls.count("export") >= 3
        descendant_pids = [int(value) for value in descendant_path.read_text().splitlines()]
        assert descendant_pids
        assert _wait_processes_gone(descendant_pids), (
            "real bh stream left successful backend descendants after consumer close"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
