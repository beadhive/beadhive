"""`beadhive.work_queue`'s composition seams: the atomic `work.claim-next` route for `bh work
next`, the epic-scoped guarded-claim route for `bh work next --epic` (bh-7ip8t), and (bh-mu5yb.1)
the `work.issue.list` children route for `bh work schedule` — all selected before execution, with
an explicit fallback to the CLI-compatibility path this cohort leaves in place.

The CLI-compatibility half of each scenario here fakes `bd` at the same `bd._run` seam
`test_work_next.py` uses, over a real (committed) git repo `worktree.ensure` can fork a claim's
worktree from — the identical fixture discipline, kept local to this file rather than imported
across test modules.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import guard, work, work_dispatch, work_queue
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
  beads: {route: "api+cli-fallback"}  # FakeBd-backed: opt into the bd route (bh-m36pc)
  identity: {mode: agent, name: "dev/next", email: "agents@test.dev"}
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
"""


def _open(bead_id, **kw):
    return {"id": bead_id, "status": "open", "assignee": "", "issue_type": "task", **kw}


class FakeBd:
    """`bd` at the `bd._run` seam: an in-memory bead store serving `ready` / `list` / `show` /
    `update --claim` (the only writes this file's CLI-compatibility fallbacks make).

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
    """Serves `/v0/beads/ready`, `/v0/beads/issues`, and `/v0/beads/issues:claimNext` for one
    fixed row set, exactly the transport fixture `packages/beadhive-core`'s own policy tests use.
    """

    def __init__(self, rows, claimed_id=None, children_rows=None, lost=()):
        self.rows = rows
        self.claimed_id = claimed_id
        self.children_rows = children_rows if children_rows is not None else []
        self.lost = set(lost)  # issue ids whose `:claim` another actor already holds (409)
        self.claim_calls: list[str] = []
        self.release_calls: list[str] = []
        self.children_calls: list[str] = []
        self.children_params: list[dict] = []
        self.issue_claims: list[tuple[str, str]] = []  # (issue id, actor) per `:claim` request

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
        if path == "/v0/beads/issues" and request.method == "GET":
            self.children_calls.append(str(request.url.params.get("parent", "")))
            self.children_params.append(dict(request.url.params))
            return httpx.Response(200, json={"items": self.children_rows, "has_more": False})
        if path == "/v0/beads/issues:claimNext" and request.method == "POST":
            import json as _json

            body = _json.loads(request.content or b"{}")
            self.claim_calls.append(body["actor"])
            if self.claimed_id is None:
                return httpx.Response(200, json={})
            claimed = next(r for r in self.rows if r["id"] == self.claimed_id)
            row = dict(claimed, assignee=body["actor"], status="in_progress")
            return httpx.Response(200, json={"claimed": row})
        if path.endswith(":claim") and request.method == "POST":
            issue_id = path.removeprefix("/v0/beads/issues/").removesuffix(":claim")
            actor = json.loads(request.content or b"{}")["actor"]
            self.issue_claims.append((issue_id, actor))
            if issue_id in self.lost:
                problem = {
                    "status": 409,
                    "title": "Conflict",
                    "code": "already_claimed",
                    "request_id": "req-lost",
                    "assignee": "dev/racer",
                }
                return httpx.Response(
                    409, json=problem, headers={"content-type": "application/problem+json"}
                )
            row = next(r for r in self.rows if r["id"] == issue_id)
            issue = dict(row, assignee=actor, status="in_progress")
            return httpx.Response(200, json={"issue": issue, "already_claimed": False})
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


def _next_json(capsys, **kwargs):
    code = 0
    try:
        work.next_(hive="mr", as_json=True, **kwargs)
    except typer.Exit as exc:
        code = exc.exit_code
    return code, json.loads(capsys.readouterr().out)


def _no_bd(monkeypatch):
    def refuse(cmd, **_kw):
        raise AssertionError(f"bd must not be reached on the --epic API route: {cmd}")

    monkeypatch.setattr(bd_mod, "_run", refuse)


def _epic_fixture(lost=()):
    """Ready front: an outsider FIRST (a scope that leaked would take it), then two molecule
    members. Membership: the two members plus a closed and an infra member (never ready) and a
    row reported with no parent edge (a dotted-prefix match, never a member)."""
    return FakeQueueService(
        rows=[_row("mr-out"), _row("mr-ep.1"), _row("mr-ep.2"), _row("mr-ep.9")],
        children_rows=[
            _row("mr-ep.1", parent="mr-ep"),
            _row("mr-ep.2", parent="mr-ep"),
            _row("mr-ep.3", parent="mr-ep", status="closed"),
            _row("mr-ep.4", parent="mr-ep", issue_type="event"),
            _row("mr-ep.9", parent="elsewhere"),
        ],
        lost=lost,
    )


def test_next_epic_scope_claims_through_the_guarded_api_route(nexthive, monkeypatch, capsys):
    """bh-7ip8t: `--epic` no longer skips the API. The scoped candidates come from ONE membership
    read (closed + infra included, narrowed to the parent edge) and the unbounded ready front,
    and the first in-scope candidate is taken through `issues.claim` — `bd` is never reached."""
    fixture = _epic_fixture()
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _no_bd(monkeypatch)
    _stub_provision(monkeypatch)

    code, payload = _next_json(capsys, as_="dev/alice", epic="mr-ep")

    assert code == 0
    assert (payload["status"], payload["bead"], payload["actor"]) == (
        "claimed",
        "mr-ep.1",
        "dev/alice",
    )
    assert fixture.issue_claims == [("mr-ep.1", "dev/alice")], "no out-of-molecule attempt"
    assert fixture.claim_calls == [], "the atomic claim-next op is not the --epic route"
    assert fixture.children_calls == ["mr-ep"]
    assert fixture.children_params[0].get("all") == "true"
    assert fixture.children_params[0].get("include_infra") == "true"


def test_next_epic_scope_resolves_a_bare_actor_s_seat_before_the_compare_and_set(
    nexthive, monkeypatch, capsys
):
    """Unlike the atomic route, the scoped route resolves the seat from the CANDIDATE's type
    before it writes, so an undeclared actor needs no CLI carve-out on it."""
    fixture = _epic_fixture()
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _no_bd(monkeypatch)
    _stub_provision(monkeypatch)

    code, payload = _next_json(capsys, as_="alice", epic="mr-ep")

    assert (code, payload["bead"], payload["actor"]) == (0, "mr-ep.1", "dev/alice")
    assert fixture.issue_claims == [("mr-ep.1", "dev/alice")]


def test_next_epic_scope_moves_past_a_lost_compare_and_set(nexthive, monkeypatch, capsys):
    """A 409 `already_claimed` is a definite lost race: the next in-scope candidate is tried."""
    fixture = _epic_fixture(lost={"mr-ep.1"})
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _no_bd(monkeypatch)
    _stub_provision(monkeypatch)

    code, payload = _next_json(capsys, as_="dev/alice", epic="mr-ep")

    assert (code, payload["bead"]) == (0, "mr-ep.2")
    assert payload["tried"] == ["mr-ep.1", "mr-ep.2"]


def test_next_epic_scope_reports_all_lost_when_every_candidate_is_taken(
    nexthive, monkeypatch, capsys
):
    fixture = _epic_fixture(lost={"mr-ep.1", "mr-ep.2"})
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    _no_bd(monkeypatch)
    _stub_provision(monkeypatch)

    code, payload = _next_json(capsys, as_="dev/alice", epic="mr-ep")

    assert code == work.NEXT_DECLINE_EXIT
    assert (payload["status"], payload["reason"]) == ("declined", "all_lost")


def test_next_epic_scope_falls_back_to_cli_when_the_service_is_unavailable(
    nexthive, monkeypatch, capsys
):
    """No capable session (and this hive opts into the `bd` route): the named CLI-compatibility
    loop runs instead, selected before any Beads read or write."""

    def factory(_main, _entry):
        raise ServiceUnavailable("no service", state="absent", start_command="bh host beads start")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeBd(ready=[_open("bh-out"), _open("bh-3")], children={"ep-1": ["bh-3"]})
    monkeypatch.setattr(bd_mod, "_run", fake)
    _stub_provision(monkeypatch)

    code, payload = _next_json(capsys, as_="dev/alice", epic="ep-1")

    assert (code, payload["bead"]) == (0, "bh-3")
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


# ---- open_children: the `bh work schedule` composition seam (bh-mu5yb.1) ---------------------


def test_open_children_routes_through_the_api_and_narrows_to_direct_edge(nexthive, monkeypatch):
    """`open_children` asks for every recursive descendant under `parent` and narrows the result
    to the direct edge locally — a grandchild the recursive fetch also returns must not leak into
    a schedule plan for the epic that isn't its direct parent."""
    fixture = FakeQueueService(
        rows=[],
        children_rows=[
            _row("ep-1.2", parent="ep-1", priority=1),
            _row("ep-1.1.1", parent="ep-1.1"),  # grandchild — excluded
            _row("ep-1.1", parent="ep-1", issue_type="epic", priority=0),
        ],
    )
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))

    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    children = work_queue.open_children(nexthive.main, entry, "ep-1")

    assert children is not None
    assert [row["id"] for row in children] == ["ep-1.2", "ep-1.1"]
    assert fixture.children_calls == ["ep-1"]


