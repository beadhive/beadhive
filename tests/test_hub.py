"""Tests for ws.hub sync/ensure_hub error handling.

The bug: `bd repo add` / `bd repo sync` ran with check=False and uncaptured output, so
re-running `ws sync` dumped bd's full 'already configured' error + usage block per hive,
while genuine failures were swallowed into a green summary. These tests pin the fixed
contract: idempotent re-adds are silent, genuine failures are surfaced (and returned),
and a missing/broken bd yields a friendly error instead of a raw traceback.
"""

from __future__ import annotations

import json
import shutil
from collections import namedtuple
from pathlib import Path

import pytest
import typer

from beadhive import bd, config, hub

Completed = namedtuple("Completed", "returncode stdout stderr")

_USAGE_DUMP = (
    "Error: failed to add repository: repository already configured: {src}\n"
    "Usage:\n  bd repo add <path> [flags]\n\nFlags:\n  -h, --help   help for add\n"
)


def _hive_cfg(*repos, bulk_sync=True):  # bh-l7sm8: ON is the default; False is now a REFUSAL
    return {
        "managed_repos": [
            {"provider": "github", "org": "a", "repo": r, "prefix": f"a-{r}"} for r in repos
        ],
        "hub": {"bulk_sync": bulk_sync},
    }


def _identify(store, project_id="fake-aggregate-id"):
    """Give a fake aggregate the `.beads/metadata.json` a real one always has."""
    (store / ".beads").mkdir(parents=True, exist_ok=True)
    (store / ".beads" / "metadata.json").write_text(json.dumps({"project_id": project_id}))
    return store


def _wire(tmp_path, monkeypatch, fake_run, *repos):
    """Point hub.sync at fake subprocesses + on-disk hive dirs for the given repo names.

    `hub.run` fakes the hub-store-only ops (repo add/remove/sync/list) hub.py still calls
    directly; `bd._run` fakes the per-hive export/bootstrap ops, which route through the
    `Engine` seam (bh-dw3e.5) and land on the SAME `bd._run` bd.py's own callers hit — same
    fake, same `cmd` shape, just a different interception point."""
    dirs = {}
    for r in repos:
        d = tmp_path / r
        (d / ".beads").mkdir(parents=True)
        dirs[r] = d
    monkeypatch.setenv("WS_HOME", str(tmp_path))  # keep metadata.invalidate off the real cache
    # The fake aggregate needs a real STORE IDENTITY: watermarks are keyed on the store's
    # `project_id` as well as its path (bh-89wxf.1), so a metadata-less hub reads as re-minted
    # and every mark is (correctly) discarded.
    _identify(tmp_path / "hub")
    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg(*repos))
    monkeypatch.setattr(hub.registry, "hive_dir", lambda e: dirs[e["repo"]])
    return dirs


def test_sync_already_configured_readd_is_silent(tmp_path, monkeypatch, capsys):
    """Re-running sync against already-configured hives prints no error/usage noise and
    still counts every hive as hydrated."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "add"]:
            return Completed(1, "", _USAGE_DUMP.format(src=cmd[-1]))
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    failed = hub.sync()
    out = capsys.readouterr()
    assert failed == []
    assert "Usage:" not in out.out + out.err
    assert "already configured" not in out.out + out.err
    assert "2 hydrated, 0 skipped" in out.out
    assert out.out.startswith("✓")


def test_sync_genuine_add_failure_surfaces(tmp_path, monkeypatch, capsys):
    """A repo add failure that is NOT 'already configured' is reported (headline only,
    no usage dump), excluded from the hydrated count, and returned by sync()."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "add"] and cmd[-1].endswith("bad"):
            err = "Error: failed to add repository: database locked\nUsage:\n  bd repo add\n"
            return Completed(1, "", err)
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "good", "bad")
    failed = hub.sync()
    out = capsys.readouterr()
    assert failed == ["a-bad"]
    assert "a-bad: bd repo add failed: Error: failed to add repository: database locked" in out.err
    assert "Usage:" not in out.err
    assert "1 hydrated" in out.out
    assert "1 failed to hydrate (a-bad)" in out.out


def test_sync_repo_sync_failure_marks_all_added_failed(tmp_path, monkeypatch, capsys):
    """If the final `bd repo sync` exits non-zero, no added hive is counted hydrated."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "sync"]:
            return Completed(1, "", "Error: sync exploded\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    failed = hub.sync()
    out = capsys.readouterr()
    assert sorted(failed) == ["a-one", "a-two"]
    assert "bd repo sync failed: Error: sync exploded" in out.err
    assert "0 hydrated" in out.out
    assert "2 failed to hydrate" in out.out


def test_sync_export_failure_warns_but_continues(tmp_path, monkeypatch, capsys):
    """A failed `bd export` warns (repo sync may still hydrate from existing JSONL) but
    doesn't fail the hive on its own."""

    def fake_run(cmd, **k):
        if len(cmd) > 3 and cmd[3] == "export":
            return Completed(1, "", "Error: export failed\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    failed = hub.sync()
    out = capsys.readouterr()
    assert failed == []
    assert "a-one: bd export failed: Error: export failed" in out.err
    assert "1 hydrated" in out.out


# ---------------------------------------------------------------------------
# Per-hive sync watermarks (bh-d5jhc.2) — skip `bd export` for an unchanged hive, and never
# advance the watermark on anything short of a confirmed `bd repo sync` hydration.
# ---------------------------------------------------------------------------


def _vc_status(commit: str):
    """A `bd vc status --json` response body, JSON-encoded on stdout."""
    import json as _json

    return _json.dumps({"branch": "main", "commit": commit, "schema_version": 1})


def test_sync_skips_export_for_a_hive_whose_watermark_matches(tmp_path, monkeypatch, capsys):
    """A hive whose `bd vc status` commit matches the stored watermark is NOT re-exported —
    `bd repo add` still runs (cheap, idempotent, self-healing), so the hive stays hydrated."""
    exported: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("same-commit"), "")
        if len(cmd) > 3 and cmd[3] == "export":
            exported.append(cmd[2])
        return Completed(0, "", "")

    dirs = _wire(tmp_path, monkeypatch, fake_run, "one")
    hub._store_watermarks(tmp_path / "hub", {"a-one": "same-commit"})

    failed = hub.sync()
    out = capsys.readouterr()

    assert failed == []
    assert exported == []  # bd export never spawned for the unchanged hive
    assert "a-one: unchanged since last sync — skipping export" in out.err
    assert "1 hydrated" in out.out
    # the watermark is unchanged, still readable next run
    assert hub._load_watermarks(tmp_path / "hub") == {"a-one": "same-commit"}
    assert dirs  # sanity: _wire actually set up the fake fleet


