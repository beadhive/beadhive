"""`ws work ready|issue|list|show` — the first-class bead reads that replace `ws bd` in the loops.

The forward verbs stream `bd`'s bytes through verbatim, so the coordinator loop's consumed shapes
(`ws bd ready --json`, `ws bd show <id> --json`) stay stable once the bd passthrough is gated off.
The test seam mirrors the rest of the suite: patch the one `ws.work.run` symbol with a fake `bd`,
drive the verbs through Typer's CliRunner, and assert the forwarded argv, the byte-identical
output, and the propagated exit code. `ws work show`'s gates section (bh-i371) is driven through
the same seam with the git producers faked.

WORKSPACE ISOLATION (bh-p76tk.1): `bh work ready --json` now composes a Beads session through
`registry.entry_for_dir`'s AMBIENT `cwd` resolution (`work_reads.ready_via_api` /
`work_queue.open_ready`, mirroring `work_queue.claim_next`'s existing seam for `bh work next`).
`_isolate_ambient_hive_resolution` below (autouse for this whole module) is the guard that keeps
every test in this file from ever reaching this shared host's real, live hive service through that
resolution — see its own docstring and `test_ambient_hive_resolution_reports_no_hive_once_isolated`
for the measured hazard it closes.
"""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from beadhive import config as config_mod
from beadhive import identity as identity_mod
from beadhive import registry as registry_mod
from beadhive import work, work_logic, work_queue, work_reads
from beadhive import worktree as worktree_mod
from beadhive_core import QUEUE_CAPABILITIES

_CP = namedtuple("CP", "returncode stdout stderr")

PROJECT = "proj"


@pytest.fixture(autouse=True)
def _isolate_ambient_hive_resolution(tmp_path, monkeypatch):
    """Close the ambient `cwd` -> real-hive resolution hazard bh-mu5yb.1 flagged and this bead
    (bh-p76tk.1) closes: `ready()` now unconditionally computes `registry.entry_for_dir(cfg, cwd)`
    for its `--json` composition, and that resolution does NOT go through `config.load` (faking it
    to `{}`, as every test here already does, doesn't touch this at all) — it walks `cwd`'s OWN
    path segments two different ways.

    Measured directly against THIS hive, not asserted from prose: a pytest process's real cwd
    running from inside a bead worktree (`.../bh-worktrees/github/beadhive/beadhive/<bead>`, the
    shared host's own default ephemeral-worktrees-root layout — see
    `config_paths.worktrees_root`) makes `registry.current_hive`'s shadow-root reverse-mapping
    branch resolve the REAL `github/beadhive/beadhive` hive from those path segments alone, even
    with `$GIT_WORKSPACE` faked — because that branch keys off `config.worktrees_root()`, not
    `$GIT_WORKSPACE`. That hive has a genuinely running, `bh host beads status`-verified `bd
    serve` on this shared host at times (bh-mu5yb.1's own notes) — a composed session reaching it
    from a test would be a real read against real production data, not a hypothetical.

    `$BH_WORKTREES` (the canonical override; `$WS_WORKTREES` is its deprecated alias, deliberately
    left alone here) is the one lever that closes the shadow-root branch too: pointed at a scratch
    `tmp_path` unrelated to this process's real cwd, `config.worktrees_root()` no longer matches
    it, so both resolvers report "cwd is a hive nowhere" (``None``) — the same honest answer a
    genuinely unmanaged process gets. `identity.workspace_identity` is process-lifetime `@cache`d
    (bh-z31lc) and keyed on the `cwd` argument, so it is cleared before AND after every test here,
    the same discipline `tests/stateful_fixtures.py`'s `_clear_git_fact_caches` already applies
    suite-wide for the bare-cwd form.
    """
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "git-workspace"))
    monkeypatch.setenv("BH_WORKTREES", str(tmp_path / "worktrees"))
    identity_mod.workspace_identity.cache_clear()
    yield
    identity_mod.workspace_identity.cache_clear()


def test_ambient_hive_resolution_reports_no_hive_once_isolated():
    """The guard test (bh-p76tk.1 acceptance criterion 1): proves the isolation fixture above
    actually works, rather than asserting it in prose only. Resolved from THIS test process's
    real, unmodified cwd — this bead's own live worktree, the exact shape that otherwise
    reverse-maps to the real `github/beadhive/beadhive` hive (see the fixture's docstring) —
    `current_hive`/`entry_for_dir` must both report "no hive here" once isolated: never the real
    hive's triplet, which is the one answer that would let a composed session reach this shared
    host's live `bd serve`."""
    assert registry_mod.current_hive({}) is None
    assert registry_mod.entry_for_dir({}, Path.cwd()) is None


