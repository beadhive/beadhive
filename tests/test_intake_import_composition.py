"""``bh work import``: the composition root, end to end, with the public store as a fixture.

``intake_services.import_issue`` composes the config switch, the tracker the hive's store binding
serves (a real ``TrackerAdapters`` over ``MemoryTracker``), the operational ``ReportStore`` (the
in-memory one, standing in for bd) and the lock. What only the composition adds is pinned here:

* acceptance item 5 of external-intake-module-adr.md: the intake composition never resolves
  ``SCOPE_PUBLIC``, and a byte-fingerprinted public-store fixture is IDENTICAL after every case,
  including every refusal R0 to R8, a dry run and a real import;
* off by default, with a clear ``intake_disabled``, and no production credential wiring needed;
* no tracker write: the world sees zero write requests across every case.

No live GitHub, no clock, no sleep: ``MemoryTracker`` and an in-process lock only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from tests.unit.bootstrap.test_tracker_composition import entry as binding_entry
from tests.unit.bootstrap.test_tracker_composition import plugin, roots

from beadhive import (
    guard,
    intake_services,
    private_paths,
    registry,
    store_target,
    validate,
    work_intake,
)
from beadhive.modules.intake import IntakeStoreError, IssueBinding, ReportDraft, SourceSnapshot
from beadhive.modules.intake.domain import IssueStatus
from beadhive.modules.tracker.contracts import IssueState
from beadhive.testing.intake import InProcessIntakeLock, MemoryReportStore
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at

ACTOR = "dev/importer"
ENABLED = {"intake": {"external": {"enabled": True}}}


def fingerprint(root: Path) -> str:
    """A digest of every path and byte under ``root``."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.hive = tmp_path / "hive"
        (self.hive / ".beads").mkdir(parents=True)
        self.public = tmp_path / "public-store"
        (self.public / ".beads" / "dolt").mkdir(parents=True)
        (self.public / ".beads" / "issues.jsonl").write_bytes(b'{"id":"wpub-1"}\n')
        (self.public / ".beads" / "dolt" / "chunk").write_bytes(b"\x00\x01public\xff")
        self.entry = {
            **binding_entry(),
            "stores": {
                "internal": {"remote": "r"},
                "public": {
                    "prefix": "wpub",
                    "path": str(self.public),
                    "remote": "git-origin",
                    "source": "github-issues",
                    "adapter": "tracker-github",
                },
            },
        }
        self.cfg = {"managed_repos": [self.entry], **ENABLED}
        monkeypatch.setattr(registry, "hive_dir", lambda e: self.hive)
        monkeypatch.setattr(validate, "bead_violations", lambda *a, **k: [])
        # the root lock's host and private-state seams: this host is primary, `.bh/` is the hive's
        monkeypatch.setattr(guard, "guard_primary", lambda hive, *, cfg, verb: None)
        monkeypatch.setattr(guard, "live_epoch", lambda hive, *, cfg: 1)
        monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: d / ".bh")
        self.scope = intake_services.store_scope(self.cfg, self.entry)
        self.store = MemoryReportStore(self.scope)
        self.lock = InProcessIntakeLock()
        self.builds = 0

        def build_store(directory, scope):
            self.builds += 1
            assert directory == self.hive
            scope()  # a real store resolves its scope on first use: the operational one
            return self.store

        monkeypatch.setattr(intake_services, "report_store", build_store)
        self.scopes: list[str | None] = []
        real = store_target.resolve_store

        def spy(e, scope=None, **kw):
            self.scopes.append(scope)
            return real(e, scope, **kw)

        monkeypatch.setattr(store_target, "resolve_store", spy)
        self.adapters, self.factory = roots()
        self.world: MemoryWorld = self.factory.world
        self.before = fingerprint(self.public)

    def import_(self, reference: str, *, dry_run: bool = False, cfg=None, **kwargs):
        kwargs.setdefault("adapters", self.adapters)
        return intake_services.import_issue(
            self.cfg if cfg is None else cfg,
            self.entry,
            self.hive,
            reference,
            ACTOR,
            dry_run=dry_run,
            **{"lock": self.lock, **kwargs},
        )

    def issue(self, title: str = "Login is broken", **kwargs):
        return self.world.add_issue(title, at=at(1), **kwargs)

    def assert_public_store_untouched(self) -> None:
        """Acceptance item 5: never resolved, never written, byte for byte."""
        assert store_target.SCOPE_PUBLIC not in self.scopes
        assert fingerprint(self.public) == self.before
        assert self.world.write_requests() == 0  # and no tracker write either


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