def test_sync_exports_a_hive_whose_watermark_differs(tmp_path, monkeypatch, capsys):
    """A hive whose current commit differs from the stored watermark IS re-exported, and the
    watermark advances to the new commit once `bd repo sync` confirms hydration."""
    exported: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("new-commit"), "")
        if len(cmd) > 3 and cmd[3] == "export":
            exported.append(cmd[2])
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub._store_watermarks(tmp_path / "hub", {"a-one": "old-commit"})

    failed = hub.sync()

    assert failed == []
    assert exported == [str(tmp_path / "one")]
    assert hub._load_watermarks(tmp_path / "hub") == {"a-one": "new-commit"}


def test_sync_exports_a_hive_with_no_prior_watermark(tmp_path, monkeypatch):
    """A first-ever sync (no watermark file at all) always exports — a cold cache means
    "treat every hive as changed", never a skip."""
    exported: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("c1"), "")
        if len(cmd) > 3 and cmd[3] == "export":
            exported.append(cmd[2])
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")

    hub.sync()

    assert exported == [str(tmp_path / "one")]
    assert hub._load_watermarks(tmp_path / "hub") == {"a-one": "c1"}


def test_sync_exports_when_bd_vc_status_is_unreadable(tmp_path, monkeypatch):
    """An unreadable watermark (`bd vc status` fails/returns junk) must never be treated as
    "unchanged" — it forces an export every time, and the watermark is never recorded for it
    (there is nothing trustworthy to record)."""
    exported: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(1, "", "boom")
        if len(cmd) > 3 and cmd[3] == "export":
            exported.append(cmd[2])
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub._store_watermarks(tmp_path / "hub", {"a-one": "whatever"})

    hub.sync()

    assert exported == [str(tmp_path / "one")]
    assert hub._load_watermarks(tmp_path / "hub") == {}


def test_sync_does_not_advance_watermark_when_repo_add_fails(tmp_path, monkeypatch):
    """A genuine `bd repo add` failure must not advance the watermark — the next run has to
    retry this hive rather than silently trust a sync that never actually registered it."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("c1"), "")
        if cmd[3:5] == ["repo", "add"]:
            return Completed(1, "", "Error: failed to add repository: database locked\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")

    failed = hub.sync()

    assert failed == ["a-one"]
    assert hub._load_watermarks(tmp_path / "hub") == {}


def test_sync_does_not_advance_watermark_when_repo_sync_fails(tmp_path, monkeypatch):
    """CONVERGENCE DISCIPLINE (bh-d5jhc.2): when the final `bd repo sync` fails entirely,
    nothing is confirmed hydrated, so NO watermark advances — including for a hive that was
    itself unchanged this round. An interrupted/failed sync must leave every hive it touched
    re-syncable, never silently skipped forever."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("c1"), "")
        if cmd[3:5] == ["repo", "sync"]:
            return Completed(1, "", "Error: sync exploded\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub._store_watermarks(tmp_path / "hub", {"a-one": "c1"})  # already "unchanged" this round

    failed = hub.sync()

    assert failed == ["a-one"]
    # the pre-existing watermark must be DROPPED, not left in place — the next run must not
    # read "unchanged" and skip export again, because bd repo sync never actually confirmed it.
    assert hub._load_watermarks(tmp_path / "hub") == {}


def test_sync_repo_add_still_runs_for_an_unchanged_hive(tmp_path, monkeypatch):
    """No regression in the idempotent already-configured re-add path (acceptance criterion):
    `bd repo add` keeps running even when export is skipped, so hub registration self-heals
    if the aggregate store itself was ever wiped/reinitialized under the same path."""
    added: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("same"), "")
        if cmd[3:5] == ["repo", "add"]:
            added.append(cmd[-1])
            return Completed(1, "", "repository already configured\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub._store_watermarks(tmp_path / "hub", {"a-one": "same"})

    failed = hub.sync()

    assert failed == []
    assert added == [str(tmp_path / "one")]


def test_reconcile_removed_prunes_watermark_for_unmanaged_hive(tmp_path, monkeypatch):
    """`_reconcile_removed` drops watermark entries for a prefix no longer in `managed`, the
    same way it drops the hub registration itself (bh-d5jhc.2's DESIGN note: `_reconcile_removed`
    is the existing home for this per-hive bookkeeping)."""

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "list"]:
            return Completed(0, "Additional repositories:\n", "")
        return Completed(0, "", "")

    monkeypatch.setattr(hub, "run", fake_run)
    managed = [{"provider": "github", "org": "a", "repo": "one", "prefix": "a-one"}]
    marks = {"a-one": "c1", "a-retired": "c2"}

    hub._reconcile_removed(tmp_path / "hub", {}, managed, marks)

    assert marks == {"a-one": "c1"}


def test_load_watermarks_ignores_a_mismatched_aggregate(tmp_path, monkeypatch):
    """A watermark file recorded against a DIFFERENT aggregate directory (e.g. the hub->HQ
    handoff) must never be read as if it applies here — HQ has never seen that hive yet."""
    monkeypatch.setattr(hub.config, "cache_dir", lambda: tmp_path / "cache")
    _identify(tmp_path / "old-hub")
    _identify(tmp_path / "new-hq", "a-different-store")

    hub._store_watermarks(tmp_path / "old-hub", {"a-one": "c1"})

    assert hub._load_watermarks(tmp_path / "old-hub") == {"a-one": "c1"}
    assert hub._load_watermarks(tmp_path / "new-hq") == {}


def test_load_watermarks_ignores_an_aggregate_re_minted_under_the_same_path(tmp_path, monkeypatch):
    """THE REBUILD HAZARD (bh-89wxf.1): `rm -rf` the hub and `bh sync` re-mints it at the SAME
    path with a NEW store identity. Path-keyed marks would have said "unchanged" for every
    hive against a store holding nothing at all."""
    monkeypatch.setattr(hub.config, "cache_dir", lambda: tmp_path / "cache")
    hub_dir = _identify(tmp_path / "hub")
    hub._store_watermarks(hub_dir, {"a-one": "c1"})
    assert hub._load_watermarks(hub_dir) == {"a-one": "c1"}

    shutil.rmtree(hub_dir)
    _identify(hub_dir, "freshly-minted")  # same path, new bd init

    assert hub._load_watermarks(hub_dir) == {}