def test_open_children_falls_back_to_none_when_the_service_is_unavailable(monkeypatch):
    def factory(_main, _entry):
        raise ServiceUnavailable("no service", state="absent", start_command="bh host beads start")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    # Only a hive opted into the bd route selects it (bh-m36pc); the default fails closed.
    entry = {"prefix": "mr", "work": {"beads": {"route": "api+cli-fallback"}}}
    assert work_queue.open_children(Path("/fake/main"), entry, "ep-1") is None


def test_open_children_is_the_only_fetch_impl_schedule_payload_tries_before_bd(monkeypatch):
    """`impl_schedule_payload`'s wiring (`work_dispatch.py`) is a 3-line pre-execution selection —
    try `open_children`, fall back to `api.bd.children` only when it returns `None` — proven at the
    `open_children` layer above (both directions). This confirms the wiring calls `open_children`
    with the exact epic/entry/main it was given, so `bd` is reached only on a real `None`, without
    re-running `work.schedule_payload`'s full config/model-routing pipeline here (already covered
    by `tests/test_mcp_work_schedule_resource.py`) or adding new `beadhive.config` monkeypatch call
    sites for `tests/unit/modules/config/test_dependency_ledger.py` to track."""
    calls: list[tuple] = []

    def _open_children(main, entry, epic):
        calls.append((main, entry, epic))
        return [_row("mr-1"), _row("mr-2")]

    def _forbidden_bd_children(*_args, **_kw):
        raise AssertionError("bd.children must never be invoked when open_children succeeds")

    fake_api = SimpleNamespace(
        work_queue=SimpleNamespace(open_children=_open_children),
        bd=SimpleNamespace(children=_forbidden_bd_children),
    )
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    main = Path("/fake/main")
    children = work_dispatch._impl_schedule_children(fake_api, "mr-epic", entry, main)

    assert calls == [(main, entry, "mr-epic")]
    assert sorted(row["id"] for row in children) == ["mr-1", "mr-2"]