class FakeReadBd:
    """Records every argv `ws work` forwards and returns a canned bd result (ignoring capture,
    exactly like the FakeBd in test_work — so `_forward_read`'s capture-then-write is exercised)."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        return _CP(self._returncode, self._stdout, self._stderr)

    @property
    def last(self) -> list[str]:
        return self.calls[-1]


def _run(monkeypatch, fake, argv):
    """Invoke the `ws work` sub-app with a faked bd + a no-op config (hive resolves to cwd)."""
    monkeypatch.setattr(work.bd, "_run", fake)
    monkeypatch.setattr(work.config, "load", lambda: {})
    return CliRunner().invoke(work.app, argv)


# ---- ready ------------------------------------------------------------------


def test_ready_forwards_json_shape_unchanged(monkeypatch):
    payload = '[{"id": "mr-1", "status": "open", "labels": ["model:opus"]}]\n'
    fake = FakeReadBd(stdout=payload)
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    # forwards `bd -C <cwd> ready --json` (the `-C` scopes the DB; the passthrough runs the same
    # `bd ready --json` in-cwd — identical output either way).
    assert fake.last[0] == "bd"
    assert fake.last[-2:] == ["ready", "--json"]
    # output is byte-identical to bd's — the coordinator loop parses the same shape it does today.
    assert res.stdout == payload
    assert json.loads(res.stdout)[0]["id"] == "mr-1"


def test_ready_passes_gated_through(monkeypatch):
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--gated", "--json"])

    assert res.exit_code == 0
    # extra bd flags (unknown to typer) ride through in order onto `bd ready`.
    assert fake.last[-3:] == ["ready", "--gated", "--json"]


def _opt_into_release(monkeypatch, *, estimator="file-overlap"):
    """Opt the hive into release start-gating for `ws work ready` (bh-k2j8.6)."""
    monkeypatch.setattr(
        work.config,
        "release_value",
        lambda cfg, entry, key, default=None: "stable-versioning" if key == "strategy" else default,
    )
    monkeypatch.setattr(work.config, "release_conflict_estimator", lambda cfg, entry: estimator)


def test_ready_json_annotates_deferred_when_release_strategy_set(monkeypatch):
    # Opted in: `mr-2` shares src/x.py with `mr-1` ranked ahead of it in the ready queue → deferred.
    beads = [
        {"id": "mr-1", "status": "open", "labels": ["path:src/x.py"]},
        {"id": "mr-2", "status": "open", "labels": ["path:src/x.py"]},
    ]
    fake = FakeReadBd(stdout=json.dumps(beads))
    _opt_into_release(monkeypatch)
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    marks = {b["id"]: b["deferred"] for b in json.loads(res.stdout)}
    assert marks == {"mr-1": False, "mr-2": True}  # head-of-queue startable, overlapper deferred


def test_ready_gated_view_is_not_start_gated(monkeypatch):
    # `--gated` is the merger's scorer-sorted view (bh-k2j8.7) — start-gating leaves it alone: the
    # same `release.strategy` opt-in re-sequences `--gated` via the release scorer (formatting may
    # differ — see test_ready_gated_sorted_by_strategy_json), but never annotates `deferred`.
    beads = [{"id": "mr-1", "status": "open", "labels": ["path:src/x.py"]}]
    payload = json.dumps(beads)
    fake = FakeReadBd(stdout=payload)
    _opt_into_release(monkeypatch)
    monkeypatch.setattr(work.config, "release_fix_churn_budget", lambda cfg, entry: 3)
    res = _run(monkeypatch, fake, ["ready", "--gated", "--json"])

    assert res.exit_code == 0
    assert json.loads(res.stdout) == beads  # same bead set — start-gating never touches --gated
    assert "deferred" not in json.loads(res.stdout)[0]


# ---- ready: truncation is never silent (bh-i0p1.2) ---------------------------
#
# bd's own default cap is 100 (`bd ready --help`); above it bd already says so — inside the
# table's own stdout footer for a plain render, on bd's OWN stderr for --json. These pin: the
# table footer gets mirrored onto stderr too (survives a `| grep` of stdout that would otherwise
# throw it away), a truncated --json read exits READY_TRUNCATED_EXIT instead of 0 (a caller
# checking $? can tell), an explicit -n/--limit is never second-guessed, and a narrowed read
# (any filter flag) is auto-widened to `-n 0` so it can't truncate at all.

_SHOWING_TABLE = "Showing 100 of 216 ready issues. Use -n to show more.\n"
_SHOWING_JSON_STDERR = (
    "Showing 100 of 216 ready issues. Use --limit 0 for all, or --limit N to raise the cap.\n"
)


def test_ready_table_truncated_mirrors_showing_line_to_stderr(monkeypatch):
    table = f"○ mr-1 open\n\n{_SHOWING_TABLE}"
    fake = FakeReadBd(stdout=table)
    res = _run(monkeypatch, fake, ["ready"])

    assert res.exit_code == 0  # table mode: exit code is never touched
    assert res.stdout == table  # byte-identical forward, untouched
    assert "Showing 100 of 216 ready issues" in res.stderr


def test_ready_table_not_truncated_no_stderr_noise(monkeypatch):
    table = "○ mr-1 open\n\nReady: 1 issue\n"
    fake = FakeReadBd(stdout=table)
    res = _run(monkeypatch, fake, ["ready"])

    assert res.exit_code == 0
    assert res.stderr == ""


def test_ready_json_truncated_exits_distinct_code(monkeypatch):
    """bd already writes "Showing X of Y" to ITS OWN stderr for --json (never mixed into the
    array); a truncated default-capped read now exits READY_TRUNCATED_EXIT instead of 0 so a
    caller checking $? — not just stdout bytes — can tell a partial read from a complete one."""
    payload = json.dumps([{"id": "mr-1"}])
    fake = FakeReadBd(stdout=payload, stderr=_SHOWING_JSON_STDERR)
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == work.READY_TRUNCATED_EXIT
    assert res.stdout == payload  # JSON bytes untouched — shape stability holds
    assert res.stderr == _SHOWING_JSON_STDERR  # bd's own message forwarded, nothing added


def test_ready_json_not_truncated_exits_zero(monkeypatch):
    payload = json.dumps([{"id": "mr-1"}])
    fake = FakeReadBd(stdout=payload, stderr="")
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0


def test_ready_explicit_limit_never_flagged_truncated(monkeypatch):
    """An explicit -n/--limit is the caller's own deliberate cap — even when bd's own output
    happens to carry the "Showing X of Y" text, it is never second-guessed."""
    payload = json.dumps([{"id": "mr-1"}])
    fake = FakeReadBd(stdout=payload, stderr=_SHOWING_JSON_STDERR)
    res = _run(monkeypatch, fake, ["ready", "--json", "-n", "5"])

    assert res.exit_code == 0
    assert fake.last[-4:] == ["ready", "--json", "-n", "5"]


def test_ready_narrowed_query_widened_to_unbounded(monkeypatch):
    """A narrowed read (any filter flag) with no explicit -n/--limit is widened to `-n 0` before
    being forwarded — a narrow question gets a complete answer, never a silently-capped one."""
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--json", "--label", "component:cli"])

    assert res.exit_code == 0
    assert fake.last[-2:] == ["-n", "0"]


def test_ready_unfiltered_query_not_widened(monkeypatch):
    """No filter flags, no explicit -n → forwarded verbatim (bd's own default cap applies) — an
    unfiltered listing is deliberately NOT auto-widened (raising its cap would just move the
    cliff, not remove it)."""
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    assert fake.last[-2:] == ["ready", "--json"]


def test_ready_narrowed_query_with_explicit_limit_not_widened(monkeypatch):
    """A narrowing flag PLUS an explicit -n/--limit respects the caller's own explicit cap over
    the auto-widen default."""
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--json", "--label", "component:cli", "-n", "5"])

    assert res.exit_code == 0
    assert fake.last[-6:] == ["ready", "--json", "--label", "component:cli", "-n", "5"]


def test_ready_json_start_gated_truncated_exits_distinct_code(monkeypatch):
    """The release start-gate annotator (bh-k2j8.6) re-serializes the array but forwards bd's
    stderr unchanged — a truncated default-capped read gets the same READY_TRUNCATED_EXIT
    treatment as the plain forward path."""
    beads = [{"id": "mr-1", "status": "open", "labels": []}]
    fake = FakeReadBd(stdout=json.dumps(beads), stderr=_SHOWING_JSON_STDERR)
    _opt_into_release(monkeypatch)
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == work.READY_TRUNCATED_EXIT
    assert json.loads(res.stdout) == [{**beads[0], "deferred": False}]


# ---- ready truncation helpers (pure, no CLI machinery) -----------------------


def test_widen_narrowed_ready_args_appends_unbounded_limit():
    assert work._widen_narrowed_ready_args(["--label", "component:cli"]) == [
        "--label",
        "component:cli",
        "-n",
        "0",
    ]


def test_widen_narrowed_ready_args_leaves_unfiltered_args_alone():
    assert work._widen_narrowed_ready_args(["--json"]) == ["--json"]


def test_widen_narrowed_ready_args_respects_explicit_limit_form():
    # both `-n` and the long `--limit` form (and a `--flag=value` spelling) count as explicit.
    assert work._widen_narrowed_ready_args(["--label", "x", "-n", "3"]) == [
        "--label",
        "x",
        "-n",
        "3",
    ]
    assert work._widen_narrowed_ready_args(["--label", "x", "--limit=3"]) == [
        "--label",
        "x",
        "--limit=3",
    ]


def test_ready_has_flag_strips_equals_form():
    assert work._ready_has_flag(["--limit=0"], work._READY_LIMIT_FLAGS) is True
    assert work._ready_has_flag(["--limit"], work._READY_LIMIT_FLAGS) is True
    assert work._ready_has_flag(["--label"], work._READY_LIMIT_FLAGS) is False


# ---- ready --json: the work.ready.list API route (bh-p76tk.1) ---------------------------------
#
# Finishes bh-mu5yb.1's byte-proven-but-unwired API surface: an UNBOUNDED `--json` read (explicit
# `--limit 0`, or any narrowing flag auto-widened to it) selects `QueueCommands.list_ready` before
# execution; every other shape (a capped read, `--mol`/`--mol-type`, human/non-JSON, `--gated`)
# stays the named CLI-compatibility forward unconditionally — see `work_reads`'s module docstring.
# `_isolate_ambient_hive_resolution` (autouse, top of file) is what keeps the FakeReadBd-only
# tests above honest: without it, the composed session could resolve this shared host's real hive.


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


class FakeReadyService:
    """Serves `/v0/beads/ready`, the same transport-fixture shape `test_work_queue.py`'s
    `FakeQueueService` uses, narrowed to just what `list_ready` needs here."""

    def __init__(self, rows):
        self.rows = rows
        self.ready_calls: list[dict] = []

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
            self.ready_calls.append(dict(request.url.params))
            return httpx.Response(200, json={"items": self.rows, "has_more": False})
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _api_session_factory(fixture: FakeReadyService):
    from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint

    def factory(_main, _entry):
        return BeadsSession(
            RemoteEndpoint("http://127.0.0.1:8080"),
            ExpectedContext(PROJECT, "bh", required_capabilities=QUEUE_CAPABILITIES),
            transport=httpx.MockTransport(fixture.handle),
        )

    return factory


def _forbidden_bd(*_a, **_kw):
    raise AssertionError("bd must never be invoked once the API route is selected")


def test_ready_json_unbounded_routes_through_the_api(monkeypatch):
    """The plain case bh-mu5yb.1 byte-proved but did not wire: `--json --limit 0`, no narrowing,
    selects the API route and emits `to_bd_json`'s byte-identical encoding — never touching `bd`
    at all."""
    fixture = FakeReadyService(rows=[_row("bh-1"), _row("bh-2")])
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    monkeypatch.setattr(work.bd, "_run", _forbidden_bd)
    monkeypatch.setattr(work.config, "load", lambda: {})
    res = CliRunner().invoke(work.app, ["ready", "--json", "--limit", "0"])

    assert res.exit_code == 0
    assert [row["id"] for row in json.loads(res.stdout)] == ["bh-1", "bh-2"]
    # the FIRST call is the session's own negotiation probe (`list_ready(limit=1)`, see
    # `BeadsSession._negotiate`) — the actual op is the LAST call, asking for the unbounded page.
    assert fixture.ready_calls[-1]["limit"] == "0"


def test_ready_json_narrowed_auto_widened_routes_through_the_api(monkeypatch):
    """A narrowing flag with no explicit limit is auto-widened to `-n 0` (bh-i0p1.2) — the
    widened, now-unbounded read also qualifies for the API route, with the narrowing flag
    forwarded onto the wire under its typed name."""
    fixture = FakeReadyService(rows=[_row("bh-3", labels=["component:cli"])])
    monkeypatch.setattr(work_queue, "session_factory", _api_session_factory(fixture))
    monkeypatch.setattr(work.bd, "_run", _forbidden_bd)
    monkeypatch.setattr(work.config, "load", lambda: {})
    res = CliRunner().invoke(work.app, ["ready", "--json", "--label", "component:cli"])

    assert res.exit_code == 0
    assert [row["id"] for row in json.loads(res.stdout)] == ["bh-3"]
    assert fixture.ready_calls[-1]["label"] == "component:cli"
    assert fixture.ready_calls[-1]["limit"] == "0"


def test_ready_json_falls_back_to_cli_when_the_service_is_unavailable(monkeypatch):
    """The missing-capability / no-service fallback, selected before any write, never as a
    retry — mirrors `test_work_queue.py`'s
    `test_next_falls_back_to_cli_when_the_service_is_unavailable`."""
    from beadhive_beads_client.service import ServiceUnavailable

    def factory(_main, _entry):
        raise ServiceUnavailable("no service", state="absent", start_command="bh host beads start")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--json", "--limit", "0"])

    assert res.exit_code == 0
    assert fake.last[-4:] == ["ready", "--json", "--limit", "0"]  # CLI-compatibility forward ran


def test_ready_json_mol_flag_never_tries_the_api_route(monkeypatch):
    """`--mol`/`--mol-type` have no HTTP equivalent (re-verified, bh-mu5yb.1) — stays
    CLI-compatibility unconditionally, decided before any session is even opened."""
    attempts: list[object] = []

    def factory(main, entry):
        attempts.append((main, entry))
        raise AssertionError("the API route must never be tried for --mol narrowing")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--json", "--mol", "mr-epic"])

    assert res.exit_code == 0
    assert attempts == []
    # `--mol` is a narrowing flag (`READY_NARROWING_FLAGS`) with no explicit limit, so it is also
    # auto-widened to `-n 0` before the CLI-compatibility forward — orthogonal to this test's point.
    assert fake.last[-6:] == ["ready", "--json", "--mol", "mr-epic", "-n", "0"]


def test_ready_json_capped_limit_never_tries_the_api_route(monkeypatch):
    """An explicit non-zero `--limit` stays CLI-compatibility unconditionally: `ReadyPage` carries
    no total-count field to reproduce bd's own truncation notice byte-for-byte, so a capped read
    is never attempted over the API at all — see `work_reads`'s module docstring."""
    attempts: list[object] = []

    def factory(main, entry):
        attempts.append((main, entry))
        raise AssertionError("the API route must never be tried for a capped (non-zero) limit")

    monkeypatch.setattr(work_queue, "session_factory", factory)
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["ready", "--json", "--limit", "5"])

    assert res.exit_code == 0
    assert attempts == []