def test_load_watermarks_treats_an_unidentifiable_aggregate_as_cold(tmp_path, monkeypatch):
    """No recorded `project_id` is UNKNOWN, and unknown fails toward the full re-sync."""
    monkeypatch.setattr(hub.config, "cache_dir", lambda: tmp_path / "cache")
    hub_dir = _identify(tmp_path / "hub")
    hub._store_watermarks(hub_dir, {"a-one": "c1"})
    (hub_dir / ".beads" / "metadata.json").write_text("{}")

    assert hub._load_watermarks(hub_dir) == {}


def test_hive_commit_reads_the_vc_status_commit_field(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        return Completed(0, _vc_status("abc123"), "")

    monkeypatch.setattr(bd, "_run", fake_run)
    assert hub._hive_commit({}, tmp_path) == "abc123"


def test_hive_commit_returns_none_on_a_failed_or_unparseable_response(monkeypatch):
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(1, "", "boom"))
    assert hub._hive_commit({}, ".") is None

    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(0, "not json", ""))
    assert hub._hive_commit({}, ".") is None

    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(0, '{"no_commit_key": 1}', ""))
    assert hub._hive_commit({}, ".") is None


def test_sync_reconciles_stale_hub_registration(tmp_path, monkeypatch, capsys):
    """A repo registered in the hub but no longer managed is dropped via `bd repo remove`,
    while a still-managed registration is left untouched (and the repo/hive itself is never
    touched — only the hub entry)."""
    removed: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "list"]:
            managed_path = str(tmp_path / "one")
            listing = (
                "Primary repository: .\n\nAdditional repositories:\n"
                f"  - {managed_path}\n"
                "  - /Users/brian/workspace/github/briancripe/story-swarm\n"
            )
            return Completed(0, listing, "")
        if cmd[3:5] == ["repo", "remove"]:
            removed.append(cmd[-1])
            return Completed(0, "", "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub.sync()
    out = capsys.readouterr()
    stale = "/Users/brian/workspace/github/briancripe/story-swarm"
    assert removed == [stale]
    assert str(tmp_path / "one") not in removed
    assert f"dropped stale hub entry: {stale}" in out.err


def test_sync_reconcile_no_op_when_all_registrations_managed(tmp_path, monkeypatch, capsys):
    """When every registered repo maps to a managed hive, no `bd repo remove` is issued."""
    removed: list[str] = []

    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "list"]:
            listing = (
                "Primary repository: .\n\nAdditional repositories:\n"
                f"  - {tmp_path / 'one'}\n  - {tmp_path / 'two'}\n"
            )
            return Completed(0, listing, "")
        if cmd[3:5] == ["repo", "remove"]:
            removed.append(cmd[-1])
            return Completed(0, "", "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    hub.sync()
    assert removed == []


def test_query_refuses_hub_write_before_running_bd(tmp_path, monkeypatch, capsys):
    """`ws hub bd create` is refused by the guard — bd is never invoked (no stranded bead)."""
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("bd must not run on a hub write"))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    with pytest.raises(typer.Exit) as exc:
        hub.query(["create", "-t", "stranded"])
    assert exc.value.exit_code == 1
    assert "ISSUES NO IDS" in capsys.readouterr().err


def test_query_label_defaults_to_hq_and_forwards_to_guard(tmp_path, monkeypatch, capsys):
    """`hub.query`'s default `label` ("hq") reaches the guard's refusal message unchanged, and
    an explicit `label="hub"` (the deprecated alias's call site) overrides it (bh-ohx2)."""
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("bd must not run on a write"))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)

    with pytest.raises(typer.Exit):
        hub.query(["create", "-t", "boom"])
    assert "`bh hq bd create`" in capsys.readouterr().err

    with pytest.raises(typer.Exit):
        hub.query(["create", "-t", "boom"], label="hub")
    assert "`bh hub bd create`" in capsys.readouterr().err


def test_query_read_verb_forwards_to_bd(tmp_path, monkeypatch):
    """A read verb passes the guard and forwards to bd against the hub."""
    (tmp_path / ".beads").mkdir()
    calls = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    # `run_bounded`, not `run`: the aggregate read is bounded since bh-toitp.
    monkeypatch.setattr(hub, "run_bounded", lambda cmd, **k: calls.append(cmd) or _Ok())
    hub.query(["ready"])
    assert calls and calls[0][-1] == "ready"


def test_intake_filters_fleet_wide_untriaged(tmp_path, monkeypatch):
    """`ws hub intake` is the superintendent's fleet-wide inbox: a filtered read for untriaged
    intake across every hydrated hive (source-agnostic — keyed on intake:untriaged), with extra
    bd flags forwarded through."""
    from beadhive import state

    (tmp_path / ".beads").mkdir()
    calls = []

    class _Ok:
        returncode = 0

    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    monkeypatch.setattr(hub, "run_bounded", lambda cmd, **k: calls.append(cmd) or _Ok())

    hub.intake(["--json"])

    argv = calls[0]
    assert argv[3:] == ["list", "--label", state.INTAKE_UNTRIAGED, "--status", "open", "--json"]


# ---- the aggregate read is BOUNDED, on both axes (bh-toitp) ---------------------------------
#
# 31 live `bd -C ~/.beadhive/hq show <~50 ids> --json` processes, 9.6 GB, oldest 2h12m, all
# ppid=1, none stuck in the kernel — `hub.query` was fire-and-forget against the store that is
# already the contention point. The consequence was not disk: it was `bh escalate` hanging >13
# minutes and failing four times, so the factory lost its own path for reporting that the
# factory was broken.


def test_a_wedged_aggregate_read_is_terminated_and_named(tmp_path, monkeypatch, capsys):
    """AC1 + AC3: bounded, terminated, and reported as a failure NAMING the store and the verb
    — never left running and never silently dropped."""
    from beadhive.run import ChildTimeout

    (tmp_path / ".beads").mkdir()
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)

    def wedged(cmd, **k):
        raise ChildTimeout("hq bd show against /hq exceeded 120s and was TERMINATED")

    monkeypatch.setattr(hub, "run_bounded", wedged)
    with pytest.raises(typer.Exit) as exc:
        hub.query(["show", "bh-1", "bh-2", "--json"])
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "TERMINATED" in err
    assert "bd -C .*\\/hq" in err or "/hq" in err  # …and points at the recovery probe


