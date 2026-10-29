"""``bh work link`` and ``accept --work`` / ``--new-work``: the composition root, end to end.

``intake_services.link_report`` / ``accept_to_work`` compose the config switch, the OPERATIONAL
``ReportStore`` (the in-memory one, standing in for bd) and the root lock. What only the
composition adds is pinned here, on the same public-store fixture as the import composition:

* off by default: every link, unlink and linking accept refuses ``intake_disabled`` and builds no
  store; the plain accept is untouched;
* acceptance item 5 of external-intake-module-adr.md: the composition never resolves
  ``SCOPE_PUBLIC`` and the byte-fingerprinted public-store fixture is IDENTICAL after every case,
  refusals included; no edge crosses stores;
* no tracker is composed or written (the world sees zero write requests);
* the command bodies render, and the Typer functions are catalogued verbs since wire 2.16.0.

No live GitHub, no clock, no sleep.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import typer
from tests.test_intake_import_composition import ACTOR, Rig

from beadhive import guard, intake_services, registry, work_intake
from beadhive.modules.intake import IntakeStoreError
from beadhive.testing.intake import draft


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def report(rig: Rig, title: str = "login is broken") -> str:
    bead = rig.store.create(draft(title, bound=True))
    rig.store.set_state(bead, "intake", "untriaged", reason="imported", actor=ACTOR)
    return bead


def work(rig: Rig, title: str = "fix the crash") -> str:
    return rig.store.create(draft(title))


def link(rig: Rig, bead: str, to: str, *, cfg=None, **kwargs):
    return intake_services.link_report(
        rig.cfg if cfg is None else cfg,
        rig.entry,
        rig.hive,
        bead,
        to,
        ACTOR,
        **{"lock": rig.lock, **kwargs},
    )


def accept(rig: Rig, bead: str, *, cfg=None, **kwargs):
    return intake_services.accept_to_work(
        rig.cfg if cfg is None else cfg,
        rig.entry,
        rig.hive,
        bead,
        ACTOR,
        **{"lock": rig.lock, **kwargs},
    )


# -- off by default ----------------------------------------------------------------------------


def test_everything_refuses_as_disabled_when_intake_is_off_and_builds_no_store(rig) -> None:
    one, target = report(rig), work(rig)
    off = {k: v for k, v in rig.cfg.items() if k != "intake"}
    built, writes = rig.builds, rig.store.writes

    results = (
        link(rig, one, target, cfg=off),
        link(rig, one, "", cfg=off, remove=True),
        accept(rig, one, cfg=off, work=target),
        accept(rig, one, cfg=off, new_work=True),
    )

    for result in results:
        assert (result.code, result.reason) == (1, "intake_disabled")
        assert "opt-in" in result.error
    assert rig.builds == built and rig.store.writes == writes and rig.lock.holds == []
    rig.assert_public_store_untouched()


def test_an_explicitly_false_switch_is_off_too(rig) -> None:
    one, target = report(rig), work(rig)
    cfg = {**rig.cfg, "intake": {"external": {"enabled": False}}}

    assert link(rig, one, target, cfg=cfg).reason == "intake_disabled"


# -- the acceptance, through the root -----------------------------------------------------------


def test_two_reports_link_to_one_work_and_unlinking_one_leaves_the_other(rig) -> None:
    first, second, target = report(rig, "first"), report(rig, "second"), work(rig)

    one, two = link(rig, first, target), link(rig, second, target)
    gone = link(rig, first, "", remove=True)

    assert [r.code for r in (one, two, gone)] == [0, 0, 0]
    assert one.outcome.kind.value == "linked" and one.outcome.delivery == "pending"
    assert gone.outcome.kind.value == "unlinked" and gone.outcome.work == target
    assert rig.store.trackers_of(target) == (second,)
    assert rig.store.tracked_work(second) == target
    # the report stays held, never ready work; the work bead is dispatchable
    assert first not in rig.store.ready_ids() and second not in rig.store.ready_ids()
    assert target in rig.store.ready_ids()
    # one lock per mutating call, keyed on the binding
    assert rig.lock.holds == ["memory:acme/widgets"] * 3
    rig.assert_public_store_untouched()


def test_accept_new_work_and_accept_work_through_the_root(rig) -> None:
    first, second = report(rig, "first"), report(rig, "second")

    made = accept(rig, first, new_work=True, issue_type="task", priority="1")
    joined = accept(rig, second, work=made.disposition.work)

    assert (made.code, joined.code) == (0, 0)
    assert made.disposition.created_work and not joined.disposition.created_work
    target = made.disposition.work
    assert rig.store.trackers_of(target) == tuple(sorted((first, second)))
    assert target in rig.store.ready_ids() and not {first, second} & set(rig.store.ready_ids())
    rig.assert_public_store_untouched()


@pytest.mark.parametrize(
    ("to", "reason"),
    [
        ("wpub-12a", "public_store_id"),
        ("other-abc1", "cross_store_link"),
        ("w-ghost", "cross_store_link"),
    ],
)
def test_no_edge_crosses_stores_and_every_refusal_leaves_the_public_store_alone(
    rig, to, reason
) -> None:
    one = report(rig)
    writes = rig.store.writes

    result = link(rig, one, to)

    assert (result.code, result.reason) == (1, reason)
    assert rig.store.writes == writes and rig.store.tracked_work(one) is None
    rig.assert_public_store_untouched()


def test_a_bound_report_is_refused_as_a_target_with_the_stable_reason(rig) -> None:
    one, other = report(rig, "a"), report(rig, "b")

    result = link(rig, one, other)

    assert (result.code, result.reason) == (1, "target_is_report")
    assert "accept --new-work" in result.error
    assert accept(rig, one, work=other).reason == "target_is_report"
    rig.assert_public_store_untouched()


def test_a_store_failure_is_rendered_with_the_retry_hint(rig, monkeypatch) -> None:
    one, target = report(rig), work(rig)

    def failing(report_id, work_id, *, actor):
        raise IntakeStoreError("bd said no", bead=report_id, operation="track", returncode=4)

    monkeypatch.setattr(rig.store, "track", failing)

    result = link(rig, one, target)

    assert (result.code, result.reason, result.bead) == (4, "store_error", one)
    assert "could not track" in result.error and "idempotent" in result.error
    rig.assert_public_store_untouched()


def test_the_default_lock_is_the_roots_primary_guarded_file_lock(rig, monkeypatch) -> None:
    one, target = report(rig), work(rig)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        guard, "guard_primary", lambda hive, *, cfg, verb: calls.append((hive, verb))
    )

    result = link(rig, one, target, lock=None, hive="widgets")
    accepted = accept(rig, report(rig, "other"), new_work=True, lock=None, hive="widgets")

    assert result.code == 0 and accepted.code == 0
    assert calls == [("widgets", "work link"), ("widgets", "work accept")]
    assert len(list((rig.hive / ".bh" / "intake" / "locks").glob("*.lock"))) == 1


def test_a_non_primary_host_is_refused_before_anything_is_written(rig, monkeypatch) -> None:
    one, target = report(rig), work(rig)

    def refuse(hive, *, cfg, verb):
        raise typer.Exit(1)

    monkeypatch.setattr(guard, "guard_primary", refuse)
    writes = rig.store.writes

    result = link(rig, one, target, lock=None)

    assert (result.code, result.reason) == (1, "not_primary") and "bh work link" in result.error
    assert rig.store.writes == writes


# -- the CLI rendering and the command bodies --------------------------------------------------


def _render(result, bead, *, as_json=False, capsys):
    code = 0
    try:
        work_intake.render_link(result, bead, as_json)
    except typer.Exit as exit_:
        code = exit_.exit_code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_render_human(rig, capsys) -> None:
    one, two, target = report(rig, "a"), report(rig, "b"), work(rig)

    code, out, _ = _render(link(rig, one, target), one, capsys=capsys)
    assert code == 0 and f"{one} now tracks {target} (delivery:pending)" in out
    code, out, _ = _render(link(rig, one, target), one, capsys=capsys)
    assert "already tracks" in out
    code, out, _ = _render(link(rig, one, "", remove=True), one, capsys=capsys)
    assert f"unlinked {one} from {target}" in out and "delivery:pending is kept" in out
    code, out, _ = _render(link(rig, one, "", remove=True), one, capsys=capsys)
    assert "nothing to unlink" in out
    code, out, _ = _render(accept(rig, two, new_work=True), two, capsys=capsys)
    assert "created and tracks" in out and f"{two} stays held" in out


def test_render_a_refusal_names_the_code_and_exits_non_zero(rig, capsys) -> None:
    one = report(rig)

    code, out, err = _render(link(rig, one, "wpub-12a"), one, capsys=capsys)

    assert code == 1 and out == "" and err.startswith("✗ link refused (public_store_id): ")


def test_render_json_is_one_stable_document(rig, capsys) -> None:
    one, target = report(rig), work(rig)

    _, out, _ = _render(link(rig, one, target), one, as_json=True, capsys=capsys)
    linked = json.loads(out)
    _, out, _ = _render(
        accept(rig, report(rig, "b"), new_work=True), "b", as_json=True, capsys=capsys
    )
    accepted = json.loads(out)
    code, out, _ = _render(link(rig, one, "wpub-1a"), one, as_json=True, capsys=capsys)
    refused = json.loads(out)

    assert (linked["ok"], linked["kind"], linked["work"], linked["delivery"]) == (
        True,
        "linked",
        target,
        "pending",
    )
    assert accepted["accepted"] and accepted["created_work"] and accepted["work"]
    assert (code, refused["ok"], refused["reason"]) == (1, False, "public_store_id")
    assert set(linked) == set(accepted) == set(refused)


def _wire(rig, monkeypatch) -> None:
    monkeypatch.setattr(work_intake, "config", SimpleNamespace(load=lambda: rig.cfg))
    monkeypatch.setattr(registry, "resolve_hive", lambda cfg, hive: rig.entry)
    monkeypatch.setattr(registry, "current_hive", lambda cfg: rig.entry)
    monkeypatch.setattr(work_intake.identity, "resolve_actor", lambda explicit="": explicit or "x")
    monkeypatch.setattr(intake_services, "intake_lock", lambda *a, **k: rig.lock)


def test_the_link_command_body_links_and_unlinks(rig, monkeypatch, capsys) -> None:
    _wire(rig, monkeypatch)
    one, target = report(rig), work(rig)

    work_intake.link(one, to=target, hive="widget", as_json=True, as_="dev/me")
    linked = json.loads(capsys.readouterr().out)
    work_intake.link(one, remove=True, as_json=True)
    removed = json.loads(capsys.readouterr().out)

    assert (linked["ok"], linked["kind"]) == (True, "linked")
    assert (removed["kind"], removed["work"]) == ("unlinked", target)
    assert rig.store.tracked_work(one) is None
    rig.assert_public_store_untouched()


def test_the_link_command_body_needs_exactly_one_of_to_and_remove(rig, monkeypatch, capsys) -> None:
    _wire(rig, monkeypatch)
    one = report(rig)

    for kwargs in ({}, {"to": "w-1", "remove": True}):
        with pytest.raises(typer.Exit) as exit_:
            work_intake.link(one, **kwargs)
        assert exit_.value.exit_code == 2
    assert "exactly one of --to" in capsys.readouterr().err
    assert rig.store.tracked_work(one) is None


def test_the_accept_command_body_routes_work_options_and_leaves_a_plain_accept_alone(
    rig, monkeypatch, capsys
) -> None:
    _wire(rig, monkeypatch)
    one = report(rig)

    work_intake.accept(one, hive="widget", new_work=True)

    assert "created and tracks" in capsys.readouterr().out
    assert rig.store.tracked_work(one) is not None

    seen: list[tuple] = []
    monkeypatch.setattr(
        work_intake.triage, "accept", lambda *a, **k: seen.append((a, k)) or (0, "", "ok")
    )
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: rig.hive)
    work_intake.accept("w-9", "bug", "1", "dev/me", "widget")

    assert seen and seen[0][1] == {"issue_type": "bug", "priority": "1"}  # the plain path


def test_the_typer_functions_delegate_and_are_catalogued_verbs(rig, monkeypatch) -> None:
    """``work.link_cmd`` is the command body, registered by wire 2.16.0 (bh-73td2), and
    ``accept_cmd`` keeps its published parameters and appends ``--work`` / ``--new-work``."""
    import inspect

    from beadhive import work

    seen: list[tuple] = []
    monkeypatch.setattr(work_intake, "link", lambda *args: seen.append(args))

    work.link_cmd("w-1", "w-2", False, True, "dev/me", "widget", True)

    assert seen == [("w-1", "w-2", False, True, "dev/me", "widget", True)]
    assert "work.link" in work.CLI_HANDLERS and "work link" in work.CLI_PROJECTION.paths
    assert list(inspect.signature(work.accept_cmd).parameters) == [
        "bead",
        "type_",
        "priority",
        "as_",
        "hive",
        "work",
        "new_work",
    ]