def test_api_ready_kwargs_maps_every_narrowing_flag():
    assert work_reads._api_ready_kwargs(
        [
            "--json",
            "-l",
            "a",
            "--label-any",
            "b",
            "--exclude-label",
            "c",
            "-t",
            "task",
            "--exclude-type",
            "gate",
            "-p",
            "1",
            "-a",
            "dev/x",
            "-u",
            "--parent",
            "mr-1",
            "--has-metadata-key",
            "k",
            "--metadata-field",
            "k=v",
            "--limit",
            "0",
        ]
    ) == {
        "label": ["a"],
        "label_any": ["b"],
        "exclude_label": ["c"],
        "type_": "task",
        "exclude_type": ["gate"],
        "priority": 1,
        "assignee": "dev/x",
        "unassigned": True,
        "parent": "mr-1",
        "has_metadata_key": "k",
        "metadata_field": ["k=v"],
        "limit": 0,
    }


def test_api_ready_kwargs_rejects_mol_and_unknown_flags():
    assert work_reads._api_ready_kwargs(["--json", "--mol", "mr-1"]) is None
    assert work_reads._api_ready_kwargs(["--json", "--mol-type", "swarm"]) is None
    assert work_reads._api_ready_kwargs(["--json", "--explain"]) is None
    assert work_reads._api_ready_kwargs(["--json", "--sort", "priority"]) is None