# -- the happy path --


def test_an_import_through_the_bound_adapter_lands_a_held_untriaged_github_report(rig) -> None:
    key = rig.issue()

    result = rig.import_(key.url)

    assert (result.code, result.error, result.reason) == (0, "", "")
    assert result.outcome is not None and result.outcome.kind.value == "created"
    view = rig.store.get(result.outcome.binding.report)
    assert view is not None
    assert {"intake:untriaged", "origin:github"} <= set(view.labels)
    assert view.external_ref == key.url and rig.store.ready_ids() == ()
    assert [h.blocks for h in rig.store.holds()] == [(view.id,)]
    rig.assert_public_store_untouched()


def test_the_same_url_twice_updates_never_duplicates_and_a_closed_issue_imports(rig) -> None:
    key = rig.issue("Old crash", state=IssueState.CLOSED)

    first = rig.import_(key.url)
    again = rig.import_(f"{key.url}#issuecomment-1")

    assert first.outcome is not None and again.outcome is not None
    assert (first.outcome.kind.value, again.outcome.kind.value) == ("created", "unchanged")
    assert first.outcome.binding.report == again.outcome.binding.report
    assert len(rig.store.bound_reports()) == 1
    view = rig.store.get(first.outcome.binding.report)
    assert view is not None and view.snapshot is not None
    assert view.snapshot.state is IssueStatus.CLOSED
    rig.assert_public_store_untouched()


def test_a_dry_run_writes_nothing_anywhere(rig) -> None:
    key = rig.issue()

    result = rig.import_(key.url, dry_run=True)

    assert result.outcome is not None
    assert (result.outcome.kind.value, result.outcome.dry_run) == ("created", True)
    assert rig.store.writes == 0 and rig.lock.holds == []
    rig.assert_public_store_untouched()


def test_an_explicit_tracker_port_works_without_the_adapter_root(rig) -> None:
    key = rig.issue()

    result = rig.import_(key.url, adapters=None, tracker=MemoryTracker(rig.world))

    assert result.code == 0 and len(rig.store.bound_reports()) == 1
    rig.assert_public_store_untouched()


def test_the_configured_default_type_files_the_report(rig) -> None:
    cfg = {**rig.cfg, "intake": {"external": {"enabled": True, "default_type": "chore"}}}
    key = rig.issue()

    result = rig.import_(key.url, cfg=cfg)

    assert result.outcome is not None
    view = rig.store.get(result.outcome.binding.report)
    assert view is not None and view.issue_type == "chore"


# -- R0: off by default, no wiring, no binding, issues.read not usable --


@pytest.mark.parametrize(
    "intake",
    [
        None,
        {},
        {"external": {}},
        {"external": {"enabled": False}},
        {"external": {"enabled": "true"}},
    ],
    ids=["absent", "empty", "no-switch", "off", "not-a-boolean"],
)
def test_r0_intake_is_off_unless_the_switch_is_literally_true(rig, intake) -> None:
    key = rig.issue()
    cfg = {"managed_repos": [rig.entry], **({"intake": intake} if intake is not None else {})}

    result = rig.import_(key.url, cfg=cfg)

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "opt-in" in result.error
    assert rig.builds == 0 and rig.world.read_requests() == 0  # nothing was even composed
    rig.assert_public_store_untouched()


def test_r0_without_production_wiring_the_verb_says_so_and_does_nothing(rig) -> None:
    key = rig.issue()

    result = rig.import_(key.url, adapters=None)  # process_adapters() is None until bh-kzs0b

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "bh-kzs0b" in result.error
    assert rig.store.writes == 0
    rig.assert_public_store_untouched()


def test_r0_a_store_that_binds_no_tracker_is_intake_disabled(rig) -> None:
    key = rig.issue()
    unbound = {**rig.entry, "stores": {**rig.entry["stores"], "public": {"prefix": "wpub"}}}
    rig.entry = unbound

    result = rig.import_(key.url)

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "binds no tracker adapter" in result.error
    rig.assert_public_store_untouched()


