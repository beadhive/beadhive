"""``beadhive.intake_services``: the composition root of the intake module, end to end.

The legacy callers (``report``, ``triage``, ``escalate``) are pinned by their own suites. This
file pins what only the composition adds: structured refusals and store failures rendered as the
byte-identical legacy ``(exit, error, ...)`` tuples, the exact bd argv of a filing and of every
disposition (recorded from the pre-module implementation), reroute-to-hive as a two-store
orchestration with a single source read, and the rule that intake only ever resolves the
OPERATIONAL store and never writes a public one.

A fake ``bd._run`` records every invocation; nothing reads a clock or touches a real store.
"""

from __future__ import annotations

import json
from collections import namedtuple

import pytest

from beadhive import (
    bd,
    hub,
    intake_services,
    registry,
    report,
    state,
    store_target,
    triage,
    validate,
)

Completed = namedtuple("Completed", "returncode stdout stderr")

ENTRY = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}
CFG = {"managed_repos": [dict(ENTRY)]}
CWD = "/hive"
UNTRIAGED = [state.INTAKE_UNTRIAGED, state.ORIGIN_REPORT]


class FakeBd:
    """Records every ``bd`` argv; answers the reads from canned data and fails named verbs."""

    def __init__(
        self, *, fail=None, bead=None, rows=None, pairs=None, new_id="wid-abc", noid=False
    ):
        self.fail = fail or {}
        self.bead = (
            bead
            if bead is not None
            else {
                "id": "wid-1",
                "title": "login is broken",
                "issue_type": "bug",
                "labels": UNTRIAGED,
            }
        )
        self.rows = rows if rows is not None else []
        self.pairs = pairs if pairs is not None else []
        self.new_id, self.noid = new_id, noid
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        rest = cmd[3:]
        if rest[:1] == ["--actor"]:
            rest = rest[2:]
        if rest[:1] == ["--json"]:
            rest = rest[1:]
        verb = rest[0] if rest else ""
        key = f"set-state:{rest[2].split('=')[0]}" if verb == "set-state" else verb
        if key in self.fail:
            code, message = self.fail[key]
            return Completed(code, "", message)
        if verb == "create":
            return Completed(0, "{}" if self.noid else json.dumps({"id": self.new_id}), "")
        if verb == "show":
            return Completed(0, json.dumps(self.bead), "")
        if verb == "list":
            return Completed(0, json.dumps(self.rows), "")
        if verb == "find-duplicates":
            return Completed(0, json.dumps({"pairs": self.pairs}), "")
        return Completed(0, "", "")

    def verbs(self) -> list[str]:
        out = []
        for cmd in self.calls:
            rest = cmd[3:]
            rest = rest[2:] if rest[:1] == ["--actor"] else rest
            rest = rest[1:] if rest[:1] == ["--json"] else rest
            rest = rest[:-1] if rest[-1:] == ["--json"] else rest
            out.append(" ".join(rest))
        return out


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the legacy callers at a fake bd and a cloned hive; return a setter for the fake."""
    (tmp_path / "hive" / ".beads").mkdir(parents=True)
    monkeypatch.setattr(registry, "resolve_hive", lambda cfg, hive: dict(ENTRY))
    monkeypatch.setattr(registry, "hive_dir", lambda e: tmp_path / "hive")
    monkeypatch.setattr(validate, "bead_violations", lambda *a, **k: [])

    def install(fake: FakeBd) -> FakeBd:
        monkeypatch.setattr(bd, "_run", fake)
        return fake

    return install


# -- filing: the exact argv, and every failure rendered as it always was --------------------


def test_filing_runs_exactly_create_then_stamp_origin_then_stamp_intake(wired) -> None:
    fake = wired(FakeBd())

    result = report.file_report(
        "wid", "login is broken", "bug", "crew/dev", cfg=CFG, description="b"
    )

    assert result == (0, "", "wid-abc")
    hive = str(CFG and registry.hive_dir(ENTRY))
    assert fake.calls == [
        ["bd", "-C", hive, "--actor", "crew/dev", "--json", "create", "login is broken",
         "--type", "bug", "-l", "provider:github,org:acme,repo:widget", "-d", "b"],
        ["bd", "-C", hive, "--actor", "crew/dev", "set-state", "wid-abc", "origin=report",
         "--reason", "filed via bh report"],
        ["bd", "-C", hive, "--actor", "crew/dev", "set-state", "wid-abc", "intake=untriaged",
         "--reason", "filed via bh report"],
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("fake", "expected"),
    [
        (
            FakeBd(fail={"create": (5, "Error: boom\nmore")}),
            (5, "bd create failed: Error: boom", ""),
        ),
        (FakeBd(noid=True), (1, "bd create reported no new bead id", "")),
        (
            FakeBd(fail={"set-state:origin": (6, "Error: no origin")}),
            (6, "filed wid-abc but could not set origin: Error: no origin", "wid-abc"),
        ),
        (
            FakeBd(fail={"set-state:intake": (7, "Error: no intake")}),
            (7, "filed wid-abc but could not set intake state: Error: no intake", "wid-abc"),
        ),
    ],
)
def test_a_failed_filing_step_is_rendered_with_its_status_and_any_half_filed_id(
    wired, fake, expected
) -> None:
    wired(fake)
    assert report.file_report("wid", "t", "bug", "a", cfg=CFG) == expected


def test_a_refused_label_set_writes_nothing(wired, monkeypatch) -> None:
    fake = wired(FakeBd())
    monkeypatch.setattr(validate, "bead_violations", lambda *a, **k: ["x\tbad:1", "y\tbad:2"])

    code, error, new_id = report.file_report("wid", "t", "bug", "a", cfg=CFG)

    assert (code, new_id) == (1, "")
    assert error == "report bead would carry invalid labels: x\tbad:1; y\tbad:2"
    assert fake.calls == []


def test_the_label_check_sees_the_new_beads_own_labels_only(wired, monkeypatch) -> None:
    wired(FakeBd())
    seen = []
    monkeypatch.setattr(
        validate, "bead_violations", lambda cfg, iid, labels, *a: seen.append((iid, labels)) or []
    )

    report.file_report("wid", "t", "bug", "a", cfg=CFG, origin=state.ORIGIN_ESCALATION)

    assert seen == [
        (
            "wid-intake",
            ["provider:github", "org:acme", "repo:widget", "origin:escalation", "intake:untriaged"],
        )
    ]


def test_a_clone_on_demand_filing_is_pushed_and_a_failed_push_still_names_the_id(
    wired, monkeypatch, tmp_path
) -> None:
    cache = tmp_path / "cache"
    (cache / ".beads").mkdir(parents=True)
    monkeypatch.setattr(registry, "hive_dir", lambda e: tmp_path / "absent")
    monkeypatch.setattr(hub, "_fetch_cache", lambda cfg, entry: cache)
    fake = wired(FakeBd(fail={"dolt": (9, "Error: push rejected")}))

    code, error, new_id = report.file_report("wid", "t", "bug", "a", cfg=CFG)

    assert (code, new_id) == (9, "wid-abc")
    assert error == "filed wid-abc in the cache but push to its hive failed: Error: push rejected"
    assert all(cmd[1:3] == ["-C", str(cache)] for cmd in fake.calls)


# -- dispositions ---------------------------------------------------------------------------


def test_each_disposition_runs_its_recorded_argv(wired) -> None:
    fake = wired(FakeBd())
    triage.accept(CWD, "wid-1", "disp/m", issue_type="feature", priority="1")
    triage.reject(CWD, "wid-1", "disp/m", reason="no thanks")
    triage.promote(CWD, "wid-1", "disp/m")
    triage.reroute(CWD, "wid-1", "disp/m", superintendent="super/anna")

    assert fake.verbs() == [
        "show wid-1",
        "update wid-1 --type feature --priority 1",
        "set-state wid-1 intake=accepted --reason accepted into backlog",
        "show wid-1",
        "set-state wid-1 intake=rejected --reason rejected: no thanks",
        "close wid-1 --reason no thanks",
        "show wid-1",
        "set-state wid-1 intake=promoted --reason handed to the planner for adoption",
        "show wid-1",
        "assign wid-1 super/anna",
    ]


@pytest.mark.parametrize(
    ("fake", "call", "expected"),
    [
        (
            FakeBd(fail={"update": (4, "Error: upd")}),
            lambda: triage.accept(CWD, "wid-1", "d", issue_type="bug"),
            (4, "bd update failed: Error: upd", ""),
        ),
        (
            FakeBd(fail={"set-state:intake": (3, "Error: st")}),
            lambda: triage.accept(CWD, "wid-1", "d"),
            (3, "could not clear intake state: Error: st", ""),
        ),
        (
            FakeBd(fail={"close": (2, "Error: cl")}),
            lambda: triage.reject(CWD, "wid-1", "d", reason="r"),
            (2, "bd close failed: Error: cl", ""),
        ),
        (
            FakeBd(fail={"assign": (3, "Error: as")}),
            lambda: triage.reroute(CWD, "wid-1", "d", superintendent="super/a"),
            (3, "bounce to superintendent failed: Error: as", ""),
        ),
        (
            FakeBd(fail={"show": (1, "Error: no issue found")}),
            lambda: triage.promote(CWD, "wid-1", "d"),
            (1, "wid-1 not found", ""),
        ),
        (
            FakeBd(bead={"id": "wid-1", "labels": []}),
            lambda: triage.reject(CWD, "wid-1", "d", reason="r"),
            (1, "wid-1 is not an untriaged intake bead — nothing to triage", ""),
        ),
    ],
)
def test_a_failed_or_refused_disposition_keeps_its_legacy_message_and_status(
    wired, fake, call, expected
) -> None:
    wired(fake)
    assert call() == expected


def test_a_rejected_disposition_without_a_reason_never_reads_the_store(wired) -> None:
    fake = wired(FakeBd())
    assert triage.reject(CWD, "wid-1", "d", reason="") == (
        1,
        "reject requires a --reason (reporter-visible)",
        "",
    )
    assert fake.calls == []


# -- reroute to another hive: a two-store orchestration, one source read --------------------


def test_reroute_to_a_hive_reads_the_source_once_refiles_then_clears_and_closes(wired) -> None:
    fake = wired(FakeBd(new_id="wid-new"))

    result = triage.reroute(CWD, "wid-1", "disp/m", to_hive="wid", cfg=CFG)

    assert result == (0, "", "✓ rerouted wid-1 → wid (wid-new)")
    assert fake.verbs() == [
        "show wid-1",  # the one read of the source, before anything is written anywhere
        "create login is broken --type bug -l provider:github,org:acme,repo:widget",
        "set-state wid-new origin=report --reason filed via bh report",
        "set-state wid-new intake=untriaged --reason filed via bh report",
        "set-state wid-1 intake=rerouted --reason rerouted to wid as wid-new",
        "close wid-1 --reason rerouted to wid as wid-new",
    ]


def test_a_refused_refile_leaves_the_source_untouched(wired) -> None:
    fake = wired(FakeBd(fail={"create": (5, "Error: boom")}))

    code, error, message = triage.reroute(CWD, "wid-1", "disp/m", to_hive="wid", cfg=CFG)

    assert (code, message) == (5, "")
    assert error == "reroute re-file failed: bd create failed: Error: boom"
    assert not any(v.startswith(("set-state wid-1", "close")) for v in fake.verbs())


def test_a_bead_that_is_not_untriaged_is_refused_before_any_refile(wired) -> None:
    fake = wired(FakeBd(bead={"id": "wid-1", "labels": []}))

    code, error, _ = triage.reroute(CWD, "wid-1", "disp/m", to_hive="wid", cfg=CFG)

    assert code == 1 and "not an untriaged intake bead" in error
    assert fake.verbs() == ["show wid-1"]


def test_an_unknown_type_is_refiled_as_a_bug(wired) -> None:
    fake = wired(
        FakeBd(bead={"id": "wid-1", "title": "x", "issue_type": "task", "labels": UNTRIAGED})
    )

    triage.reroute(CWD, "wid-1", "disp/m", to_hive="wid", cfg=CFG)

    assert "--type bug" in fake.verbs()[1]


# -- the queue and the duplicate reads hand back the store's own rows -----------------------


def test_the_queue_is_the_stores_own_rows_narrowed_by_resolved_channel(wired) -> None:
    rows = [
        {"id": "wid-1", "labels": UNTRIAGED, "extra": {"k": [1, {"z": None}]}},
        {"id": "wid-2", "source_system": "github", "labels": [state.INTAKE_UNTRIAGED]},
    ]
    fake = wired(FakeBd(rows=rows))

    assert triage.list_intake(CWD) == rows
    assert triage.list_intake(CWD, source="github") == [rows[1]]
    assert triage.list_intake(CWD, source="report") == [rows[0]]
    assert fake.verbs()[0] == f"list --label {state.INTAKE_UNTRIAGED} --status open"


def test_duplicate_pairs_keep_every_field_bd_printed(wired) -> None:
    pairs = [
        {"issue_a_id": "wid-1", "issue_b_id": "wid-9", "similarity": 0.7, "issue_a_title": "t"},
        {"issue_a_id": "wid-3", "issue_b_id": "wid-4", "similarity": 0.8},
    ]
    fake = wired(FakeBd(pairs=pairs))

    assert triage.find_dupes(CWD, threshold=0.4) == pairs
    assert intake_services.entry_dupes(CWD, "wid-1") == [pairs[0]]
    triage.find_dupes(CWD, method="ai")
    assert fake.verbs()[0] == "find-duplicates --threshold 0.4 --method mechanical"
    assert fake.verbs()[-1] == "find-duplicates --threshold 0.5 --method ai"


# -- the store scope: operational only, never the public projection store -------------------


def test_intake_resolves_only_the_operational_store_and_never_a_public_one(
    wired, monkeypatch
) -> None:
    """Acceptance item 5: the composition resolves stores through ``operational_store`` alone.

    The entry declares a public store; every ``resolve_store`` call the filing makes is recorded,
    and none names ``SCOPE_PUBLIC``. Every bd invocation is scoped to the hive's own directory."""
    entry = {
        **ENTRY,
        "stores": {
            "internal": {"remote": "widget-private"},
            "public": {"prefix": "widpub", "path": "/stores/widpub", "remote": "git-origin"},
        },
    }
    cfg = {"managed_repos": [entry]}
    monkeypatch.setattr(registry, "resolve_hive", lambda c, hive: dict(entry))
    scopes: list[str | None] = []
    real = store_target.resolve_store

    def spy(e, scope=None, **kw):
        scopes.append(scope)
        return real(e, scope, **kw)

    monkeypatch.setattr(store_target, "resolve_store", spy)
    fake = wired(FakeBd())
    monkeypatch.setattr(registry, "resolve_hive", lambda c, hive: dict(entry))

    assert report.file_report("wid", "t", "bug", "a", cfg=cfg) == (0, "", "wid-abc")

    assert scopes and store_target.SCOPE_PUBLIC not in scopes
    assert all(
        "widpub" not in " ".join(cmd) and "/stores/" not in " ".join(cmd) for cmd in fake.calls
    )
    scope = intake_services.store_scope(cfg, entry)
    assert scope.public_prefixes == frozenset({"widpub"})  # recognised, never opened
    assert scope.operational_prefix == "wid"