# ---- readiness: one molecule, including persistent gates over wisp steps ---------------------


class FakeMoleculeReadinessBd:
    """Shape-aware fake for the three reads behind ``work readiness``."""

    def __init__(self, *, blocked=(), ready=(), members=None):
        self.calls: list[list[str]] = []
        self.blocked = list(blocked)
        self.ready = list(ready)
        self.members = (
            list(members)
            if members is not None
            else [
                {
                    "id": "mr-wisp-release",
                    "title": "release",
                    "status": "open",
                    "dependency_type": "parent-child",
                }
            ]
        )

    def __call__(self, cmd, **_kw):
        argv = list(cmd)
        self.calls.append(argv)
        if "show" in argv and "--children" in argv:
            payload = {"mr-wisp-run": self.members, "schema_version": 1}
        elif "--explain" in argv:
            payload = {"ready": [], "blocked": self.blocked, "summary": {}}
        else:
            payload = self.ready
        return _CP(0, json.dumps(payload), "")


def test_molecule_readiness_m4_persistent_gate_blocks_wisp_step(monkeypatch):
    """bh-yber2.1 M4 exactly: an OPEN persistent gate blocks an ephemeral one-way-door step.

    The first-class verb must say blocked even though ``bd mol current`` / ``bd ready --mol``
    would say ready.  The command audit is as important as the render assertion: it pins the
    confirmed-correct GLOBAL ``--include-ephemeral --explain`` path and forbids both unsafe
    molecule-scoped spellings from creeping back in.
    """
    blocker = {"id": "mr-gate", "title": "releaser sign-off", "status": "open"}
    blocked = [
        {
            "id": "mr-wisp-release",
            "title": "release",
            "status": "open",
            "blocked_by": [blocker],
        }
    ]
    fake = FakeMoleculeReadinessBd(blocked=blocked)
    res = _run(monkeypatch, fake, ["readiness", "mr-wisp-run", "--json"])

    assert res.exit_code == 0
    step = json.loads(res.stdout)["steps"][0]
    assert step["readiness"] == "blocked"
    assert step["blocked_by"] == [blocker]
    commands = [call[call.index("bd") + 1 :] for call in fake.calls]
    safe_shape = ["ready", "--include-ephemeral", "--explain", "--limit", "0", "--json"]
    assert any(any(cmd[i : i + 6] == safe_shape for i in range(len(cmd) - 5)) for cmd in commands)
    assert any(
        any(
            cmd[i : i + 4] == ["show", "mr-wisp-run", "--children", "--json"]
            for i in range(len(cmd) - 3)
        )
        for cmd in commands
    )
    assert all("--mol" not in cmd for cmd in commands)
    assert all(
        not any(cmd[i : i + 2] == ["mol", "current"] for i in range(len(cmd) - 1))
        for cmd in commands
    )