@pytest.mark.parametrize(
    ("plugins", "fragment"),
    [
        ({"tracker-github": plugin(issues=False)}, "read.issues.enabled"),
        ({"tracker-github": plugin(enabled=False)}, "plugins.tracker-github.enabled"),
    ],
    ids=["issues-read-off", "plugin-off"],
)
def test_r0_a_disabled_plugin_or_read_names_the_key_to_set(rig, plugins, fragment) -> None:
    key = rig.issue()
    adapters, factory = roots(plugins)

    result = rig.import_(key.url, adapters=adapters)

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert fragment in result.error
    assert rig.store.writes == 0 and factory.world.write_requests() == 0
    rig.assert_public_store_untouched()


# -- R1 to R8, each with its distinct code, through the composition --


def test_every_refusal_has_its_distinct_code_and_leaves_both_stores_untouched(rig) -> None:
    world = rig.world
    live = rig.issue("live")
    moved = rig.issue("moved")
    world.transfer_issue(moved, to="acme/elsewhere", at=at(3))
    pull = world.add_change_request("Fix", at=at(2))
    dup = rig.issue("duplicated")
    for _ in range(2):
        rig.store.create(
            ReportDraft(
                "dup",
                "bug",
                "origin:github",
                ACTOR,
                binding=IssueBinding("", "github", "acme/widget", dup.url, dup.identifier),
                snapshot=SourceSnapshot("t", "b", IssueStatus.OPEN, "r"),
            )
        )
    writes = rig.store.writes
    cases = [
        ("R1", "wpub-12a", "public_store_id"),
        ("R2", "bh-wjvq9", "not_a_url"),
        ("R2", "#12", "not_a_url"),
        ("R3", world.unrecognized_url(), "unknown_url"),
        ("R4", world.foreign_issue_url(), "other_project"),
        ("R5", pull.url, "change_request"),
        ("R5", pull.url.replace("/changes/", "/issues/"), "change_request"),
        ("R6", world.issue_url(99), "not_visible"),
        ("R7", moved.url, "other_project"),
        ("R8", dup.url, "duplicate_binding"),
    ]

    for rule, reference, reason in cases:
        for dry_run in (False, True):
            result = rig.import_(reference, dry_run=dry_run)
            assert (result.code, result.reason, rule) == (1, reason, rule), (reference, dry_run)
            assert result.error and result.outcome is None
            assert rig.store.writes == writes  # not one store write, ever
            rig.assert_public_store_untouched()
    assert live.url != dup.url  # the unrelated live issue was never imported
    assert [v.external_ref for v in rig.store.bound_reports()] == [dup.url, dup.url]


def test_r1_a_public_store_id_is_refused_before_any_tracker_request(rig) -> None:
    result = rig.import_("wpub-12a")

    assert (result.code, result.reason) == (1, "public_store_id")
    assert "issue URL" in result.error
    assert rig.world.read_requests() == 0  # R0 composes the adapter; R1 never calls it
    rig.assert_public_store_untouched()


# -- the store scope: operational only --


def test_the_composition_never_resolves_the_public_scope_for_any_outcome(rig) -> None:
    key = rig.issue()
    rig.import_(key.url)
    rig.import_(key.url, dry_run=True)
    rig.import_("wpub-12a")
    rig.import_(rig.world.foreign_issue_url())

    assert rig.scopes, "the composition must resolve the operational store"
    assert set(rig.scopes) == {None}  # only ever the operational store, by default scope
    assert store_target.SCOPE_PUBLIC not in rig.scopes
    assert rig.scope.public_prefixes == frozenset({"wpub"})  # recognised (R1), never opened
    assert rig.scope.operational_prefix == "w"
    rig.assert_public_store_untouched()


def test_negative_control_the_public_store_checks_bite(rig) -> None:
    """The acceptance check is not vacuous: a public write or a public resolution is detected."""
    rig.assert_public_store_untouched()

    (rig.public / ".beads" / "issues.jsonl").write_bytes(b'{"id":"wpub-1","x":1}\n')
    with pytest.raises(AssertionError):
        rig.assert_public_store_untouched()
    (rig.public / ".beads" / "issues.jsonl").write_bytes(b'{"id":"wpub-1"}\n')
    rig.assert_public_store_untouched()

    store_target.resolve_store(rig.entry, store_target.SCOPE_PUBLIC)
    with pytest.raises(AssertionError):
        rig.assert_public_store_untouched()


