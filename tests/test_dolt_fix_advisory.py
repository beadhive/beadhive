"""`setup.dolt_fix_advisory` — the bd version floor bh never had (bh-gnqc).

dolt is statically compiled into bd, so the dolt version is frozen at bd build time and is NOT
readable from `bd version`. On a build embedding dolt < 2.2.0, `bd dolt pull` can hang
indefinitely on a large store (upstream beads#4770) — and bh's multi-host sync runs exactly that
pull. Before this, nothing in bh stated a bd requirement: no floor, no check, no note, so an
operator met the bug as an unexplained hang on 0.7.0's headline feature.

The two properties that matter, and why:

  * it fires on a tagged release <= the last one known to pin old dolt (verified by decoding
    go.mod at v1.1.0/v1.1.1/v1.1.2 — all three pin the same 2026-06-05 dolt commit);
  * it stays SILENT on anything it cannot judge. A HEAD build is how an operator picks the fix
    up ahead of a release — this hive's own Brewfile pins exactly that — so warning on
    unparseable input would nag the very people who already worked around the problem.
"""

from __future__ import annotations

import pytest

from beadhive.setup import BD_LAST_RELEASE_WITHOUT_DOLT_FIX, dolt_fix_advisory

AFFECTED = "bd version 1.1.2 (20e493e56: main@20e493e569c9)"
HEAD_BUILD = "bd version HEAD-af076b6 (Homebrew: HEAD@af076b628984)"


@pytest.mark.parametrize(
    "version",
    [
        "bd version 1.1.2 (20e493e56: main@20e493e569c9)",
        "bd version 1.1.1",
        "bd version 1.1.0 (abc123)",
        "bd version 1.0.5",
        "bd version 0.63.3",
    ],
)
def test_warns_on_a_tagged_release_at_or_below_the_floor(version):
    advisory = dolt_fix_advisory(version)

    assert advisory is not None
    assert "beads#4770" in advisory


@pytest.mark.parametrize(
    "version",
    [
        HEAD_BUILD,  # the documented escape — must never be nagged
        "bd version 1.1.3",  # a future release, presumed fixed until proven otherwise
        "bd version 2.0.0",
        "some unparseable string",
        None,  # probe failed to read a version at all
        "",
    ],
)
def test_silent_when_fixed_or_unjudgeable(version):
    assert dolt_fix_advisory(version) is None


def test_the_floor_is_exclusive_at_the_next_patch():
    """Boundary: the floor is the LAST affected release, so floor+patch must be clean. Encoded
    against the constant rather than a literal, so raising the floor moves this test with it."""
    major, minor, patch = BD_LAST_RELEASE_WITHOUT_DOLT_FIX

    assert dolt_fix_advisory(f"bd version {major}.{minor}.{patch}") is not None
    assert dolt_fix_advisory(f"bd version {major}.{minor}.{patch + 1}") is None


def test_the_advisory_names_both_escapes_and_the_dead_end():
    """The message has to carry the whole diagnosis, because it is the only place a user meets
    this. Upgrading the standalone dolt CLI is the obvious thing to try and it does NOT work
    (dolt is compiled in — verified in bh-p24m by upgrading 2.1.10 -> 2.2.2 and retesting), so
    saying that explicitly saves the reader the same dead end."""
    advisory = dolt_fix_advisory(AFFECTED)

    assert "--HEAD" in advisory  # escape 1: a HEAD build
    assert "--server" in advisory  # escape 2: external dolt sql-server
    assert "does NOT help" in advisory  # the dead end, called out
    assert "multi-host sync" in advisory  # why a bh user should care


def test_doctor_warns_too_because_setup_check_is_cached(monkeypatch):
    """`setup check` is a once-then-cached gate, so an operator who passed it BEFORE upgrading
    bd never sees its warning again. The bug presents as bead sync hanging, and `doctor` is what
    people run when something is stuck — so it has to be visible from there independently."""
    from beadhive import doctor

    monkeypatch.setattr(
        "beadhive.setup.probe_one",
        lambda *a, **k: {"found": True, "version": AFFECTED},
    )

    warns = doctor._bd_dolt_fix_warnings()

    assert len(warns) == 1
    assert "beads#4770" in warns[0]
    assert "\n" not in warns[0]  # one line: the section is a flat list


def test_doctor_silent_on_a_fixed_build(monkeypatch):
    from beadhive import doctor

    monkeypatch.setattr(
        "beadhive.setup.probe_one",
        lambda *a, **k: {"found": True, "version": HEAD_BUILD},
    )

    assert doctor._bd_dolt_fix_warnings() == []


def test_setup_check_surfaces_the_advisory(monkeypatch, capsys):
    """End-to-end through the command an operator actually runs. `found` stays true — an
    affected bd is present and works — so setup still PASSES; this is advisory, not a gate."""
    from beadhive import setup as setup_mod

    monkeypatch.setattr(
        setup_mod,
        "probe_tools",
        lambda: {"bd": {"found": True, "version": AFFECTED}},
    )
    monkeypatch.setattr(setup_mod, "_write_cache", lambda *a, **k: None)

    setup_mod.run_check()

    out = capsys.readouterr()
    assert "beads#4770" in (out.out + out.err)
    assert "setup complete" in out.out  # advisory did not turn into a failure