def test_the_aggregate_read_passes_the_store_and_verb_into_its_label(tmp_path, monkeypatch):
    """The failure has to name WHICH hive and WHICH verb — a timeout that says only 'timed out'
    puts the reader back to enumerating processes by hand, which is how this was found."""
    (tmp_path / ".beads").mkdir()
    seen = {}
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)

    class _Ok:
        returncode = 0

    def spy(cmd, *, timeout, label):
        seen["timeout"], seen["label"] = timeout, label
        return _Ok()

    monkeypatch.setattr(hub, "run_bounded", spy)
    hub.query(["show", "bh-1", "--json"])
    assert seen["timeout"] == hub.AGGREGATE_TIMEOUT
    assert "show bh-1" in seen["label"]
    assert str(tmp_path) in seen["label"]


def test_the_concurrency_ceiling_refuses_rather_than_queueing(tmp_path):
    """AC2: the SPAWN is bounded, not merely cleaned up. Ten waves 10s apart with no completion
    check is how 31 processes accumulated; a caller that cannot be served must be TOLD, because
    queuing is what turned a slow read into a pile.

    `slot_dir=tmp_path` (bh-a4zsr): the production permits are HOST-WIDE by design, so without
    an override this test takes a REAL slot — colliding with its sibling below under `-n auto`
    (inside the fence TMPDIR is one directory shared by all 24 workers) and with the operator's
    own `bh hq bd …` outside it. A flaky gate here would be blamed on the change under test.
    """
    with hub._aggregate_slot(slots=1, wait=0.1, slot_dir=tmp_path):
        with pytest.raises(hub.AggregateBusy) as exc:
            with hub._aggregate_slot(slots=1, wait=0.1, slot_dir=tmp_path):
                pytest.fail("the second reader must not have been admitted")
    assert "busy" in str(exc.value)


def test_the_ceiling_admits_up_to_its_bound(tmp_path):
    """The negative arm: a ceiling of 2 must actually admit 2, or the bound is just an outage."""
    with hub._aggregate_slot(slots=2, wait=0.1, slot_dir=tmp_path) as first:
        with hub._aggregate_slot(slots=2, wait=0.1, slot_dir=tmp_path) as second:
            assert {first, second} == {0, 1}


def test_permits_in_different_directories_do_not_contend(tmp_path):
    """What the override buys, stated as behaviour rather than as a parameter: two scopes each
    get their own `slot-0`, which is exactly why two ceiling tests can now run concurrently."""
    with hub._aggregate_slot(slots=1, wait=0.1, slot_dir=tmp_path / "a") as first:
        with hub._aggregate_slot(slots=1, wait=0.1, slot_dir=tmp_path / "b") as second:
            assert first == second == 0


def test_the_production_slot_scope_stays_host_wide():
    """The bound only means anything if it is shared across INVOCATIONS — every one of the 31
    leaked processes belonged to a different `bh`. A future "fix" to a per-process path would
    leave the ceiling looking present and doing nothing, which is worse than not having it."""
    import tempfile

    assert hub.aggregate_slot_dir().parent == Path(tempfile.gettempdir())
    assert "HOST-WIDE IS THE WHOLE POINT" in (hub.aggregate_slot_dir.__doc__ or "")


def test_the_ceiling_can_be_disabled_for_the_other_arm_of_the_measurement():
    """`BH_HQ_QUERY_SLOTS=0`. The acceptance is a MEASUREMENT ('zero processes remain older
    than the timeout'), and a measurement needs an unbounded arm to compare against."""
    with hub._aggregate_slot(slots=0) as a:
        with hub._aggregate_slot(slots=0) as b:
            assert a == b == -1


def test_escalate_never_reaches_the_bounded_aggregate_path():
    """THE acceptance that matters most, pinned structurally rather than argued: `bh escalate`
    must still work while a hydration wave is in flight. It is exempt from the read ceiling BY
    CONSTRUCTION — it writes to HQ through `bd` directly and never through `hub.query` — and
    this test fails if a future refactor routes it through the aggregate."""
    import inspect

    from beadhive import escalate

    source = inspect.getsource(escalate)
    assert "hub.query" not in source
    assert "hub.intake" not in source


def test_ensure_hub_missing_bd_is_friendly(tmp_path, monkeypatch, capsys):
    """A missing bd binary exits with a friendly message, not a raw FileNotFoundError."""
    # WS_HOME must point at an empty dir so config.load() raises FileNotFoundError and
    # _aggregation_target() falls back to hub_dir() (which honours WS_HUB).
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))

    def raise_fnf(cmd, **k):
        raise FileNotFoundError("bd")

    monkeypatch.setattr(hub, "run", raise_fnf)
    with pytest.raises(typer.Exit):
        hub.ensure_hub()
    assert "`bd` not found" in capsys.readouterr().err


def test_ensure_hub_init_failure_is_friendly(tmp_path, monkeypatch, capsys):
    """A failing `bd init` exits with a legible bh-level message, not a CalledProcessError
    trace. bd's own error streams straight through and is never captured/re-quoted here
    (bh-areg.7's review, round 3 — capturing it read the WRONG line for `--shared-server`'s
    two-phase git-then-dolt-server shape), so a hermetic fake `hub.run` — which only returns
    a value, never actually writing to the terminal the way a real streaming subprocess would
    — has nothing of bd's own to assert on beyond bh's own summary line and the clean exit."""
    # Same WS_HOME isolation — see test_ensure_hub_missing_bd_is_friendly.
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    monkeypatch.setattr(hub, "run", lambda cmd, **k: Completed(1, "", ""))
    with pytest.raises(typer.Exit):
        hub.ensure_hub()
    err = capsys.readouterr().err
    assert "bd init failed" in err
    assert "bd's error is above" in err
    assert "Usage:" not in err


# ---------------------------------------------------------------------------
# ensure_store defaults a FRESH store onto bd's shared server (bh-areg.7)
# ---------------------------------------------------------------------------