def test_molecule_readiness_reports_satisfied_step_ready(monkeypatch):
    """A blocker-free step remains ready, including in an ordinary persistent molecule."""
    ready = [{"id": "mr-wisp-release", "title": "release", "status": "open"}]
    fake = FakeMoleculeReadinessBd(ready=ready)
    res = _run(monkeypatch, fake, ["readiness", "mr-wisp-run"])

    assert res.exit_code == 0
    assert "[ready] mr-wisp-release: release" in res.stdout


def test_molecule_readiness_rejects_an_unreadable_molecule(monkeypatch):
    fake = FakeReadBd(stdout="[]")
    res = _run(monkeypatch, fake, ["readiness", "missing"])

    assert res.exit_code == 1
    assert "cannot read molecule missing" in res.stderr


# ---- issue (show a single bead) ---------------------------------------------


def test_issue_forwards_show_id_json(monkeypatch):
    payload = '{"id": "mr-7", "labels": ["model:sonnet", "harness:claude"]}\n'
    fake = FakeReadBd(stdout=payload)
    res = _run(monkeypatch, fake, ["issue", "mr-7", "--json"])

    assert res.exit_code == 0
    assert fake.last[-3:] == ["show", "mr-7", "--json"]
    assert res.stdout == payload
    assert json.loads(res.stdout)["labels"] == ["model:sonnet", "harness:claude"]


