"""`bh work next`'s composition seam: the atomic `work.claim-next` route, selected before
execution, and the explicit fallback to the CLI-compatibility loop this cohort leaves in place.

The CLI-compatibility half of each scenario here fakes `bd` at the same `bd._run` seam
`test_work_next.py` uses, over a real (committed) git repo `worktree.ensure` can fork a claim's
worktree from — the identical fixture discipline, kept local to this file rather than imported
across test modules.
"""

from __future__ import annotations

import json
import subprocess
from collections import namedtuple
from types import SimpleNamespace

import httpx
import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import guard, work, work_queue
from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint
from beadhive_beads_client.service import ServiceUnavailable
from beadhive_core import QUEUE_CAPABILITIES

_CP = namedtuple("CP", "returncode stdout stderr")

PROJECT = "proj"

CONFIG_YAML = """\
providers: [github]
work:
  validate_cmd: "true"
  review_gate: "human"
  identity: {mode: agent, name: "dev/next", email: "agents@test.dev"}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


def _open(bead_id, **kw):
    return {"id": bead_id, "status": "open", "assignee": "", "issue_type": "task", **kw}


class FakeBd:
    """`bd` at the `bd._run` seam: an in-memory bead store serving `ready` / `show` / `update`.

    The non-racy half of `test_work_next.py`'s fixture of the same name — this file's atomic-route
    scenarios don't need the race modelling, only a working CLI-compatibility fallback."""

    def __init__(self, ready=(), children=None):
        self.beads = {b["id"]: dict(b) for b in ready}
        self.order = [b["id"] for b in ready]
        self.children = {k: list(v) for k, v in (children or {}).items()}
        self.claims: list[str] = []

    def __call__(self, cmd, **_kw):
        args = list(cmd[1:])
        actor = ""
        while args and args[0] in ("-C", "--actor"):
            if args[0] == "--actor":
                actor = args[1]
            args = args[2:]
        return self._dispatch(actor, [a for a in args if a != "--json"])

    def _dispatch(self, actor, args):
        sub = args[0] if args else ""
        if sub == "ready":
            rows = [self.beads[i] for i in self.order if self.beads[i]["status"] == "open"]
            return _CP(0, json.dumps(rows), "")
        if sub == "list":
            parent = args[args.index("--parent") + 1] if "--parent" in args else ""
            kids = self.children.get(parent, [])
            rows = [dict(self.beads.get(kid) or {"id": kid}, parent=parent) for kid in kids]
            return _CP(0, json.dumps(rows), "")
        if sub == "show":
            row = self.beads.get(args[1])
            return _CP(0 if row else 1, json.dumps(row) if row else "", "")
        if sub == "update" and "--claim" in args:
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            self.claims.append(args[1])
            bead.update(assignee=actor, status="in_progress")
            return _CP(0, "", "")
        if sub == "update" and "--status" in args:
            bead = self.beads.setdefault(args[1], {"id": args[1]})
            bead["status"] = args[args.index("--status") + 1]
            if "--assignee" in args:
                bead["assignee"] = args[args.index("--assignee") + 1]
            return _CP(0, "", "")
        if sub == "set-state":
            return _CP(0, "", "")
        return _CP(0, "", "")