def test_ensure_store_passes_shared_server_flag_on_a_fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return Completed(0, "", "")

    monkeypatch.setattr(hub, "run", fake_run)
    from beadhive import store_locator

    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda store: False)

    hub.ensure_hub()

    init_call = calls[0]
    assert init_call[:2] == ["bd", "init"]
    assert "--shared-server" in init_call
    config_call = ["bd", "-C", str(tmp_path / "hub"), "config", "set", "dolt.shared-server", "true"]
    assert config_call in calls


def test_ensure_store_warns_visibly_when_dolt_mode_needed_fixing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setenv("WS_HUB", str(tmp_path / "hub"))
    monkeypatch.setattr(hub, "run", lambda cmd, **k: Completed(0, "", ""))
    from beadhive import store_locator

    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda store: True)

    hub.ensure_hub()

    err = capsys.readouterr().err
    assert "dolt_mode" in err
    assert "⚠" in err


def test_ensure_store_leaves_an_existing_store_untouched(tmp_path, monkeypatch):
    """The `.beads`-exists guard: a pre-existing store is never re-inited, so a store that
    predates this bead (or was migrated by hand) is never touched by the shared-server
    default — same "existing hives untouched" discipline as onboarding's own skip path."""
    monkeypatch.setenv("WS_HOME", str(tmp_path))
    store = tmp_path / "hub"
    (store / ".beads").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))
    monkeypatch.setenv("WS_HUB", str(store))

    hub.ensure_store(store, "hub")

    assert calls == []


def test_bd_ni_env_reads_os_environ_fresh_on_every_call(monkeypatch):
    """`_bd_ni_env()` must never behave like the module-level snapshot it replaced
    (bh-areg.7's review, round 3): a constant frozen at import time could not see a
    later-set env override (e.g. a test's own `BEADS_SHARED_SERVER_DIR` isolation fixture),
    silently falling through to whatever was ambient at first import — a real path back into
    the operator's production shared server."""
    monkeypatch.delenv("BH_AREG7_PROBE", raising=False)
    assert "BH_AREG7_PROBE" not in hub._bd_ni_env()

    monkeypatch.setenv("BH_AREG7_PROBE", "1")
    env = hub._bd_ni_env()
    assert env["BH_AREG7_PROBE"] == "1"
    assert env["BD_NON_INTERACTIVE"] == "1"


# ---- bh-hpeye: `bd bootstrap` must activate shared-server too, not just `bd init` ---------


def test_bootstrap_env_activates_shared_server(monkeypatch):
    """`bd bootstrap` has no `--shared-server` flag of its own (unlike `bd init`) — `env()`
    must carry the activating env var for every `bd bootstrap` call site."""
    monkeypatch.delenv("BEADS_DOLT_SHARED_SERVER", raising=False)
    env = hub.bootstrap_env()
    assert env["BEADS_DOLT_SHARED_SERVER"] == "1"
    assert env["BD_NON_INTERACTIVE"] == "1"


# ---- bh-fnn3d / bh-qpa3g / bh-gbmyw: cache stores carry their own per-hive identity -------


def _cache_entry(repo="r", prefix="dxnvh"):
    return {"provider": "github", "org": "o", "repo": repo, "prefix": prefix}


def _wire_cache(tmp_path, monkeypatch, cache, calls, *, server_up=True):
    """Fake clone + bootstrap for `_fetch_cache`, against a fake shared-server datadir."""
    server = tmp_path / "shared" / "dolt"
    server.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(tmp_path / "shared"))

    def fake_run(cmd, **k):
        calls.append((cmd, k))
        if cmd[:2] == ["git", "clone"]:
            cache.mkdir(parents=True, exist_ok=True)
            (cache / ".git").mkdir(exist_ok=True)
        return Completed(0, "", "")

    def fake_bd_run(cmd, **k):
        calls.append((cmd, k))
        if cmd[:2] == ["bd", "bootstrap"]:
            # what a real `bd bootstrap` does with the seeded name: clone into that database ON
            # THE SHARED SERVER (measured against bd 1.1.0 — "via server at 127.0.0.1:3308")
            (server / _metadata(cache)["dolt_database"]).mkdir(parents=True, exist_ok=True)
        return Completed(0, "", "")

    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(bd, "_run", fake_bd_run)
    monkeypatch.setattr(hub.config, "cache_dir", lambda: cache.parents[2])
    monkeypatch.setattr(hub, "_hive_url", lambda cfg, e: "git@github.com:o/r.git")
    monkeypatch.setattr(hub.registry, "hives", lambda cfg: cfg.get("managed_repos", []))
    from beadhive import dolt_health

    probe = dolt_health.ProbeResult(reachable=server_up, detail="")
    monkeypatch.setattr(dolt_health, "probe_shared_server", lambda **k: probe)
    return server


def _metadata(cache):
    return json.loads((cache / ".beads" / "metadata.json").read_text())


def test_fetch_cache_hydrates_into_its_own_database_on_the_shared_server(tmp_path, monkeypatch):
    """bh-fnn3d: the registered prefix was never an input to the bootstrap's database
    resolution, so a fresh host's cache-only bootstrap landed in bd's generic database and could
    never hydrate. The name is seeded into the store's own metadata BEFORE bootstrap — the only
    lever, bootstrap has no working `--database` override (bh-8g6cj) — and the fleet's
    shared-server mode stays ON, per docs/design/dolt-server-mode-adr.md."""
    cache = tmp_path / "cache" / "github" / "o" / "r"
    calls = []
    server = _wire_cache(tmp_path, monkeypatch, cache, calls)
    entry = _cache_entry(prefix="bc-workspace")

    result = hub._fetch_cache({"managed_repos": [entry]}, entry)

    assert result == cache
    assert (server / "bc_workspace").is_dir()
    meta = _metadata(cache)
    assert meta["dolt_mode"] == "server"
    assert meta["dolt_database"] == "bc_workspace"
    assert meta["dolt_server_database"] == "bc_workspace"  # recorded, never re-derived
    bootstrap_kwargs = next(k for c, k in calls if c[:2] == ["bd", "bootstrap"])
    assert bootstrap_kwargs["env"]["BEADS_DOLT_SHARED_SERVER"] == "1"