def test_impl_schedule_children_falls_back_to_bd_when_open_children_declines(monkeypatch):
    """The other direction: `open_children` reporting `None` (service/capability unavailable)
    selects `bd.children` — the CLI-compatibility route `impl_schedule_payload` always had."""
    bd_calls: list[tuple] = []

    def _forbidden_open_children(*_args, **_kw):
        return None

    def _bd_children(epic, main):
        bd_calls.append((epic, main))
        return [_row("mr-3")]

    fake_api = SimpleNamespace(
        work_queue=SimpleNamespace(open_children=_forbidden_open_children),
        bd=SimpleNamespace(children=_bd_children),
    )
    main = Path("/fake/main")
    children = work_dispatch._impl_schedule_children(fake_api, "mr-epic", {"prefix": "mr"}, main)

    assert bd_calls == [("mr-epic", main)]
    assert [row["id"] for row in children] == ["mr-3"]


# ---- real-service: the seat-mismatch release path (bh-mu5yb.1 closes bh-l5sxi.2's gap) --------
#
# `test_next_releases_and_refuses_a_seat_mismatched_atomic_claim` above proves the same outcome
# against a MOCK transport. This proves it against a genuine, disposable `bd serve` 1.3.0: the
# atomic claim really commits, the release really lands, and the row is really left unclaimed in
# the real store afterward — not just in a fixture's in-memory model of one.


