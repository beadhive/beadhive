"""Tests for the ws-layer write-guard (ws.guard).

Two footguns bd will not protect against, one guard:
  1. `ws hub bd create` strands a bead in the read cache — allowlist reads on the hub.
     Exception: hq-prefixed (control-plane) bead writes are allowed into the HQ store.
  2. bare `bd github sync`/`push` would push local beads to a PUBLIC tracker — deny for every
     seat except a contributor, and even then only the gated single-item push.
"""

from __future__ import annotations

import pytest
import typer

from ws import guard

# ---- hub allowlist: reads pass, writes refused -------------------------------


@pytest.mark.parametrize("verb", sorted(guard.READ_VERBS))
def test_guard_hub_read_verbs_pass(verb):
    """Every read verb forwards to the hub cache untouched."""
    guard.guard_hub([verb, "--json"])  # no raise


def test_guard_hub_bare_invocation_passes():
    """A bare/help invocation (no verb) is not a write — let bd render its own help."""
    guard.guard_hub([])
    guard.guard_hub(["--help"])


@pytest.mark.parametrize("verb", ["create", "update", "close", "import", "dep"])
def test_guard_hub_mutating_verbs_refused(verb, capsys):
    """A mutating verb against the hub is refused with a pointer to the write paths."""
    with pytest.raises(typer.Exit) as exc:
        guard.guard_hub([verb, "-t", "boom"])
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "READ-ONLY" in err
    assert "ws report" in err
    assert "ws escalate" in err
    assert "ws -r <rig> bd create" in err


# ---- hq-native allowlist: hq-prefixed writes pass, product-rig writes refused ----


def test_guard_hub_hq_native_write_allowed():
    """(a) An hq-prefixed bead write (control-plane) is allowed into the HQ aggregate store."""
    guard.guard_hub(["update", "hq-123", "--status", "done"])  # no raise
    guard.guard_hub(["close", "hq-456"])  # no raise
    guard.guard_hub(["set-state", "hq-789", "priority=high"])  # no raise


def test_guard_hub_product_rig_write_refused(capsys):
    """(b) A product-rig bead written directly into the aggregate is refused with a pointer."""
    with pytest.raises(typer.Exit) as exc:
        guard.guard_hub(["update", "", "--status", "done"])
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "READ-ONLY" in err
    assert "ws report" in err
    assert "ws -r <rig> bd create" in err


def test_guard_hub_escalate_nudge_appears(capsys):
    """(c) The escalate path surfaces in the guard nudge when a write is refused."""
    with pytest.raises(typer.Exit):
        guard.guard_hub(["update", "", "--status", "done"])
    assert "ws escalate" in capsys.readouterr().err


# ---- github push/sync: seat-scoped + gated single-item -----------------------


def test_guard_bd_non_github_passes():
    """Non-publish verbs pass regardless of seat (create/import handled upstream)."""
    guard.guard_bd(["create", "-t", "x"], "crew/dev")
    guard.guard_bd(["ready"], "crew/dev")
    guard.guard_bd(["github", "pull"], "crew/dev")  # pull is not a publish verb


@pytest.mark.parametrize("actor", ["crew/dev", "coord/lead", "brian", ""])
@pytest.mark.parametrize("sub", ["push", "sync"])
def test_guard_bd_non_contributor_publish_refused(actor, sub, capsys):
    """github push/sync is denied for every non-contributor seat."""
    with pytest.raises(typer.Exit) as exc:
        guard.guard_bd(["github", sub, "--issues", "bc-1"], actor)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "contributor seat" in err
    assert "contrib/<name>" in err
    assert "ws escalate" in err


def test_guard_bd_contributor_bare_sync_refused(capsys):
    """Even a contributor may not run a bare sync — bd has no sync-eligibility filter."""
    with pytest.raises(typer.Exit) as exc:
        guard.guard_bd(["github", "sync"], "contrib/ann")
    assert exc.value.exit_code == 1
    assert "sync-eligibility filter" in capsys.readouterr().err


def test_guard_bd_contributor_sync_with_issues_still_refused(capsys):
    """`sync` is refused even with --issues — only `push` is the safe publish verb."""
    with pytest.raises(typer.Exit):
        guard.guard_bd(["github", "sync", "--issues", "bc-1"], "contrib/ann")
    assert "safe publish" in capsys.readouterr().err


def test_guard_bd_contributor_push_without_issues_refused(capsys):
    """A bare `push` (no explicit single id) is refused — no unfiltered broadcast."""
    with pytest.raises(typer.Exit):
        guard.guard_bd(["github", "push"], "contrib/ann")
    assert "one bead at a time" in capsys.readouterr().err


def test_guard_bd_contributor_push_multiple_issues_refused(capsys):
    """More than one id is refused — publication is one bead at a time."""
    with pytest.raises(typer.Exit):
        guard.guard_bd(["github", "push", "--issues", "bc-1,bc-2"], "contrib/ann")
    assert "one bead at a time" in capsys.readouterr().err


def test_guard_bd_contributor_gated_push_allowed():
    """The gated single-item push is the one allowed publish path for a contributor."""
    guard.guard_bd(["github", "push", "--issues", "bc-1"], "contrib/ann")  # no raise
    guard.guard_bd(["github", "push", "--issues=bc-1"], "contrib/ann")  # =form too