def test_an_unresolvable_store_is_a_clean_refusal_with_nothing_written(rig, monkeypatch) -> None:
    key = rig.issue()

    def broken(e, scope=None, **kw):
        raise store_target.StoreConfigInvalid("stores.public.path is inside the code checkout")

    monkeypatch.setattr(store_target, "resolve_store", broken)

    result = rig.import_(key.url)

    assert (result.code, result.reason) == (1, "store_unresolved")
    assert result.error == "stores.public.path is inside the code checkout"
    assert rig.store.writes == 0
    assert fingerprint(rig.public) == rig.before


# -- store failures are rendered, and a half-import is named --


def test_a_failed_create_is_a_store_error_that_imported_nothing(rig, monkeypatch) -> None:
    key = rig.issue()

    def failing(draft):
        raise IntakeStoreError("bd create failed: boom", operation="create", returncode=5)

    monkeypatch.setattr(rig.store, "create", failing)

    result = rig.import_(key.url)

    assert (result.code, result.reason) == (5, "store_error")
    assert result.error == "nothing was imported: bd create failed: boom"
    rig.assert_public_store_untouched()


def test_a_failed_stamp_names_the_held_half_import_and_a_rerun_completes_it(
    rig, monkeypatch
) -> None:
    key = rig.issue()
    real = rig.store.set_state

    def failing(bead, dimension, value, *, reason, actor):
        if dimension == "intake":
            raise IntakeStoreError("bd said no", operation="set_state", returncode=3)
        real(bead, dimension, value, reason=reason, actor=actor)

    monkeypatch.setattr(rig.store, "set_state", failing)

    result = rig.import_(key.url)

    assert (result.code, result.reason) == (3, "store_error")
    assert (
        result.report and result.report in result.error and "run the import again" in result.error
    )
    assert rig.store.ready_ids() == ()  # held from the moment it was created
    monkeypatch.setattr(rig.store, "set_state", real)
    again = rig.import_(key.url)
    assert again.outcome is not None and again.outcome.repaired == ("intake",)
    rig.assert_public_store_untouched()


def test_the_default_lock_is_the_roots_primary_guarded_file_lock(rig, monkeypatch) -> None:
    key = rig.issue()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        guard, "guard_primary", lambda hive, *, cfg, verb: calls.append((hive, verb))
    )
    monkeypatch.setattr(guard, "live_epoch", lambda hive, *, cfg: 4)

    result = rig.import_(key.url, lock=None, hive="widgets")
    rig.import_(key.url, dry_run=True, lock=None)

    assert result.code == 0
    assert calls == [("widgets", "work import")]  # the mutating import only, not the dry run
    locks = sorted(p.name for p in (rig.hive / ".bh" / "intake" / "locks").glob("*.lock"))
    assert len(locks) == 1  # one binding
    rig.assert_public_store_untouched()


def test_a_non_primary_host_is_refused_before_anything_is_written(rig, monkeypatch) -> None:
    key = rig.issue()

    def refuse(hive, *, cfg, verb):
        raise typer.Exit(1)

    monkeypatch.setattr(guard, "guard_primary", refuse)

    result = rig.import_(key.url, lock=None)

    assert (result.code, result.reason) == (1, "not_primary")
    assert "bh work import" in result.error
    assert rig.store.writes == 0
    rig.assert_public_store_untouched()


# -- the CLI rendering --


def _render(rig, capsys, reference, *, as_json=False, dry_run=False, **kwargs):
    result = rig.import_(reference, dry_run=dry_run, **kwargs)
    code = 0
    try:
        work_intake.render_import(result, reference, dry_run, as_json)
    except typer.Exit as exit_:
        code = exit_.exit_code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_render_human(rig, capsys) -> None:
    key = rig.issue()

    code, out, _ = _render(rig, capsys, key.url, dry_run=True)
    assert code == 0 and "would create a held, untriaged report for" in out and key.url in out

    code, out, _ = _render(rig, capsys, key.url)
    assert code == 0 and "imported" in out and "intake:untriaged, held" in out

    code, out, _ = _render(rig, capsys, key.url)
    assert code == 0 and "already up to date" in out


