"""``guard_primary()`` — the multi-host write gate on the write verbs (bh-ytbb.9).

Covers the acceptance bar directly:
  * ``guard_primary(hive)`` lives in ``guard.py`` beside ``guard_hq_registry_write`` /
    ``guard_hub`` and follows the same shape (decide, echo, ``typer.Exit(1)``).
  * it is called from ``bh work assign|claim|submit|merge`` **and** ``bh plan file``;
  * reads — ready / list / show / brief / sync — are NEVER gated;
  * the refusal names the current holder and its expiry.

Plus the bead's spec-review requirement: the lease refusal must be **distinguishable** from
bd's pre-existing post-merge close failure (bh-r8el, ``cannot close: assignee is dev/X, actor
is <human>``), and ``guard_primary`` must not fire on the ``bd close --force`` retry an
operator runs afterwards.

The lease store is a scratch HQ clone under ``tmp_path`` with ``BH_HQ`` pointed at it — the
operator's real HQ is never read or written.
"""

from __future__ import annotations

import subprocess

import pytest
import typer

from beadhive import cli, config, gitref, guard, host, host_lease, plan, registry, work, work_logic

PREFIX = "tt"
THIS_HOST = "11111111-1111-4111-8111-111111111111"
OTHER_HOST = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0

# bd's own post-merge close failure (bh-r8el), verbatim in shape — the string this guard's
# refusal must never be confused with, and must never cause.
BD_CLOSE_FAILURE = "cannot close: assignee is dev/lease, actor is brian"


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


@pytest.fixture
def hq(tmp_path, monkeypatch):
    """A scratch HQ clone this host's cached lease lives in (BH_HQ points here)."""
    path = tmp_path / "hq"
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    monkeypatch.setenv("BH_HQ", str(path))
    return path


@pytest.fixture
def this_host(monkeypatch):
    monkeypatch.setattr(host, "host_id", lambda: THIS_HOST)
    return THIS_HOST


@pytest.fixture
def hive(tmp_path, monkeypatch):
    """A registered hive whose prefix the guard resolves to."""
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}
    monkeypatch.setattr(registry, "hive_dir_for", lambda _cfg, _hive: tmp_path / "hive")
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    return entry


def _record_lease(hq_dir, lease):
    """Install a cached host lease at the LOCAL refs/bh/lease/<prefix> in the HQ clone —
    exactly what a won adopt CAS mirrors down (`host_lease.cache`)."""
    sha = gitref.write_object(lease.to_record(), cwd=hq_dir)
    gitref.set_local(host_lease.lease_ref(PREFIX), sha, cwd=hq_dir)


def _lease(host_id, *, epoch=1, ttl=600.0, label="deskmac"):
    return host_lease.HostLease(
        host_id=host_id,
        label=label,
        epoch=epoch,
        adopted_at=host_lease.now_stamp(T0),
        expires_at=host_lease.now_stamp(T0 + ttl),
    )


# ---- the decision ----------------------------------------------------------------


def test_guard_primary_lives_in_guard_py_beside_the_existing_guards():
    assert callable(guard.guard_primary)
    assert callable(guard.guard_hq_registry_write)
    assert callable(guard.guard_hub)


def test_an_unadopted_factory_is_never_gated(hq, hive, this_host):
    """No lease has ever been taken: single-host default, behavior unchanged."""
    guard.guard_primary("", cfg={})  # no raise


def test_no_hq_clone_on_this_host_is_never_gated(tmp_path, hive, this_host, monkeypatch):
    monkeypatch.setenv("BH_HQ", str(tmp_path / "absent-hq"))
    guard.guard_primary("", cfg={})  # no raise


def test_this_hosts_live_lease_is_allowed(hq, hive, this_host, monkeypatch):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(THIS_HOST))
    guard.guard_primary("", cfg={})  # no raise


def test_the_held_by_branch_renews_via_host_lease_and_hq_dir_resolved_locally(
    hq, hive, this_host, monkeypatch
):
    """Regression (bh-ytbb.16): bh-ytbb.9's ``primary_state()`` extraction moved the
    ``host_lease`` import and ``hq_dir`` computation into ``primary_state``'s own scope;
    bh-ytbb.11's renewal wiring in the ``held_by`` branch then referenced both names as if
    they were still in ``guard_primary``'s scope. Both merged clean individually — the break
    only appeared once combined (caught by ``ruff`` F821, not by any test). This calls
    ``guard_primary()`` end to end while THIS host holds the lease (exercising the exact
    ``held_by`` branch that dereferences ``host_lease``/``hq_dir``) and spies on
    ``host_lease.renew_if_due`` so the test fails on a bare ``NameError`` scope break AND on
    the renewal call being silently dropped altogether."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(THIS_HOST))

    calls: list[tuple[tuple, dict]] = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return None  # stand in for "not due yet" — no real renewal attempted

    monkeypatch.setattr(host_lease, "renew_if_due", spy)

    guard.guard_primary("", cfg={})  # must not NameError on host_lease / hq_dir

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ("origin", PREFIX)
    assert kwargs["host_id"] == THIS_HOST
    assert kwargs["cwd"] == hq  # guard_primary resolved the SAME hq_dir primary_state read


def test_a_foreign_live_lease_is_refused(hq, hive, this_host, monkeypatch, capsys):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    err = capsys.readouterr().err
    assert guard.PRIMARY_REFUSAL_MARKER in err


def test_the_refusal_names_the_holder_and_its_expiry(hq, hive, this_host, monkeypatch, capsys):
    """Without both, an operator is blocked with no next action."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST, label="deskmac", ttl=600.0))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    err = capsys.readouterr().err
    assert OTHER_HOST in err  # who
    assert "deskmac" in err  # its human label
    assert host_lease.now_stamp(T0 + 600.0) in err  # until when


