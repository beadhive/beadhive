"""GH#2455 dirty-config bypass (bh-areg.2) — ``onboard._bypass_gh2455_dirty_config`` (the one
named unit) and its wiring into ``onboard._act_bd_init``'s three paths.

Hermetic: ``hive.run`` is faked throughout, so no real ``bd``/dolt process ever runs.
``Ctx._derived`` is pre-set ``True`` so ``_ensure_derived`` is a no-op — these tests exercise
``_act_bd_init`` in isolation from registry/classify lookups (see ``test_onboard_dag.py`` for
the full-DAG, real-git-repo harness).

Empirical background (verified for this bead against a real ``bd`` binary + a real git-backed
origin, in an isolated scratch dir — see ``docs/design/gh2455-dirty-config-bypass-adr.md``):
a bare, non-clone ``bd init`` mints a fresh store and reproduces the bug; ``bd bootstrap``'s
clone-from-origin path (the second-host case) never does. The wiring tests below assert that
split directly: the furnished and zero-footprint branches probe for the bug, the bootstrap
branch never even calls ``bd sql``.
"""

from __future__ import annotations

import pytest

from beadhive import hive, onboard

_CLEAN_STATUS = "[]"
_DIRTY_STATUS = '[{"staged": 0, "status": "modified", "table_name": "config"}]'