def test_render_a_refusal_names_the_code_and_exits_non_zero(rig, capsys) -> None:
    code, out, err = _render(rig, capsys, "wpub-12a")

    assert code == 1 and out == ""
    assert err.startswith("✗ import refused (public_store_id): ")


def test_render_json_is_one_stable_document(rig, capsys) -> None:
    key = rig.issue()

    code, out, _ = _render(rig, capsys, key.url, as_json=True)
    created = json.loads(out)
    code2, out2, _ = _render(rig, capsys, "bh-wjvq9", as_json=True)
    refused = json.loads(out2)

    assert code == 0 and created["ok"] is True and created["kind"] == "created"
    assert created["report"].startswith("w-") and created["url"] == key.url
    assert (code2, refused["ok"], refused["reason"]) == (1, False, "not_a_url")
    assert set(created) == set(refused)


# -- the command body: hive resolution, identity, the process wiring seam --


def _wire_command(rig, monkeypatch, *, adapters=True) -> None:
    monkeypatch.setattr(work_intake, "config", SimpleNamespace(load=lambda: rig.cfg))
    monkeypatch.setattr(registry, "resolve_hive", lambda cfg, hive: rig.entry)
    monkeypatch.setattr(registry, "current_hive", lambda cfg: rig.entry)
    monkeypatch.setattr(work_intake.identity, "resolve_actor", lambda explicit="": explicit or "x")
    monkeypatch.setattr(
        intake_services, "process_adapters", lambda cfg: rig.adapters if adapters else None
    )


def test_the_command_body_resolves_the_hive_and_imports(rig, monkeypatch, capsys) -> None:
    _wire_command(rig, monkeypatch)
    key = rig.issue()

    work_intake.import_issue(key.url, hive="widget", as_json=True, as_="dev/me")

    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True and document["kind"] == "created"
    assert {view.id for view in rig.store.bound_reports()} == {document["report"]}
    rig.assert_public_store_untouched()


def test_the_command_body_defaults_to_the_current_hive_and_honours_dry_run(
    rig, monkeypatch, capsys
) -> None:
    _wire_command(rig, monkeypatch)
    key = rig.issue()

    work_intake.import_issue(key.url, dry_run=True)

    assert "would create" in capsys.readouterr().out
    assert rig.store.writes == 0
    rig.assert_public_store_untouched()


def test_the_command_body_refuses_with_a_nonzero_exit_while_no_wiring_exists(
    rig, monkeypatch, capsys
) -> None:
    _wire_command(rig, monkeypatch, adapters=False)
    key = rig.issue()

    with pytest.raises(typer.Exit) as exit_:
        work_intake.import_issue(key.url)

    assert exit_.value.exit_code == 1
    assert "import refused (intake_disabled)" in capsys.readouterr().err
    assert rig.store.writes == 0
    rig.assert_public_store_untouched()


def test_the_command_body_outside_a_managed_hive_says_so(rig, monkeypatch, capsys) -> None:
    _wire_command(rig, monkeypatch)
    monkeypatch.setattr(registry, "current_hive", lambda cfg: None)

    with pytest.raises(typer.Exit) as exit_:
        work_intake.import_issue("https://example.test/acme/widget/issues/1")

    assert exit_.value.exit_code == 1 and "pass --hive" in capsys.readouterr().err


def test_the_typer_function_delegates_without_being_a_catalogued_verb(rig, monkeypatch) -> None:
    """``work.import_cmd`` is the command body, ready for registration; until the wire release that
    registers it (bh-2ef4g) it is deliberately not in the catalog-derived ``CLI_HANDLERS``."""
    from beadhive import work

    seen: list[tuple] = []
    monkeypatch.setattr(work_intake, "import_issue", lambda *args: seen.append(args))

    work.import_cmd("https://x/y/issues/1", "widget", True, True, "dev/me")

    assert seen == [("https://x/y/issues/1", "widget", True, True, "dev/me")]
    assert "work.import" not in work.CLI_HANDLERS
