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
import textwrap
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


def _raw_issue(issue_id: str, *, status: str = "open", labels=()) -> dict:
    return {
        "_type": "issue",
        "id": issue_id,
        "title": f"Issue {issue_id}",
        "issue_type": "task",
        "status": status,
        "priority": 1,
        "updated_at": AS_OF,
        "labels": list(labels),
        "dependencies": [],
    }


def _issue(issue_id: str, *, status: str = "open", hive: str = "alpha"):
    return state_stream.StreamIssue(
        id=issue_id,
        hive=hive,
        issue_type="task",
        status=status,
        priority="P1",
        title=f"Issue {issue_id}",
        updated_at=AS_OF,
    )


def _snapshot(scope, revision, issues):
    return state_stream.ProviderSnapshot(scope, revision, AS_OF, tuple(issues))


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


@dataclass
class _World:
    cfg: dict
    targets: dict[str, Path]
    state_path: Path

    def script(self, steps_by_target: dict[Path, list]) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "targets": {str(target): steps for target, steps in steps_by_target.items()},
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
                state_path.write_text(json.dumps(state))
                print("[]")
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

    assert _observed(harness.resync_frames(), aliases) == [
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