def test_this_hosts_LAPSED_lease_is_refused_fail_closed(hq, hive, this_host, monkeypatch):
    """A lapsed lease is exactly the window another host may have taken over in."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 9999)
    _record_lease(hq, _lease(THIS_HOST, ttl=600.0))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})


def test_a_released_tombstone_is_refused(hq, hive, this_host, monkeypatch, capsys):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, host_lease.HostLease("", "", 3, "t", host_lease.now_stamp(T0)))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    assert "nobody currently holds it" in capsys.readouterr().err


def test_a_host_with_no_minted_identity_can_hold_nothing(hq, hive, monkeypatch):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    monkeypatch.setattr(host, "host_id", _raise_missing)
    _record_lease(hq, _lease(OTHER_HOST))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})


def _raise_missing():
    raise FileNotFoundError("host.yaml not minted")


def test_an_unresolvable_hive_is_not_this_guards_error_to_raise(hq, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no such hive")

    monkeypatch.setattr(registry, "hive_dir_for", boom)
    guard.guard_primary("nope", cfg={})  # no raise: hive resolution errors belong to the verb


# ---- which verbs are gated ---------------------------------------------------------


def _gated_calls(monkeypatch):
    """Record every guard_primary call a verb makes, and stop the verb right there."""
    calls: list[str] = []

    class _Stop(Exception):
        pass

    def spy(hive="", *, cfg=None, verb=""):
        calls.append(verb)
        raise _Stop

    monkeypatch.setattr(guard, "guard_primary", spy)
    monkeypatch.setattr(work.guard, "guard_primary", spy)
    monkeypatch.setattr(plan.guard, "guard_primary", spy)
    return calls, _Stop


@pytest.mark.parametrize(
    ("verb", "call"),
    [
        ("work assign", lambda: work.assign(bead="tt-1", to="dev/x", hive="h")),
        ("work claim", lambda: work.claim(bead="tt-1", hive="h")),
        ("work submit", lambda: work.submit(bead="tt-1", hive="h")),
        ("work merge", lambda: work.merge(bead="tt-1", hive="h")),
    ],
)
def test_each_write_verb_calls_guard_primary(verb, call, monkeypatch):
    calls, stop = _gated_calls(monkeypatch)
    monkeypatch.setattr(work.config, "load", lambda: {})
    monkeypatch.setattr(work.otel, "set_bead", lambda *_a, **_k: None)
    with pytest.raises(stop):
        call()
    assert calls == [verb]


def test_plan_file_calls_guard_primary(tmp_path, monkeypatch):
    """The non-obvious, most important one: filing a molecule from a follower is the
    beads#4796 trigger, not an edge case."""
    calls, stop = _gated_calls(monkeypatch)
    spec = tmp_path / "m.yaml"
    spec.write_text("x")
    monkeypatch.setattr(plan.config, "load", lambda: {})
    monkeypatch.setattr(plan.registry, "hive_dir_for", lambda *_a: tmp_path)
    monkeypatch.setattr(plan.molecule, "load_spec", lambda _s: {"epic": {}, "issues": []})
    monkeypatch.setattr(plan.molecule, "validate_or_raise", lambda *_a, **_k: None)
    with pytest.raises(stop):
        plan.file(spec=str(spec), dry_run=False, save="", hive="h")
    assert calls == ["plan file"]


def test_plan_file_dry_run_is_not_gated(tmp_path, monkeypatch, capsys):
    """--dry-run creates nothing, so it stays a read."""
    calls, _stop = _gated_calls(monkeypatch)
    spec = tmp_path / "m.yaml"
    spec.write_text("x")
    monkeypatch.setattr(plan.config, "load", lambda: {})
    monkeypatch.setattr(plan.registry, "hive_dir_for", lambda *_a: tmp_path)
    monkeypatch.setattr(plan.molecule, "load_spec", lambda _s: {"epic": {}, "issues": []})
    monkeypatch.setattr(plan.molecule, "validate_or_raise", lambda *_a, **_k: None)
    monkeypatch.setattr(plan, "_preview", lambda *_a, **_k: None)
    plan.file(spec=str(spec), dry_run=True, save="", hive="h")
    assert calls == []