class _Result:
    """Minimal ``subprocess.CompletedProcess``-shaped stand-in — matches what ``hive.run``
    actually returns, read via ``getattr`` in the production code (never attribute access
    directly), so this fake only needs to support the same shape."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ctx(tmp_path, *, furnish: bool) -> onboard.Ctx:
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(tmp_path),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(tmp_path),
        prefix="widget",
        furnish=furnish,
    )
    ctx._derived = True  # skip real registry/classify lookups — irrelevant to this bead
    return ctx


def _fake_run_factory(*, dolt_status_responses):
    """Fake ``hive.run``: a ``bd sql --json ... dolt_status`` call pops the next entry from
    *dolt_status_responses* (in call order); every other command succeeds trivially. Records
    every command received, in order."""
    calls: list[list[str]] = []
    responses = list(dolt_status_responses)

    def _fake_run(cmd, **kw):  # noqa: ARG001
        calls.append(list(cmd))
        if cmd[:3] == ["bd", "sql", "--json"]:
            return responses.pop(0)
        return _Result(returncode=0)

    return calls, _fake_run


# ---------------------------------------------------------------------------
# _dolt_status_has_dirty_config — the pure detector
# ---------------------------------------------------------------------------


def test_detects_dirty_config_row():
    assert onboard._dolt_status_has_dirty_config(_DIRTY_STATUS) is True


def test_clean_status_is_not_dirty():
    assert onboard._dolt_status_has_dirty_config(_CLEAN_STATUS) is False


def test_unrelated_dirty_table_is_not_the_config_bug():
    other = '[{"staged": 0, "status": "modified", "table_name": "issues"}]'
    assert onboard._dolt_status_has_dirty_config(other) is False


@pytest.mark.parametrize("raw", ["", "not json", "{}", "null", "[1, 2]"])
def test_malformed_or_unexpected_shape_never_raises_and_reads_clean(raw):
    assert onboard._dolt_status_has_dirty_config(raw) is False


# ---------------------------------------------------------------------------
# _bypass_gh2455_dirty_config — the one named unit, in isolation
# ---------------------------------------------------------------------------


def test_embedded_mode_probe_failure_is_silent_noop(tmp_path, monkeypatch, capsys):
    """`bd sql` failing (embedded mode: unsupported) means nothing to do — no output, no
    mutating calls, exactly one probe."""
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[_Result(returncode=1, stderr="not yet supported in embedded mode")]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    assert calls == [["bd", "sql", "--json", "SELECT * FROM dolt_status"]]
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_clean_status_is_silent_noop(tmp_path, monkeypatch, capsys):
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[_Result(returncode=0, stdout=_CLEAN_STATUS)]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    assert calls == [["bd", "sql", "--json", "SELECT * FROM dolt_status"]]
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_dirty_status_applies_the_documented_bypass_visibly(tmp_path, monkeypatch, capsys):
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[
            _Result(returncode=0, stdout=_DIRTY_STATUS),  # initial probe
            _Result(returncode=0, stdout=_CLEAN_STATUS),  # post-bypass verify
        ]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    assert calls == [
        ["bd", "sql", "--json", "SELECT * FROM dolt_status"],
        ["bd", "sql", "CALL DOLT_ADD('-A')"],
        ["bd", "sql", "CALL DOLT_COMMIT('-m', 'chore: clear bd dirty-config state (bh-areg.2)')"],
        ["bd", "sql", "--json", "SELECT * FROM dolt_status"],
    ]
    out = capsys.readouterr()
    # Detected + cleared: visible, never silent about the mutation itself.
    assert "GH#2455" in out.err
    assert "gastownhall/beads#4934" in out.err
    assert "cleared" in out.out
    assert "clean" in out.out


def test_bypass_never_claims_sanctioned_bd_behavior(tmp_path, monkeypatch, capsys):
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[
            _Result(returncode=0, stdout=_DIRTY_STATUS),
            _Result(returncode=0, stdout=_CLEAN_STATUS),
        ]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    combined = "".join(capsys.readouterr()).lower()
    assert "not bd-sanctioned" in combined


def test_never_points_at_gh2455_as_a_public_issue(tmp_path, monkeypatch, capsys):
    """The bug is bd-internal numbering — any mention must cite the real, open upstream
    reports rather than implying "GH#2455" is a public issue an operator could go read."""
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[
            _Result(returncode=0, stdout=_DIRTY_STATUS),
            _Result(returncode=0, stdout=_CLEAN_STATUS),
        ]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    out = capsys.readouterr().err
    assert "bd-internal" in out
    assert "gastownhall/beads#4934" in out
    assert "#5111" in out


def test_bypass_that_fails_to_clear_warns_distinctly(tmp_path, monkeypatch, capsys):
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[
            _Result(returncode=0, stdout=_DIRTY_STATUS),  # initial probe: dirty
            _Result(returncode=0, stdout=_DIRTY_STATUS),  # verify: STILL dirty
        ]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    out = capsys.readouterr()
    assert "✗" in out.err
    assert "did not clear" in out.err
    assert "✓" not in out.out  # never claims a success it didn't achieve


def test_verify_probe_erroring_is_treated_as_still_dirty(tmp_path, monkeypatch, capsys):
    """A verify probe that itself fails must fail CLOSED (report unresolved), never silently
    claim success."""
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[
            _Result(returncode=0, stdout=_DIRTY_STATUS),
            _Result(returncode=1, stderr="transient"),
        ]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._bypass_gh2455_dirty_config(_ctx(tmp_path, furnish=True))

    out = capsys.readouterr()
    assert "✗" in out.err
    assert "✓" not in out.out


# ---------------------------------------------------------------------------
# Wiring into _act_bd_init's three paths
# ---------------------------------------------------------------------------


def _patch_common(monkeypatch):
    """Stub the neighbors of ``_act_bd_init`` that are irrelevant to this bead, so each test
    below exercises exactly the bd-init branching + the bypass wiring. ``_ensure_server_mode_
    persisted``/``_enable_backup_if_remote`` are bh-areg.7's own server-mode wiring (own test
    file, ``test_onboard_server_mode.py``) — stubbed here for the same reason."""
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    monkeypatch.setattr(onboard, "_guard_beads_remote", lambda ctx: None)
    monkeypatch.setattr(onboard, "_ensure_server_mode_persisted", lambda ctx: None)
    monkeypatch.setattr(onboard, "_enable_backup_if_remote", lambda ctx: None)


def test_furnished_path_probes_after_bd_init(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[_Result(returncode=0, stdout=_CLEAN_STATUS)]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    # bd init, THEN the probe — the bypass only ever runs after bd init, never before.
    assert calls[0][:2] == ["bd", "init"]
    assert calls[1] == ["bd", "sql", "--json", "SELECT * FROM dolt_status"]


def test_zero_footprint_path_probes_after_bd_init(tmp_path, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: False)
    monkeypatch.setattr(hive, "_relocate_bd_gitignore", lambda base: False)
    calls, fake_run = _fake_run_factory(
        dolt_status_responses=[_Result(returncode=0, stdout=_CLEAN_STATUS)]
    )
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    assert calls[0][:2] == ["bd", "init"]
    assert ["bd", "sql", "--json", "SELECT * FROM dolt_status"] in calls


def test_bootstrap_path_never_probes_dolt_status(tmp_path, monkeypatch):
    """The second-host `bd bootstrap` case (bh-u562.1 Finding 7, re-verified for this bead
    against a real git-backed origin) never needs the bypass — `bd sql` must never even be
    called on this path."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    calls, fake_run = _fake_run_factory(dolt_status_responses=[])
    monkeypatch.setattr(hive, "run", fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    assert calls == [["bd", "bootstrap", "--non-interactive"]]
    assert not any(c[:2] == ["bd", "sql"] for c in calls)


# ---------------------------------------------------------------------------
# Embedded-mode regression — onboarding stays byte-for-byte unaffected
# ---------------------------------------------------------------------------


def _embedded_fake_run(cmd, **kw):  # noqa: ARG001
    if cmd[:3] == ["bd", "sql", "--json"]:
        return _Result(returncode=1, stderr="Error: 'bd sql' is not yet supported in embedded mode")
    return _Result(returncode=0)


def test_furnished_embedded_onboarding_output_is_byte_for_byte_unaffected(
    tmp_path, monkeypatch, capsys
):
    """Embedded mode's `bd sql` probe fails fast and silently: the printed output for a bare
    `bd init` onboard is IDENTICAL to what it was before this bead — nothing from the bypass
    unit leaks into stdout/stderr. This is the regression the bead's acceptance bar names
    explicitly."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(hive, "run", _embedded_fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=True))

    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_zero_footprint_embedded_onboarding_output_is_byte_for_byte_unaffected(
    tmp_path, monkeypatch, capsys
):
    _patch_common(monkeypatch)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: False)
    monkeypatch.setattr(hive, "_relocate_bd_gitignore", lambda base: False)
    monkeypatch.setattr(hive, "run", _embedded_fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def test_bootstrap_embedded_onboarding_output_is_byte_for_byte_unaffected(
    tmp_path, monkeypatch, capsys
):
    """The bootstrap path never wires the bypass at all, so it was already unaffected — this
    pins that down explicitly alongside its two siblings above."""
    _patch_common(monkeypatch)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    monkeypatch.setattr(hive, "run", _embedded_fake_run)

    onboard._act_bd_init(_ctx(tmp_path, furnish=False))

    out = capsys.readouterr()
    assert out.out == "• beads: bootstrapping from origin refs/dolt/data (zero-footprint)\n"
    assert out.err == ""
