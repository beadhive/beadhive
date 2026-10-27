"""``bh work discover``: the composition root, end to end, on the import composition's rig.

``intake_services.discover`` composes the config switch, the tracker the hive's public-store
binding serves (a real ``TrackerAdapters`` over ``MemoryTracker``), the operational ``ReportStore``
(in-memory, standing in for bd), the root lock and the file-backed checkpoint in the hive's private
``.bh/``. What only the composition adds is pinned here:

* off by default (``intake_disabled``, nothing composed) and refused, naming ``bh-kzs0b``, while no
  production credential wiring exists;
* acceptance item 5 of external-intake-module-adr.md: the composition never resolves
  ``SCOPE_PUBLIC`` and the byte-fingerprinted public-store fixture is IDENTICAL after every case;
  the world sees zero tracker write requests;
* the checkpoint is a file under ``.bh/intake/checkpoints`` and a later call resumes from it;
  ``--full`` re-pulls closed issues;
* the command body and renderer, and the Typer function that is NOT a catalogued verb.

No live GitHub, no clock, no sleep.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import typer
from tests.test_intake_import_composition import ACTOR, Rig

from beadhive import guard, intake_services, registry, work_intake
from beadhive.modules.intake import Checkpoint, IntakeStoreError
from beadhive.modules.tracker.contracts import IssueState
from beadhive.testing.intake import MemoryCheckpointStore
from beadhive.testing.tracker import at


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def discover(rig: Rig, *, full: bool = False, cfg=None, **kwargs):
    kwargs.setdefault("adapters", rig.adapters)
    return intake_services.discover(
        rig.cfg if cfg is None else cfg,
        rig.entry,
        rig.hive,
        ACTOR,
        full=full,
        **{"lock": rig.lock, **kwargs},
    )


def marks(rig: Rig) -> list:
    return sorted((rig.hive / ".bh" / "intake" / "checkpoints").glob("*.json"))


# -- the happy path --


def test_discover_through_the_bound_adapter_lands_held_untriaged_github_reports(rig) -> None:
    rig.issue("Login is broken")
    rig.issue("Old crash", state=IssueState.CLOSED)

    result = discover(rig)

    assert (result.code, result.error, result.reason) == (0, "", "")
    assert result.outcome is not None and result.outcome.created == 2
    assert len(rig.store.bound_reports()) == 2
    for view in rig.store.bound_reports():
        assert {"intake:untriaged", "origin:github"} <= set(view.labels)
    assert rig.store.ready_ids() == ()
    rig.assert_public_store_untouched()


def test_the_checkpoint_is_a_private_file_and_a_later_call_resumes_from_it(rig) -> None:
    for day in (1, 2, 3):
        rig.world.add_issue(f"Issue {day}", at=at(day))

    first = discover(rig)
    assert first.outcome is not None and first.outcome.created == 3
    assert len(marks(rig)) == 1 and not marks(rig)[0].is_relative_to(rig.public)
    rig.world.add_issue("Issue 4", at=at(4))
    writes = rig.store.writes

    second = discover(rig)  # a fresh composition: only the file carries the mark across
    third = discover(rig)

    assert second.outcome is not None and third.outcome is not None
    assert (second.outcome.created, second.outcome.unchanged) == (1, 1)  # since= was honoured
    assert (third.outcome.created, third.outcome.updated) == (0, 0)
    assert rig.store.writes > writes
    rig.assert_public_store_untouched()


def test_discover_is_idempotent_and_writes_nothing_the_second_time(rig) -> None:
    rig.issue("One")
    discover(rig)
    writes, files = rig.store.writes, [p.read_bytes() for p in marks(rig)]

    again = discover(rig)

    assert again.outcome is not None and (again.outcome.created, again.outcome.updated) == (0, 0)
    assert rig.store.writes == writes and [p.read_bytes() for p in marks(rig)] == files
    rig.assert_public_store_untouched()


def test_full_re_pulls_closed_issues_a_stale_checkpoint_hides(rig) -> None:
    rig.issue("Open one", state=IssueState.OPEN)
    discover(rig)
    rig.world.add_issue("Late closed", at=at(1), state=IssueState.CLOSED)
    intake_services.FileCheckpointStore(
        lambda: intake_services.checkpoint_dir(rig.hive)
    ).put(  # a mark ahead of the late issue
        f"issues:memory:{rig.world.locator}", Checkpoint("memory:" + at(9).isoformat())
    )

    stale = discover(rig)
    full = discover(rig, full=True)

    assert stale.outcome is not None and stale.outcome.created == 0
    assert full.outcome is not None and full.outcome.created == 1 and full.outcome.reconciled
    assert full.full is True
    rig.assert_public_store_untouched()


def test_an_injected_checkpoint_store_replaces_the_file(rig) -> None:
    rig.issue("One")
    memory = MemoryCheckpointStore()

    result = discover(rig, checkpoints=memory)

    assert result.code == 0 and len(memory.puts) == 1 and marks(rig) == []
    rig.assert_public_store_untouched()


def test_an_explicit_tracker_port_works_without_the_adapter_root(rig) -> None:
    from beadhive.testing.tracker import MemoryTracker

    rig.issue("One")

    result = discover(rig, adapters=None, tracker=MemoryTracker(rig.world))

    assert result.code == 0 and len(rig.store.bound_reports()) == 1
    rig.assert_public_store_untouched()


def test_the_configured_default_type_files_the_report(rig) -> None:
    cfg = {**rig.cfg, "intake": {"external": {"enabled": True, "default_type": "chore"}}}
    rig.issue("One")

    discover(rig, cfg=cfg)

    (view,) = rig.store.bound_reports()
    assert view.issue_type == "chore"


# -- off by default, and the refusals that name the fix --


@pytest.mark.parametrize(
    "intake",
    [
        None,
        {"external": {}},
        {"external": {"enabled": False}},
        {"external": {"enabled": "true"}},
        {"external": {"enabled": False, "discovery": {"enabled": True, "interval_seconds": 60}}},
    ],
    ids=["absent", "no-switch", "off", "not-a-boolean", "timer-on-but-intake-off"],
)
def test_intake_is_off_unless_the_switch_is_literally_true(rig, intake) -> None:
    rig.issue("One")
    cfg = {"managed_repos": [rig.entry], **({"intake": intake} if intake is not None else {})}

    result = discover(rig, cfg=cfg)

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "opt-in" in result.error
    assert rig.builds == 0 and rig.world.read_requests() == 0 and marks(rig) == []
    rig.assert_public_store_untouched()


def test_without_production_wiring_discover_says_so_and_does_nothing(rig) -> None:
    rig.issue("One")

    result = discover(rig, adapters=None)  # process_adapters() is None until bh-kzs0b

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "bh-kzs0b" in result.error
    assert rig.store.writes == 0 and marks(rig) == []
    rig.assert_public_store_untouched()


def test_a_store_that_binds_no_tracker_is_intake_disabled(rig) -> None:
    rig.issue("One")
    rig.entry = {**rig.entry, "stores": {**rig.entry["stores"], "public": {"prefix": "wpub"}}}

    result = discover(rig)

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "binds no tracker adapter" in result.error
    rig.assert_public_store_untouched()


def test_a_disabled_issues_read_names_the_key_to_set(rig) -> None:
    from tests.unit.bootstrap.test_tracker_composition import plugin, roots

    rig.issue("One")
    adapters, factory = roots({"tracker-github": plugin(issues=False)})

    result = discover(rig, adapters=adapters)

    assert (result.code, result.reason) == (1, "intake_disabled")
    assert "read.issues.enabled" in result.error
    assert rig.store.writes == 0 and factory.world.write_requests() == 0
    rig.assert_public_store_untouched()


# -- the lock and the store failures --


def test_the_default_lock_is_the_roots_primary_guarded_file_lock(rig, monkeypatch) -> None:
    rig.issue("One")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        guard, "guard_primary", lambda hive, *, cfg, verb: calls.append((hive, verb))
    )

    result = discover(rig, lock=None, hive="widgets")

    assert result.code == 0 and calls == [("widgets", "work discover")]
    assert len(list((rig.hive / ".bh" / "intake" / "locks").glob("*.lock"))) == 1
    rig.assert_public_store_untouched()


def test_a_non_primary_host_is_refused_before_anything_is_written(rig, monkeypatch) -> None:
    rig.issue("One")

    def refuse(hive, *, cfg, verb):
        raise typer.Exit(1)

    monkeypatch.setattr(guard, "guard_primary", refuse)

    result = discover(rig, lock=None)

    assert (result.code, result.reason) == (1, "not_primary")
    assert "bh work discover" in result.error
    assert rig.store.writes == 0 and marks(rig) == []
    rig.assert_public_store_untouched()


def test_a_store_failure_is_rendered_and_the_checkpoint_does_not_pass_it(rig, monkeypatch) -> None:
    rig.issue("One")

    def failing(draft):
        raise IntakeStoreError("bd create failed: boom", operation="create", returncode=5)

    real = rig.store.create
    monkeypatch.setattr(rig.store, "create", failing)

    result = discover(rig)

    assert (result.code, result.reason) == (5, "store_error")
    assert "bd create failed: boom" in result.error and "did not move" in result.error
    assert marks(rig) == []
    monkeypatch.setattr(rig.store, "create", real)
    assert discover(rig).code == 0 and len(rig.store.bound_reports()) == 1
    rig.assert_public_store_untouched()


def test_an_unresolvable_store_is_a_clean_refusal_with_nothing_written(rig, monkeypatch) -> None:
    from beadhive import store_target

    rig.issue("One")

    def broken(e, scope=None, **kw):
        raise store_target.StoreConfigInvalid("stores.public.path is inside the code checkout")

    monkeypatch.setattr(store_target, "resolve_store", broken)

    result = discover(rig)

    assert (result.code, result.reason) == (1, "store_unresolved")
    assert rig.store.writes == 0


# -- discovery_settings: the timer's switches --


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({}, (False, 0.0, 24)),
        ({"intake": {"external": {"enabled": True}}}, (False, 0.0, 24)),
        (
            {"intake": {"external": {"enabled": True, "discovery": {"enabled": True}}}},
            (False, 0.0, 24),  # no cadence chosen: no timer
        ),
        (
            {"intake": {"external": {"discovery": {"enabled": True, "interval_seconds": 60}}}},
            (False, 0.0, 24),  # intake itself off
        ),
        (
            {"intake": {"external": {"enabled": True, "discovery": {"interval_seconds": 60}}}},
            (False, 0.0, 24),  # timer switch off
        ),
        (
            {
                "intake": {
                    "external": {
                        "enabled": True,
                        "discovery": {
                            "enabled": True,
                            "interval_seconds": 900,
                            "full_reconcile_every": 6,
                        },
                    }
                }
            },
            (True, 900.0, 6),
        ),
        (
            {
                "intake": {
                    "external": {
                        "enabled": "true",
                        "discovery": {"enabled": True, "interval_seconds": 60},
                    }
                }
            },
            (False, 0.0, 24),
        ),
        (
            {
                "intake": {
                    "external": {
                        "enabled": True,
                        "discovery": {"enabled": True, "interval_seconds": True},
                    }
                }
            },
            (False, 0.0, 24),
        ),
    ],
)
def test_discovery_settings_are_off_unless_everything_is_explicit(cfg, expected) -> None:
    settings = intake_services.discovery_settings(cfg)

    assert (settings.enabled, settings.interval_seconds, settings.full_reconcile_every) == expected


# -- the CLI rendering, the command body and the (unregistered) Typer function --


def _render(rig, capsys, *, as_json=False, full=False, **kwargs):
    result = discover(rig, full=full, **kwargs)
    code = 0
    try:
        work_intake.render_discover(result, as_json)
    except typer.Exit as exit_:
        code = exit_.exit_code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_render_human(rig, capsys) -> None:
    rig.issue("One")

    code, out, _ = _render(rig, capsys)
    assert code == 0 and "✓ discover (incremental): 1 created, 0 updated, 0 unchanged" in out
    assert "→ w-" in out

    code, out, _ = _render(rig, capsys, full=True)
    assert code == 0 and "discover (full pass)" in out


def test_render_a_refusal_names_the_code_and_exits_non_zero(rig, capsys) -> None:
    code, out, err = _render(rig, capsys, adapters=None)

    assert code == 1 and out == ""
    assert err.startswith("✗ discover refused (intake_disabled): ")


def test_render_json_is_one_stable_document(rig, capsys) -> None:
    rig.issue("One")

    code, out, _ = _render(rig, capsys, as_json=True)
    ok = json.loads(out)
    code2, out2, _ = _render(rig, capsys, as_json=True, adapters=None)
    refused = json.loads(out2)

    assert (
        code == 0 and ok["ok"] is True and (ok["created"], ok["checkpoint_advanced"]) == (1, True)
    )
    assert (code2, refused["ok"], refused["reason"]) == (1, False, "intake_disabled")
    assert set(ok) == set(refused)


def _wire_command(rig, monkeypatch, *, adapters=True) -> None:
    monkeypatch.setattr(work_intake, "config", SimpleNamespace(load=lambda: rig.cfg))
    monkeypatch.setattr(registry, "resolve_hive", lambda cfg, hive: rig.entry)
    monkeypatch.setattr(registry, "current_hive", lambda cfg: rig.entry)
    monkeypatch.setattr(work_intake.identity, "resolve_actor", lambda explicit="": explicit or "x")
    monkeypatch.setattr(
        intake_services, "process_adapters", lambda cfg: rig.adapters if adapters else None
    )
    real = intake_services.discover
    monkeypatch.setattr(
        intake_services, "discover", lambda *a, **k: real(*a, **{"lock": rig.lock, **k})
    )


def test_the_command_body_resolves_the_hive_and_discovers(rig, monkeypatch, capsys) -> None:
    _wire_command(rig, monkeypatch)
    rig.issue("One")

    work_intake.discover(hive="widget", full=True, as_json=True, as_="dev/me")

    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True and document["full"] is True and document["created"] == 1
    assert document["reconciled"] is True
    rig.assert_public_store_untouched()


def test_the_command_body_refuses_with_a_nonzero_exit_while_no_wiring_exists(
    rig, monkeypatch, capsys
) -> None:
    _wire_command(rig, monkeypatch, adapters=False)
    rig.issue("One")

    with pytest.raises(typer.Exit) as exit_:
        work_intake.discover()

    assert exit_.value.exit_code == 1
    assert "discover refused (intake_disabled)" in capsys.readouterr().err
    rig.assert_public_store_untouched()


def test_the_command_body_outside_a_managed_hive_says_so(rig, monkeypatch, capsys) -> None:
    _wire_command(rig, monkeypatch)
    monkeypatch.setattr(registry, "current_hive", lambda cfg: None)

    with pytest.raises(typer.Exit) as exit_:
        work_intake.discover()

    assert exit_.value.exit_code == 1 and "pass --hive" in capsys.readouterr().err


def test_the_typer_function_delegates_without_being_a_catalogued_verb(monkeypatch) -> None:
    """``work.discover_cmd`` is the command body, ready for registration; until the wire release
    that registers it (bh-df64b) it is deliberately not in the catalog-derived ``CLI_HANDLERS``."""
    from beadhive import work

    seen: list[tuple] = []
    monkeypatch.setattr(work_intake, "discover", lambda *args: seen.append(args))

    work.discover_cmd("widget", True, True, "dev/me")

    assert seen == [("widget", True, True, "dev/me")]
    assert "work.discover" not in work.CLI_HANDLERS