# Every ungated read verb -> the callable that implements it. `list` is `list_` (the builtin
# would be shadowed) and `sync` is the top-level `bh sync`, not a `bh work` subcommand — both
# spelled out so no read verb can silently drop out of this check as a skip.
_READ_IMPLS = {
    "ready": work.ready,
    "list": work.list_,
    "show": work.show,
    "brief": work.brief,
    "issue": work.issue,
    "review": work.review,
    "sync": cli.sync_cmd,
}


def test_every_ungated_read_verb_has_an_implementation_under_test():
    assert set(_READ_IMPLS) == set(guard.UNGATED_READ_VERBS)


@pytest.mark.parametrize("verb", sorted(_READ_IMPLS))
def test_read_verbs_are_never_gated(verb, hq, hive, this_host, monkeypatch):
    """A foreign lease blocks nothing a read verb does — the guard is not on their path at
    all. Asserted structurally: no read verb's source names guard_primary."""
    import inspect

    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    assert "guard_primary" not in inspect.getsource(_READ_IMPLS[verb])


def test_reads_still_work_under_a_foreign_lease(hq, hive, this_host, monkeypatch):
    """End-to-end statement of "looking is always safe": the same state that refuses a write
    leaves a read untouched."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    # a read path consults the lease not at all
    assert host_lease.read_cached(PREFIX, cwd=config.hq_dir()).host_id == OTHER_HOST


# ---- bh-r8el: distinguishable from the pre-existing close failure --------------------


def test_the_lease_refusal_and_bd_close_failure_share_no_marker(
    hq, hive, this_host, monkeypatch, capsys
):
    """bh-r8el (fixed via `work_logic.close_merged` — merge now retries the close as the
    bead's own assignee, so `bd close`'s "cannot close: assignee is dev/X, actor is <human>"
    refusal is no longer the common case): the lease refusal must stay structurally
    distinguishable from that close-failure text regardless, since a genuinely un-closable bead
    (no assignee, or a pinned issue) can still surface it after the `--force` fallback."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    refusal = capsys.readouterr().err

    # structurally different text, in both directions
    assert guard.PRIMARY_REFUSAL_MARKER in refusal
    assert guard.PRIMARY_REFUSAL_MARKER not in BD_CLOSE_FAILURE
    assert "cannot close" not in refusal
    assert "assignee is" not in refusal
    assert "actor is" not in refusal
    # and the remedies they point at are different
    assert "Adopt this hive" in refusal
    assert "--force" not in refusal  # the lease refusal never suggests bd's close escape hatch


def test_the_two_failures_are_different_exception_types(hq, hive, this_host, monkeypatch):
    """The close failure (bh-r8el/bh-3nuo) is now a CONDITIONAL typer.Exit raised AFTER the
    merge already landed — `work_logic.close_merged` retries the close as the bead's own
    assignee, then as `--force`, and only when BOTH still fail does `_merge_bead` raise; the
    lease refusal is a typer.Exit raised BEFORE the merge ever starts. Disjoint timing — one
    fires before anything is touched, the other only after a merge nothing can undo — so they
    cannot be caught, reported, or retried by the same handler."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))
    with pytest.raises(typer.Exit):
        guard.guard_primary("", cfg={})
    import inspect

    merge_src = inspect.getsource(work._merge_bead)
    close_src = inspect.getsource(work_logic.close_merged)
    assert 'bd.run(["close", bead' in close_src  # the close call itself lives in close_merged
    assert "work_logic.close_merged" in merge_src  # _merge_bead delegates, doesn't inline it
    assert "guard_primary" not in merge_src  # the gate is up front, not around the close
    assert "guard_primary" not in close_src


def test_guard_primary_never_fires_on_the_bd_close_force_retry(
    hq, hive, this_host, monkeypatch
):
    """The operator's post-merge `bd close --force` is bookkeeping on a merge that already
    succeeded. It must never be blocked by the lease gate — so the bd passthrough guard does
    not consult guard_primary, and a foreign lease leaves it untouched."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)
    _record_lease(hq, _lease(OTHER_HOST))

    fired: list[str] = []
    real = guard.guard_primary

    def spy(*a, **kw):
        fired.append("primary")
        return real(*a, **kw)

    monkeypatch.setattr(guard, "guard_primary", spy)
    guard.guard_bd(["close", "tt-1", "--force"], actor="brian")  # no raise
    guard.guard_bd(["close", "tt-1", "--force", "--reason", "merged"], actor="dev/lease")
    assert fired == []


def test_the_gate_sits_before_the_merge_not_around_its_close(monkeypatch):
    """Placement, asserted on the source: one gate up front. If it ever moved to wrap the
    close, a merge that already landed would become un-cleanable."""
    import inspect

    src = inspect.getsource(work.merge)
    gate_at = src.index("guard.guard_primary")
    slot_at = src.index("work_group.merge_group")
    assert gate_at < slot_at