def test_fetch_cache_repairs_an_owned_mode_store_and_never_restamps_it(tmp_path, monkeypatch):
    """bh-qpa3g, the measured shape: metadata asserting server mode + another hive's database
    (`ag_hp`) + the shared project_id, a per-cache dolt server on a rotating port whose datadir
    holds nothing (bd's OWNED mode, rejected by the ADR), and the data stranded in
    `embeddeddolt/`. Dropping the stale project_id is load-bearing: with it in place bd's own
    "Bootstrap metadata repair" resolves it back to `ag_hp` and overwrites the seeded name —
    that is the re-stamp the bead observed from the outside."""
    cache = tmp_path / "cache" / "github" / "o" / "r"
    (cache / ".beads" / "embeddeddolt" / "beads").mkdir(parents=True)
    (cache / ".beads" / "dolt").mkdir()  # the owned server's datadir, holding no database
    (cache / ".beads" / "dolt-server-config.yaml").write_text("listener:\n  port: 43755\n")
    (cache / ".git").mkdir()
    (cache / ".beads" / "metadata.json").write_text(
        json.dumps(
            {
                "dolt_mode": "server",
                "dolt_database": "ag_hp",
                "dolt_server_database": "ag_hp",
                "project_id": "8b177292-509a-4c36-b10d-6fd4b0047b33",
            }
        )
    )
    calls = []
    server = _wire_cache(tmp_path, monkeypatch, cache, calls)
    (server / "ag_hp").mkdir()  # another hive's database really is on the server
    entry = _cache_entry(prefix="dxnvh")
    cfg = {"managed_repos": [entry]}

    assert hub._fetch_cache(cfg, entry) == cache

    meta = _metadata(cache)
    assert meta["dolt_database"] == "dxnvh"
    assert meta["dolt_mode"] == "server"
    assert "project_id" not in meta  # nothing left for bd to "repair" back to ag_hp
    assert (server / "dxnvh").is_dir()

    # THE REGRESSION: a second sync must leave the repaired store exactly as it found it.
    before = (cache / ".beads" / "metadata.json").read_bytes()
    assert hub._fetch_cache(cfg, entry) == cache
    assert (cache / ".beads" / "metadata.json").read_bytes() == before


def test_fetch_cache_starts_the_shared_server_on_a_fresh_host(tmp_path, monkeypatch):
    """bh-fnn3d's actual scenario is a host with no prior store, where nothing has started bd's
    shared server yet — and `bd bootstrap`, unlike `bd init --shared-server`, does not start one
    itself. Probe first, start only when nothing answers (bh-l90xk)."""
    cache = tmp_path / "cache" / "github" / "o" / "r"
    calls = []
    _wire_cache(tmp_path, monkeypatch, cache, calls, server_up=False)
    entry = _cache_entry()

    assert hub._fetch_cache({"managed_repos": [entry]}, entry) == cache
    assert ["bd", "-C", str(cache), "dolt", "start", "--global"] in [c for c, _ in calls]


def test_fetch_cache_does_not_start_a_server_that_is_already_up(tmp_path, monkeypatch):
    cache = tmp_path / "cache" / "github" / "o" / "r"
    calls = []
    _wire_cache(tmp_path, monkeypatch, cache, calls, server_up=True)
    entry = _cache_entry()

    hub._fetch_cache({"managed_repos": [entry]}, entry)
    assert not any(c[:4] == ["bd", "-C", str(cache), "dolt"] for c, _ in calls)


def test_fetch_cache_refuses_loudly_when_the_bootstrap_misses_the_server(
    tmp_path, monkeypatch, capsys
):
    """bh-fnn3d asked for a loud refusal naming the checkout + migrate-storage route, not a warn
    and exit 0 — and explicitly not a fallback onto a mode the ADR retired."""
    cache = tmp_path / "cache" / "github" / "o" / "r"
    calls = []
    _wire_cache(tmp_path, monkeypatch, cache, calls)
    monkeypatch.setattr(bd, "_run", lambda cmd, **k: Completed(1, "", "connection refused"))
    entry = _cache_entry()

    assert hub._fetch_cache({"managed_repos": [entry]}, entry) is None
    err = capsys.readouterr().err
    assert "did not land 'dxnvh' on the shared server" in err
    assert "migrate-storage dxnvh --confirm" in err


def test_fetch_cache_refuses_a_database_two_hives_claim(tmp_path, monkeypatch):
    """bh-gbmyw: `_fetch_cache` had no equivalent of `migrate-storage`'s
    `detect_target_collisions`, and five stores ended up sharing one database. Prefixes are
    unique fleet-wide, so this is unreachable by construction — asserted, not assumed."""
    cache = tmp_path / "cache" / "github" / "o" / "r"
    calls = []
    _wire_cache(tmp_path, monkeypatch, cache, calls)
    entry = _cache_entry(prefix="dup")
    cfg = {"managed_repos": [entry, _cache_entry(repo="other", prefix="dup")]}

    assert hub.detect_cache_collisions(cfg) == {"dup": ["github/o/r", "github/o/other"]}
    assert hub._fetch_cache(cfg, entry) is None
    assert not any(c[:2] == ["bd", "bootstrap"] for c, _ in calls)


def test_cache_databases_are_distinct_across_every_registered_hive():
    """bh-gbmyw's acceptance: no two cache stores resolve to the same identity."""
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "briancripe", "repo": "homelab", "prefix": "hl"},
            {
                "provider": "github",
                "org": "briancripe",
                "repo": "workspace",
                "prefix": "bc-workspace",
            },
            {
                "provider": "github",
                "org": "agentguides",
                "repo": "hermes-plugin",
                "prefix": "ag-hp",
            },
            {"provider": "github", "org": "ric03uec", "repo": "dxnvh", "prefix": "dxnvh"},
        ]
    }
    names = [hub.cache_database(e) for e in cfg["managed_repos"]]
    assert names == ["hl", "bc_workspace", "ag_hp", "dxnvh"]
    assert hub.detect_cache_collisions(cfg) == {}


def test_persist_shared_server_mode_refuses_when_nothing_landed_on_the_server(
    tmp_path, monkeypatch, capsys
):
    """bh-qpa3g: the unconditional `dolt_mode="server"` assert is what turned bootstrap's
    `Database already exists. Nothing to do` decline into a store that claims a server database
    it does not have. No database under the shared-server datadir, no stamp."""
    store = tmp_path / "store"
    (store / ".beads").mkdir(parents=True)
    (store / ".beads" / "metadata.json").write_text(
        json.dumps({"dolt_mode": "embedded", "dolt_database": "beads"})
    )
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(tmp_path / "shared"))
    calls = []
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))

    hub.persist_shared_server_mode(store)

    assert json.loads((store / ".beads" / "metadata.json").read_text())["dolt_mode"] == "embedded"
    assert calls == []
    assert "did not land it on the shared server" in capsys.readouterr().err