def test_an_unresolvable_store_is_a_clean_refusal_with_nothing_written(wired, monkeypatch) -> None:
    fake = wired(FakeBd())

    def broken(e, scope=None, **kw):
        raise store_target.StoreConfigInvalid("stores.public.path is inside the code checkout")

    monkeypatch.setattr(store_target, "resolve_store", broken)

    code, error, new_id = report.file_report("wid", "t", "bug", "a", cfg=CFG)

    assert (code, new_id) == (1, "")
    assert error == "stores.public.path is inside the code checkout"
    assert fake.calls == []


# -- the lifetime intake-hold ----------------------------------------------------------------


def test_the_root_sweeps_one_operational_store_with_two_reads_and_writes_nothing(wired) -> None:
    fake = wired(FakeBd())

    sweep = intake_services.sweep_holds(CWD)

    assert sweep.clean
    assert fake.verbs() == [
        "list --all --limit 0 --has-metadata-key intake.source",
        "gate list --limit 0",
    ]


def test_a_hold_that_could_not_be_opened_names_the_report_and_where_to_look() -> None:
    from beadhive.modules.intake import IntakeStoreError

    exc = IntakeStoreError("bd gate create failed: boom", bead="wid-9", operation="open_hold")

    message = intake_services.store_error_message(exc)

    assert message.startswith("created wid-9 but could not hold it: bd gate create failed: boom")
    assert "bh doctor" in message