# ---- list / filter ----------------------------------------------------------


def test_list_filters_by_state(monkeypatch):
    fake = FakeReadBd(stdout="[]\n")
    res = _run(monkeypatch, fake, ["list", "--status", "in_progress", "--json"])

    assert res.exit_code == 0
    assert fake.last[-4:] == ["list", "--status", "in_progress", "--json"]


# ---- exit-code propagation --------------------------------------------------


def test_read_propagates_bd_exit_code(monkeypatch):
    fake = FakeReadBd(returncode=2, stderr="boom\n")
    res = _run(monkeypatch, fake, ["issue", "missing"])

    assert res.exit_code == 2


# ---- show: gates section (bh-i371) -------------------------------------------

# One gate per kind, descriptions mirroring what the verbs stamp: kickoff (plan), review
# (submit), security: (warden), and an unstamped ad-hoc hold. The resolved kickoff gate is
# listed FIRST by bd so the open-first re-ordering is observable.
SHOW_GATES = [
    {"id": "g0", "status": "closed", "description": "blocks mr-9\n\nReason: kickoff mr-epic"},
    {"id": "g1", "status": "open", "description": "blocks mr-9\n\nReason: review cafef00d"},
    {"id": "g2", "status": "open", "description": "blocks mr-9\n\nReason: security:sast pending"},
    {"id": "g3", "status": "open", "description": "blocks mr-9\n\nReason: operator hold"},
]


def _run_show(monkeypatch, gates, argv=("show", "mr-9")):
    """Drive `ws work show` through CliRunner with the git producers faked (no repo needed);
    the same FakeReadBd serves `bd gate list --all` (the only bd read on the show path)."""
    fake = FakeReadBd(stdout=json.dumps(gates))
    monkeypatch.setattr(work.bd, "_run", fake)
    monkeypatch.setattr(work.config, "load", lambda: {})
    monkeypatch.setattr(
        worktree_mod,
        "locate",
        lambda cfg, hive, bead, **kw: (
            {"prefix": "mr"},
            Path("/fake/main"),
            Path("/fake/wt"),
            "wt/bead/issue/mr-9",
        ),
    )
    monkeypatch.setattr(worktree_mod, "integration_base", lambda entry, bead, integration: "main")
    monkeypatch.setattr(worktree_mod, "base_of", lambda entry, branch, integration: "abc1234def")
    monkeypatch.setattr(worktree_mod, "commit_rows", lambda entry, base, branch: [])
    monkeypatch.setattr(config_mod, "integration_branch", lambda cfg, entry: "main")
    monkeypatch.setattr(config_mod, "max_commits", lambda cfg, entry: 10)
    return CliRunner().invoke(work.app, list(argv))


def test_show_gates_section_renders_kind_status_reason_id(monkeypatch):
    """Every gate touching the bead renders: kind (kickoff/review/security/ad-hoc), open ○ vs
    resolved ✓, gate id, and the reason snippet — resolved gates stay visible as history."""
    res = _run_show(monkeypatch, SHOW_GATES)

    assert res.exit_code == 0
    assert "gates: 4 (3 open)" in res.stdout
    assert "○ review gate g1: review cafef00d" in res.stdout
    assert "○ security gate g2: security:sast pending" in res.stdout
    assert "○ ad-hoc gate g3: operator hold" in res.stdout
    assert "✓ kickoff gate g0: kickoff mr-epic" in res.stdout  # resolved, marked ✓


def test_show_gates_open_first_ordering(monkeypatch):
    """Open gates render before resolved ones even when bd lists the resolved gate first."""
    res = _run_show(monkeypatch, SHOW_GATES)

    out = res.stdout
    assert out.index("review gate g1") < out.index("kickoff gate g0")
    assert out.index("ad-hoc gate g3") < out.index("kickoff gate g0")