def test_persist_shared_server_mode_stamps_a_store_that_really_is_on_the_server(
    tmp_path, monkeypatch, capsys
):
    """The `hq.clone` path, unchanged: a bootstrap that really did land on the shared server
    still gets its mode + `dolt.shared-server` made durable, warning visibly when bd itself
    left the mode unpersisted."""
    store = tmp_path / "hq"
    (store / ".beads").mkdir(parents=True)
    (store / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "hq"}))
    (tmp_path / "shared" / "dolt" / "hq").mkdir(parents=True)
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(tmp_path / "shared"))
    calls = []
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or Completed(0, "", ""))

    hub.persist_shared_server_mode(store)

    assert json.loads((store / ".beads" / "metadata.json").read_text())["dolt_mode"] == "server"
    assert calls == [["bd", "-C", str(store), "config", "set", "dolt.shared-server", "true"]]
    assert "⚠" in capsys.readouterr().err


def test_sync_emits_banner_and_per_hive_progress(tmp_path, monkeypatch, capsys):
    """sync() emits a 'starting hub sync' banner before the import loop and a per-hive
    progress line for each hive, both on stderr to match the existing err=True convention."""

    def fake_run(cmd, **k):
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one", "two")
    hub.sync()
    err = capsys.readouterr().err
    assert "starting hub sync (2 hive(s))" in err
    assert "• syncing a-one (1/2)" in err
    assert "• syncing a-two (2/2)" in err


# ---------------------------------------------------------------------------
# sync_one / sync_background — the split of sync()'s two responsibilities (bh-d5jhc.1)
# ---------------------------------------------------------------------------


def test_sync_one_exports_and_adds_without_a_fleet_walk(tmp_path, monkeypatch):
    """`sync_one` is the cheap synchronous half: export + `bd repo add` for ONE hive — no
    `repo sync` / `repo list` / `repo remove` fleet-wide walk, and no read of managed_repos."""
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return Completed(0, "", "")

    d = tmp_path / "one"
    (d / ".beads").mkdir(parents=True)
    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one"))

    ok = hub.sync_one("a-one", d)

    assert ok is True
    verbs = [tuple(c[3:5]) for c in calls if len(c) > 4]
    assert ("repo", "add") in verbs
    assert ("repo", "sync") not in verbs
    assert ("repo", "list") not in verbs


def test_sync_one_reports_a_genuine_add_failure(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        if cmd[3:5] == ["repo", "add"]:
            return Completed(1, "", "Error: failed to add repository: database locked\n")
        return Completed(0, "", "")

    d = tmp_path / "one"
    (d / ".beads").mkdir(parents=True)
    monkeypatch.setattr(hub, "run", fake_run)
    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(hub, "ensure_hub", lambda: tmp_path / "hub")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one"))

    assert hub.sync_one("a-one", d) is False


def test_sync_background_default_runs_full_sync_in_a_daemon_thread(tmp_path, monkeypatch):
    """`sync_background` kicks `sync()` on a daemon thread and returns immediately — the test
    joins the thread to observe the completed work (bh-d5jhc.1's best-effort daemon-thread
    shape, mirroring `metadata._spawn_reload`)."""
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return Completed(0, "", "")

    dirs = _wire(tmp_path, monkeypatch, fake_run, "one")
    monkeypatch.setattr(hub.config, "hub_sync_background", lambda cfg=None: True)

    t = hub.sync_background(hub.config.load())

    assert t is not None
    assert t.daemon is True
    t.join(timeout=5)
    assert not t.is_alive()
    assert any(c[3:5] == ["repo", "sync"] for c in calls if len(c) > 4)
    assert dirs  # sanity: _wire actually set up the fake fleet


def test_sync_background_disabled_by_config_is_a_no_op(monkeypatch):
    monkeypatch.setattr(hub.config, "hub_sync_background", lambda cfg=None: False)
    calls = []
    monkeypatch.setattr(hub, "sync", lambda: calls.append(True))

    t = hub.sync_background({})

    assert t is None
    assert calls == []


def test_sync_background_swallows_a_failing_sync(monkeypatch):
    """A background sync that raises never propagates to the caller — best-effort, mirroring
    escalate.py's non-blocking treatment of `hub.sync` failures."""

    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hub, "sync", boom)
    monkeypatch.setattr(hub.config, "hub_sync_background", lambda cfg=None: True)

    t = hub.sync_background({})
    t.join(timeout=5)
    assert not t.is_alive()


def test_hub_sync_background_config_default_and_override():
    assert config.hub_sync_background({}) is True
    assert config.hub_sync_background({"hub": {"background_sync": False}}) is False


# ---------------------------------------------------------------------------
# hub.bulk_sync wiring (bh-l7sm8) — `sync()` calls `hub_bulk.run_bulk_pass` when enabled, and
# a bulk-hydrated prefix bypasses `bd repo sync`'s own report-text bookkeeping entirely.
# ---------------------------------------------------------------------------


def test_hub_bulk_sync_config_default_and_override():
    assert config.hub_bulk_sync({}) is True  # default ON — see config.hub_bulk_sync
    assert config.hub_bulk_sync({"hub": {"bulk_sync": True}}) is True
    assert config.hub_bulk_sync({"hub": {"bulk_sync": False}}) is False  # escape hatch


def test_sync_refuses_outright_when_bulk_sync_is_disabled(tmp_path, monkeypatch):
    """bh-l7sm8: disabling the bulk path is a REFUSAL, not a fallback. The non-bulk path is
    `bd repo sync`, a known upstream perf defect (bh-z4z52) — falling through to it silently
    would charge ~398x with nothing on screen connecting the cost to the cause. So `sync()`
    no-ops, names the reason, and returns the sentinel that makes `bh sync` exit non-zero."""
    from beadhive import hub_bulk

    def boom(hub_dir, entries):
        raise AssertionError("run_bulk_pass must not run when hub.bulk_sync is false")

    calls = []

    def rec(cmd, **k):
        calls.append(cmd)
        return Completed(0, "", "")

    monkeypatch.setattr(hub_bulk, "run_bulk_pass", boom)
    _wire(tmp_path, monkeypatch, rec, "one")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one", bulk_sync=False))

    failed = hub.sync()

    assert failed == [hub.BULK_SYNC_DISABLED]
    # NO-OP: no bd subprocess of any kind, and emphatically no `bd repo sync`.
    assert not any("repo" in c and "sync" in c for c in calls), calls