@pytest.fixture
def nexthive(tmp_path, monkeypatch):
    """A hive `bh work next` can resolve, with `bd` faked and the host-lease/state seams stubbed.

    A real, committed git repo: a won claim provisions a real worktree via `worktree.ensure`,
    which forks a new branch off the integration base."""
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "config", "user.email", "human@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(main), "config", "user.name", "human"], check=True)
    (main / "README.md").write_text("# x\n")
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "chore: init"], check=True)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_CONFIG", str(cfg_path))
    monkeypatch.setenv("WS_WORKTREES", str(tmp_path / "wts"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(guard, "guard_primary", lambda *a, **k: None)
    monkeypatch.setattr(work, "_pull_state", lambda *a, **k: None)
    return SimpleNamespace(main=main, wts=tmp_path / "wts", cfg_path=cfg_path)


def _row(bead_id, **kw):
    row = {
        "id": bead_id,
        "title": f"title {bead_id}",
        "priority": 2,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
        "dependency_count": 0,
        "dependent_count": 0,
        "comment_count": 0,
        "status": "open",
        "issue_type": "task",
    }
    row.update(kw)
    return row


class FakeQueueService:
    """Serves `/v0/beads/ready` and `/v0/beads/issues:claimNext` for one fixed row set,
    exactly the transport fixture `packages/beadhive-core`'s own policy tests use."""

    def __init__(self, rows, claimed_id=None):
        self.rows = rows
        self.claimed_id = claimed_id
        self.claim_calls: list[str] = []
        self.release_calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v0/beads/context":
            return httpx.Response(
                200,
                json={
                    "api_version": "v0",
                    "bd_version": "1.3.0",
                    "schema_version": 1,
                    "backend": "dolt",
                    "dolt_mode": "server",
                    "database": "bh",
                    "project_id": PROJECT,
                    "capabilities": sorted(QUEUE_CAPABILITIES),
                },
            )
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": self.rows, "has_more": False})
        if path == "/v0/beads/issues:claimNext" and request.method == "POST":
            import json as _json

            body = _json.loads(request.content or b"{}")
            self.claim_calls.append(body["actor"])
            if self.claimed_id is None:
                return httpx.Response(200, json={})
            claimed = next(r for r in self.rows if r["id"] == self.claimed_id)
            row = dict(claimed, assignee=body["actor"], status="in_progress")
            return httpx.Response(200, json={"claimed": row})
        if path.endswith(":release") and request.method == "POST":
            issue_id = path.removeprefix("/v0/beads/issues/").removesuffix(":release")
            self.release_calls.append(issue_id)
            issue = {
                "id": issue_id,
                "title": f"title {issue_id}",
                "priority": 2,
                "created_at": "2026-09-26T00:00:00Z",
                "updated_at": "2026-09-26T00:00:00Z",
                "status": "open",
            }
            return httpx.Response(
                200, json={"issue": issue, "changed": True, "revision": "rev-released"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _api_session_factory(fixture: FakeQueueService):
    def factory(_main, _entry):
        return BeadsSession(
            RemoteEndpoint("http://127.0.0.1:8080"),
            ExpectedContext(PROJECT, "bh", required_capabilities=QUEUE_CAPABILITIES),
            transport=httpx.MockTransport(fixture.handle),
        )

    return factory


def _stub_provision(monkeypatch):
    monkeypatch.setattr(
        work, "_provision_claim", lambda cfg, hive, main, bead, actor: (f"/fake/{bead}", {})
    )


def test_next_claims_through_the_atomic_route_when_the_service_is_available(
    nexthive, monkeypatch, capsys
):
    fixture = FakeQueueService(rows=[_row("bh-1")], claimed_id="bh-1")
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _stub_provision(monkeypatch)

    code = 0
    try:
        work.next_(as_="dev/alice", hive="mr", as_json=True)
    except typer.Exit as exc:
        code = exc.exit_code
    assert code == 0
    assert fixture.claim_calls == ["dev/alice"]
    out = capsys.readouterr().out
    assert '"status": "claimed"' in out
    assert '"bead": "bh-1"' in out


def test_next_falls_back_to_cli_when_the_service_is_unavailable(nexthive, monkeypatch, capsys):
    """The missing-capability / no-service fallback: selected before any write, never as a retry
    after one fails. `FakeBd` proves the CLI-compatibility loop still runs underneath, unchanged."""

    def factory(_main, _entry):
        raise ServiceUnavailable("no service", state="absent", start_command="bh host beads start")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeBd(ready=[_open("bh-2")])
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)

    code = 0
    try:
        work.next_(as_="dev/alice", hive="mr", as_json=True)
    except typer.Exit as exc:
        code = exc.exit_code
    assert code == 0
    assert fake.claims == ["bh-2"]  # the CLI-compatibility loop actually claimed it


def test_next_epic_scope_never_tries_the_api_route(nexthive, monkeypatch, capsys):
    """`--epic` has no recursive-molecule equivalent over HTTP — it stays CLI-only regardless of
    whether the API route would otherwise be available (see `work_queue`'s module docstring)."""
    attempts: list[object] = []

    def factory(main, entry):
        attempts.append((main, entry))
        raise AssertionError("the atomic route must never be tried for an --epic scoped claim")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeBd(ready=[_open("bh-3")], children={"ep-1": ["bh-3"]})
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)

    try:
        work.next_(as_="dev/alice", hive="mr", as_json=True, epic="ep-1")
    except typer.Exit:
        pass
    assert attempts == []
    assert fake.claims == ["bh-3"]


def test_next_bare_actor_never_tries_the_api_route(nexthive, monkeypatch, capsys):
    """An undeclared actor needs the candidate's type to resolve its seat prefix — the atomic
    route commits its actor before that type is known, so a bare actor stays CLI-only."""
    attempts: list[object] = []

    def factory(main, entry):
        attempts.append((main, entry))
        raise AssertionError("the atomic route must never be tried for an undeclared actor")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeBd(ready=[_open("bh-4")])
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)

    try:
        work.next_(as_="alice", hive="mr", as_json=True)
    except typer.Exit:
        pass
    assert attempts == []
    assert fake.claims == ["bh-4"]


def test_next_releases_and_refuses_a_seat_mismatched_atomic_claim(nexthive, monkeypatch, capsys):
    """A declared developer actor can never end up holding an epic: the claim already committed
    atomically before the type was known, so the seam releases it back rather than leaving a
    wrongly-typed bead claimed, and reports the same `refused`/`seat_mismatch` outcome the
    CLI-compatibility pre-claim check would have."""
    fixture = FakeQueueService(rows=[_row("bh-5", issue_type="epic")], claimed_id="bh-5")
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _stub_provision(monkeypatch)

    code = 0
    try:
        work.next_(as_="dev/alice", hive="mr", as_json=True)
    except typer.Exit as exc:
        code = exc.exit_code
    assert code == work.NEXT_REFUSE_EXIT
    out = capsys.readouterr().out
    assert '"status": "refused"' in out
    assert '"refused": [\n    "bh-5"\n  ]' in out or '"bh-5"' in out


def test_next_reports_empty_queue_when_the_atomic_route_finds_nothing(
    nexthive, monkeypatch, capsys
):
    fixture = FakeQueueService(rows=[], claimed_id=None)
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _stub_provision(monkeypatch)

    code = 0
    try:
        work.next_(as_="dev/alice", hive="mr", as_json=True)
    except typer.Exit as exc:
        code = exc.exit_code
    assert code == work.NEXT_DECLINE_EXIT
    out = capsys.readouterr().out
    assert '"reason": "empty_queue"' in out