def test_show_no_gates_no_section(monkeypatch):
    """A bead no gate touches renders no gates section at all (compact, not an empty header)."""
    res = _run_show(monkeypatch, [])

    assert res.exit_code == 0
    assert "gates:" not in res.stdout


def test_show_json_carries_gate_rows_open_first(monkeypatch):
    """`show --json` exposes the same gate list under `gates` — id/kind/status/reason rows."""
    res = _run_show(monkeypatch, SHOW_GATES, argv=("show", "mr-9", "--json"))

    rows = json.loads(res.stdout)["gates"]
    assert [r["id"] for r in rows] == ["g1", "g2", "g3", "g0"]  # open first, bd order kept
    assert rows[3] == {
        "id": "g0",
        "kind": "kickoff",
        "status": "resolved",
        "reason": "kickoff mr-epic",
    }


# ---- release-hold gate classification (bh-k2j8) ------------------------------


def test_gate_kind_classifies_release_hold_marker():
    """A gate whose reason carries the `release-hold:` marker classifies as kind 'release-hold' —
    distinct from review/security/kickoff/other."""
    hold = {"id": "rh0", "description": "blocks mr-9\n\nReason: release-hold: mr-epic — held"}
    assert work_logic._gate_kind(hold) == "release-hold"
    # a plain review/kickoff gate is unaffected.
    assert work_logic._gate_kind({"description": "Reason: review cafef00d"}) == "review"
    assert work_logic._gate_kind({"description": "Reason: kickoff mr-epic"}) == "kickoff"


def test_open_gate_lines_points_release_hold_at_releaser(monkeypatch):
    """An open release-hold gate renders a refusal line pointing the merger at the releaser seat."""
    hold = {"id": "rh1", "status": "open", "description": "blocks mr-9\n\nReason: release-hold: e"}
    monkeypatch.setattr(work_logic, "_bead_gates", lambda bead, cwd: [hold])
    lines = work_logic.open_gate_lines("mr-9", Path("/fake"))
    assert len(lines) == 1
    assert "release-hold gate rh1" in lines[0]
    assert "releaser/<name>" in lines[0]


# ---- ready --gated: advisory strategy sort (bh-k2j8) -------------------------

_GATED_BEADS = (
    json.dumps(
        [
            {"id": "mr-brk", "labels": ["release:breaking"]},
            {"id": "mr-fix", "labels": ["release:fix"]},
            {"id": "mr-feat", "labels": ["release:feature", "wave:one"]},
            {"id": "mr-bare", "labels": []},
        ]
    )
    + "\n"
)


def _run_gated(monkeypatch, fake, argv, *, strategy=""):
    """Drive `work ready` with a faked bd and a chosen release.strategy (empty ⇒ unset)."""
    monkeypatch.setattr(work.bd, "_run", fake)
    monkeypatch.setattr(work.config, "load", lambda: {})
    monkeypatch.setattr(
        work.config,
        "release_value",
        lambda cfg, entry, key, default=None: strategy if key == "strategy" else default,
    )
    monkeypatch.setattr(work.config, "release_fix_churn_budget", lambda cfg, entry: 3)
    return CliRunner().invoke(work.app, argv)


def test_ready_gated_sorted_by_strategy_json(monkeypatch):
    """`ready --gated --json` under a configured strategy re-sequences the array by the scorer:
    fix, then the feature cohort, then breaking; the unclassified bead trails, never dropped."""
    fake = FakeReadBd(stdout=_GATED_BEADS)
    res = _run_gated(
        monkeypatch, fake, ["ready", "--gated", "--json"], strategy="stable-versioning"
    )

    assert res.exit_code == 0
    ids = [b["id"] for b in json.loads(res.stdout)]
    assert ids == ["mr-fix", "mr-feat", "mr-brk", "mr-bare"]


def test_ready_gated_unset_strategy_forwards_verbatim(monkeypatch):
    """With no release.strategy the gated forward is byte-verbatim — no behavior change (the sort is
    strictly opt-in, like the rest of the release layer)."""
    fake = FakeReadBd(stdout="VERBATIM TABLE\n")
    res = _run_gated(monkeypatch, fake, ["ready", "--gated"], strategy="")

    assert res.exit_code == 0
    assert res.stdout == "VERBATIM TABLE\n"
    assert fake.last[-2:] == ["ready", "--gated"]  # plain forward, no --json probe


def test_reorder_ready_lines_moves_rows_keeps_framing():
    """The table reorder re-sequences bead rows by the given id order while leaving header/footer
    and blank lines exactly where bd put them."""
    text = "○ mr-brk ● P0 breaking\n○ mr-fix ● P1 fix\n\nReady: 2 issues\n"
    out = work._reorder_ready_lines(text, ("mr-fix", "mr-brk"))
    assert out == ("○ mr-fix ● P1 fix\n○ mr-brk ● P0 breaking\n\nReady: 2 issues\n")