def _bd_real(*args: str, cwd: Path) -> dict:
    result = subprocess.run(
        ["bd", *args, "--json"], cwd=cwd, check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _init_owned_scratch_hive(path: Path) -> None:
    """An OWNED-mode (`bd init --server`) scratch hive: `bd serve` refuses embedded Dolt, so the
    default `bd init` shape cannot serve HTTP at all. Mirrors
    `packages/beadhive-core/tests/test_core_queue_real_service.py`'s fixture of the same name."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "smx", "--non-interactive"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _reap_owned_scratch_hive(beads_dir: Path) -> None:
    """Terminate BOTH long-lived processes an OWNED-mode scratch hive can leave behind: the Dolt
    SQL server (`dolt-server.pid`, a bare pid) AND the per-rootDir `bd db-proxy-child` TCP proxy
    `bd init --server` also spawns (`dolt/proxy.pid`, a JSON record whose `pid` field is the one
    that matters here) — `bd`'s own child, fork+exec'd detached, with an unbounded idle timeout
    by default, so it outlives this test's own `bd serve` subprocess entirely if not reaped
    explicitly. bh-l5sxi.2's contention test leaked exactly this second process; this reaps both."""
    for pid_file, is_json in (
        (beads_dir / "dolt-server.pid", False),
        (beads_dir / "dolt" / "proxy.pid", True),
    ):
        if not pid_file.is_file():
            continue
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError, KeyError):
            text = pid_file.read_text().strip()
            pid = int(json.loads(text)["pid"]) if is_json else int(text)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)


def _free_tcp_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


@pytest.mark.real_service
def test_next_releases_and_refuses_a_seat_mismatch_against_a_real_service(tmp_path):
    """A declared `dev/` seat that atomically wins an EPIC via a genuine `bd serve` is released
    back in the SAME session and reported as refused — never left claimed — and the epic is
    genuinely unclaimed (open, unassigned) in the real store afterward.

    Opt-in via `BEADS_QUEUE_SCRATCH=1`, the same convention
    `test_core_queue_real_service.py` uses::

        BEADS_QUEUE_SCRATCH=1 \\
        uv run --locked --all-packages pytest tests/test_work_queue.py -m real_service
    """
    if not os.environ.get("BEADS_QUEUE_SCRATCH"):
        pytest.skip("set BEADS_QUEUE_SCRATCH=1 to run the real-bd scratch-hive proof")

    _init_owned_scratch_hive(tmp_path)
    try:
        port = _free_tcp_port()
        proc = subprocess.Popen(
            ["bd", "serve", "--addr", f"127.0.0.1:{port}"],
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                with contextlib.suppress(httpx.HTTPError):
                    if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                        break
                time.sleep(0.1)
            else:
                out = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"bd serve never became healthy: {out}")

            context = _bd_real("context", cwd=tmp_path)
            epic = _bd_real(
                "create",
                "--title",
                "epic under test",
                "--type",
                "epic",
                "--priority",
                "1",
                cwd=tmp_path,
            )
            epic_id = epic["id"]

            def factory(_main, _entry):
                return BeadsSession(
                    RemoteEndpoint(base),
                    ExpectedContext(
                        context["project_id"],
                        context["database"],
                        required_capabilities=QUEUE_CAPABILITIES,
                        repo_root=tmp_path,
                    ),
                )

            import beadhive.work_queue as work_queue_mod

            old_factory = work_queue_mod.session_factory
            work_queue_mod.session_factory = factory
            try:
                result = work_queue_mod.claim_next(tmp_path, {}, "dev/alice")
            finally:
                work_queue_mod.session_factory = old_factory

            assert result is not None, "a declared dev/ actor must try the atomic route"
            assert result.claimed == ""
            assert result.refused == (epic_id,)

            shown = _bd_real("show", epic_id, cwd=tmp_path)
            final = shown[0] if isinstance(shown, list) else shown
            assert final["status"] == "open"
            assert not final.get("assignee")
        finally:
            proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:
                proc.kill()
    finally:
        _reap_owned_scratch_hive(tmp_path / ".beads")