def test_sync_bulk_enabled_calls_run_bulk_pass_with_resolved_entries(tmp_path, monkeypatch):
    from beadhive import hub_bulk

    captured = {}

    def fake_run_bulk_pass(hub_dir, entries):
        captured["hub_dir"] = hub_dir
        captured["entries"] = entries
        return []

    monkeypatch.setattr(hub_bulk, "run_bulk_pass", fake_run_bulk_pass)

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("c1"), "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one", bulk_sync=True))

    failed = hub.sync()

    assert failed == []
    assert captured["hub_dir"] == tmp_path / "hub"
    assert captured["entries"] == [("a-one", tmp_path / "one", True, False)]


def test_sync_bulk_hydrated_prefix_counts_hydrated_without_repo_sync_report(
    tmp_path, monkeypatch, capsys
):
    """A prefix `run_bulk_pass` reports as hydrated is trusted directly — it was deliberately
    de-registered before `bd repo sync` ran, so that call's own (empty) report can say nothing
    about it either way."""
    from beadhive import hub_bulk

    monkeypatch.setattr(hub_bulk, "run_bulk_pass", lambda hub_dir, entries: ["a-one"])

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("c1"), "")
        if cmd[3:5] == ["repo", "sync"]:
            return Completed(0, "Multi-repo sync complete: imported 0 issue(s) from 0 repo(s)", "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one", bulk_sync=True))

    failed = hub.sync()
    out = capsys.readouterr()

    assert failed == []
    assert "1 hydrated" in out.out
    assert hub._load_watermarks(tmp_path / "hub") == {"a-one": "c1"}


def test_sync_bulk_hydrated_prefix_survives_a_failing_repo_sync_call(tmp_path, monkeypatch):
    """A bulk-hydrated prefix is independent of the trailing `bd repo sync` call entirely — if
    THAT call fails (e.g. because of a totally unrelated, non-bulk hive), the bulk-hydrated
    prefix must not be swept into the blanket failure the old code applied to every `added`
    entry."""
    from beadhive import hub_bulk

    monkeypatch.setattr(hub_bulk, "run_bulk_pass", lambda hub_dir, entries: ["a-one"])

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("c1"), "")
        if cmd[3:5] == ["repo", "sync"]:
            return Completed(1, "", "Error: sync exploded\n")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one", bulk_sync=True))

    failed = hub.sync()

    assert failed == []
    assert hub._load_watermarks(tmp_path / "hub") == {"a-one": "c1"}


def test_sync_bulk_pass_receives_changed_flag_from_the_existing_watermark(tmp_path, monkeypatch):
    """`run_bulk_pass`'s per-entry `changed` flag reuses the SAME per-hive watermark comparison
    `_sync_hive`'s own export-skip already computes — never re-derived."""
    from beadhive import hub_bulk

    captured = {}
    monkeypatch.setattr(
        hub_bulk,
        "run_bulk_pass",
        lambda hub_dir, entries: captured.setdefault("entries", entries) and [],
    )

    def fake_run(cmd, **k):
        if cmd[3:5] == ["vc", "status"]:
            return Completed(0, _vc_status("same-commit"), "")
        return Completed(0, "", "")

    _wire(tmp_path, monkeypatch, fake_run, "one")
    hub._store_watermarks(tmp_path / "hub", {"a-one": "same-commit"})
    monkeypatch.setattr(hub.config, "load", lambda: _hive_cfg("one", bulk_sync=True))

    hub.sync()

    assert captured["entries"] == [("a-one", tmp_path / "one", False, False)]


# ---------------------------------------------------------------------------
# The hub is prefix-less (bh-89wxf.1) — a store with a prefix is a store that can ISSUE ids,
# and an aggregate never creates a bead. bd demands a prefix string, so the string is chosen
# to self-identify a leak; `guard.guard_hub` is the enforcement.
# ---------------------------------------------------------------------------


def test_hub_prefix_cannot_collide_with_any_derived_hive_prefix():
    """`registry.derive_prefix` builds prefixes from lowercase repo slugs, so an uppercase,
    underscore-led sentinel is unreachable from it by construction — not merely unlikely."""
    from beadhive import registry

    assert hub.HUB_PREFIX == hub.HUB_PREFIX.upper()
    assert hub.HUB_PREFIX.startswith("_")
    derived, _ = registry.derive_prefix("github", "acme", "hub", cfg={"managed_repos": []})
    assert derived != hub.HUB_PREFIX
    assert hub.HUB_PREFIX not in hub._LEGACY_HUB_PREFIXES


def test_ensure_hub_retires_a_legacy_hub_prefixed_store(tmp_path, monkeypatch, capsys):
    """An existing host's pre-bh-89wxf.1 hub (bd prefix 'hub') is MOVED ASIDE and re-minted
    prefix-less — the migration path, and a rebuild rather than Dolt surgery."""
    store = tmp_path / "hub"
    (store / ".beads").mkdir(parents=True)
    (store / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "hub"}))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: store)
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (store, hub.HUB_PREFIX))
    minted = []
    monkeypatch.setattr(hub, "ensure_store", lambda s, p: minted.append((s, p)) or s)

    hub.ensure_hub()

    assert minted == [(store, hub.HUB_PREFIX)]
    assert not store.exists()  # renamed, never deleted
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("hub.legacy-")]
    assert len(aside) == 1 and (aside[0] / ".beads").is_dir()
    assert "retired the legacy hub" in capsys.readouterr().err


def test_ensure_hub_leaves_a_store_of_unknown_provenance_alone(tmp_path, monkeypatch):
    """No recorded database name means UNKNOWN, not legacy. Retiring a store bh cannot
    identify is the one irreversible mistake available here, so it doesn't."""
    store = tmp_path / "hub"
    (store / ".beads").mkdir(parents=True)
    monkeypatch.setattr(hub.config, "hub_dir", lambda: store)
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (store, hub.HUB_PREFIX))
    monkeypatch.setattr(hub, "ensure_store", lambda s, p: s)

    hub.ensure_hub()

    assert (store / ".beads").is_dir()
    assert not list(tmp_path.glob("hub.legacy-*"))