# ---- OTEL counters: deferred-start + conflicts-avoided (bh-k2j8.8) -----------
#
# The start-gate (`ready --json`) and merge-order scorer (`ready --gated --json`) wiring emit the
# release counters through the gated otel surface. Since the SDK extra is absent in the default
# test env, these spy on the `otel.record_*` calls directly (mirrors the module's own
# `test_flow_helpers_are_noops_when_off` no-op contract — no meter needed to assert *that a call
# happened*, only the counter helpers themselves assert instrument shape).


def test_ready_json_emits_deferred_start_counter(monkeypatch):
    """A deferred bead on `ready --json` bumps `record_deferred_start` once, tagged with the
    hive's release strategy."""
    beads = [
        {"id": "mr-1", "status": "open", "labels": ["path:src/x.py"]},
        {"id": "mr-2", "status": "open", "labels": ["path:src/x.py"]},
    ]
    fake = FakeReadBd(stdout=json.dumps(beads))
    _opt_into_release(monkeypatch)
    calls = []
    monkeypatch.setattr(work.otel, "record_deferred_start", lambda attrs: calls.append(attrs))
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    assert calls == [{"bh.release.strategy": "stable-versioning"}]


def test_ready_json_no_deferral_no_counter(monkeypatch):
    """Disjoint expected paths ⇒ nothing deferred ⇒ the counter never fires."""
    beads = [
        {"id": "mr-1", "status": "open", "labels": ["path:src/x.py"]},
        {"id": "mr-2", "status": "open", "labels": ["path:src/y.py"]},
    ]
    fake = FakeReadBd(stdout=json.dumps(beads))
    _opt_into_release(monkeypatch)
    calls = []
    monkeypatch.setattr(work.otel, "record_deferred_start", lambda attrs: calls.append(attrs))
    res = _run(monkeypatch, fake, ["ready", "--json"])

    assert res.exit_code == 0
    assert calls == []


_AVOIDED_BEADS = (
    json.dumps(
        [
            # `a`/`b` are FCFS-adjacent and share an expected path (conflict-likely): `a` is a
            # fix, `b` a breaking change. The scorer's tiering (fixes, then additive features by
            # wave, then breaking last) sequences the feature `c` between them — the pair is no
            # longer adjacent post-scorer, even at the default fix_churn_budget (only one fix,
            # well under the cap).
            {"id": "a", "labels": ["release:fix", "path:src/x.py"]},
            {"id": "b", "labels": ["release:breaking", "path:src/x.py"]},
            {"id": "c", "labels": ["release:feature", "wave:one"]},
        ]
    )
    + "\n"
)


def test_ready_gated_emits_conflict_avoided_counter(monkeypatch):
    """`ready --gated --json`'s merge-order re-sequencing separates an FCFS-adjacent
    conflict-likely pair ⇒ `record_conflict_avoided` fires once, tagged with the strategy."""
    fake = FakeReadBd(stdout=_AVOIDED_BEADS)
    calls = []
    monkeypatch.setattr(work.otel, "record_conflict_avoided", lambda attrs: calls.append(attrs))
    res = _run_gated(
        monkeypatch, fake, ["ready", "--gated", "--json"], strategy="stable-versioning"
    )

    assert res.exit_code == 0
    ids = [b["id"] for b in json.loads(res.stdout)]
    assert ids == ["a", "c", "b"]  # scorer separates the conflict-likely pair with `c`
    assert calls == [{"bh.release.strategy": "stable-versioning"}]


_NOT_AVOIDED_BEADS = (
    json.dumps(
        [
            # `a`/`b` share an expected path (conflict-likely) and are both fixes, well under the
            # default fix_churn_budget ⇒ the scorer flushes both ahead of the feature, keeping the
            # FCFS-adjacent pair adjacent post-scorer too — nothing was avoided.
            {"id": "a", "labels": ["release:fix", "path:src/x.py"]},
            {"id": "b", "labels": ["release:fix", "path:src/x.py"]},
            {"id": "c", "labels": ["release:feature", "wave:one"]},
        ]
    )
    + "\n"
)


def test_ready_gated_no_separation_no_counter(monkeypatch):
    """The scorer keeps the conflict-likely pair adjacent too ⇒ nothing was avoided ⇒ the
    counter never fires."""
    fake = FakeReadBd(stdout=_NOT_AVOIDED_BEADS)
    calls = []
    monkeypatch.setattr(work.otel, "record_conflict_avoided", lambda attrs: calls.append(attrs))
    res = _run_gated(
        monkeypatch, fake, ["ready", "--gated", "--json"], strategy="stable-versioning"
    )

    assert res.exit_code == 0
    ids = [b["id"] for b in json.loads(res.stdout)]
    assert ids == ["a", "b", "c"]  # both fixes flush ahead of the feature — pair stays adjacent
    assert calls == []
