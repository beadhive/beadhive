"""Tests for the ws-layer write-guard (ws.guard).

Two footguns bd will not protect against, one guard:
  1. `bh hub bd create` strands a bead in the derived aggregate — allowlist reads (plus the
     hydration verb that builds it) and refuse the rest, naming the owning hive. No
     exceptions since bh-89wxf.2: HQ has its own store and its own surface.
  2. bare `bd github sync`/`push` would push local beads to a PUBLIC tracker — deny for every
     seat except a contributor, and even then only the gated single-item push.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import guard

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
    assert "ISSUES NO IDS" in err
    assert "bh report" in err
    assert "bh escalate" in err
    assert "bh --hive <hive> bd create" in err


# ---- hq-native allowlist: hq-prefixed writes pass, product-hive writes refused ----


def test_guard_hub_hq_native_write_is_refused_here_too(capsys):
    """(a) bh-89wxf.2: an hq-prefixed write no longer gets a carve-out. It has its own store
    and its own surface (`bh hq bd …`, `hq.query`) — writing it into the derived aggregate
    would strand it exactly like any other."""
    with pytest.raises(typer.Exit):
        guard.guard_hub(["update", "hq-123", "--status", "done"])
    assert "ISSUES NO IDS" in capsys.readouterr().err


def test_guard_hub_product_hive_write_refused(capsys):
    """(b) A product-hive bead written directly into the aggregate is refused with a pointer."""
    with pytest.raises(typer.Exit) as exc:
        guard.guard_hub(["update", "", "--status", "done"])
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "ISSUES NO IDS" in err
    assert "bh report" in err
    assert "bh --hive <hive> bd create" in err


def test_guard_hub_escalate_nudge_appears(capsys):
    """(c) The escalate path surfaces in the guard nudge when a write is refused."""
    with pytest.raises(typer.Exit):
        guard.guard_hub(["update", "", "--status", "done"])
    assert "bh escalate" in capsys.readouterr().err


# ---- `bd dolt` against the hub: all of it refused now (bh-89wxf.2) ----
# The old allowance for `push`/`status`/`remote list` (bh-ohx2) existed because the aggregate
# WAS the HQ store, which has a real remote. The hub has none and is never published, and
# `bh hq bd dolt push` reaches HQ directly now — so there is nothing left to carve out.


@pytest.mark.parametrize(
    "args",
    [
        ["dolt", "push"],
        ["dolt", "status"],
        ["dolt", "remote", "list"],
        ["dolt", "remote", "add", "origin", "url"],
        ["dolt", "pull"],
        ["dolt"],
    ],
)
def test_guard_hub_every_dolt_verb_is_refused(args, capsys):
    with pytest.raises(typer.Exit) as exc:
        guard.guard_hub(args)
    assert exc.value.exit_code == 1
    assert "ISSUES NO IDS" in capsys.readouterr().err


# ---- the refusal names the hive the caller should have written to (bh-89wxf.1) ----


def test_guard_hub_refusal_names_the_owning_hive(monkeypatch, capsys):
    """A refused write carrying a registered hive's bead id gets told WHERE to write it."""
    monkeypatch.setattr(
        guard.config,
        "load",
        lambda: {"managed_repos": [{"prefix": "bh"}, {"prefix": "bh-app"}]},
    )
    with pytest.raises(typer.Exit):
        guard.guard_hub(["update", "bh-app-7", "--status", "done"])
    err = capsys.readouterr().err
    # LONGEST prefix wins: bh-app-7 is bh-app's, not bh's.
    assert "'bh-app' hive" in err
    assert "bh --hive bh-app bd update bh-app-7" in err


def test_guard_hub_refusal_falls_back_when_no_hive_is_identifiable(monkeypatch, capsys):
    """An id belonging to no registered hive (or no id at all) still gets the generic
    report/escalate nudge rather than a confidently wrong hive name."""
    monkeypatch.setattr(guard.config, "load", lambda: {"managed_repos": [{"prefix": "bh"}]})
    with pytest.raises(typer.Exit):
        guard.guard_hub(["update", "nope-7", "--status", "done"])
    err = capsys.readouterr().err
    assert "bh report" in err and "bh --hive <hive> bd create" in err


def test_guard_hub_hydration_verb_passes():
    """`bd repo add/sync` BUILDS the aggregate — it is how the hub exists, not a write into
    it, and every row it lands carries its source hive's prefix."""
    guard.guard_hub(["repo", "add", "/some/hive"])  # no raise
    guard.guard_hub(["repo", "sync"])  # no raise


# ---- refusal message names the actual command surface invoked (hq vs hub, bh-ohx2) ----


def test_guard_hub_refusal_names_hub_by_default(capsys):
    """The default `label` ("hub") — matching `bh hub bd …`, the surface this guard serves
    since bh-89wxf.2 — is what the refusal names when the caller doesn't specify otherwise."""
    with pytest.raises(typer.Exit):
        guard.guard_hub(["create", "-t", "boom"])
    assert "`bh hub bd create`" in capsys.readouterr().err


def test_guard_hub_refusal_names_the_invoked_surface(capsys):
    """A caller that names its own `label` gets a refusal naming THAT command — not a
    hardcoded guess unrelated to what was actually typed."""
    with pytest.raises(typer.Exit):
        guard.guard_hub(["create", "-t", "boom"], label="somewhere")
    assert "`bh somewhere bd create`" in capsys.readouterr().err


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
    assert "bh escalate" in err


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


# ---- warden-only security:* gate resolution (Assurance, bead .33) ------------


def test_is_warden():
    assert guard.is_warden("warden/sec")
    assert not guard.is_warden("dev/dev")
    assert not guard.is_warden("disp/lead")
    assert not guard.is_warden("brian")


@pytest.mark.parametrize(
    "gate,expected",
    [
        ({"reason": "security:secret-scan"}, True),
        ({"description": "blocks bc-1\n\nReason: security:sbom"}, True),
        ({"reason": "review abc123"}, False),
        ({"description": "blocks bc-1\n\nReason: kickoff bc-1"}, False),
        ("not-a-dict", False),
        ({}, False),
    ],
)
def test_is_security_gate(gate, expected):
    assert guard.is_security_gate(gate) is expected


def test_guard_security_gate_resolution_refuses_non_warden(capsys):
    """A non-warden resolving a security:* gate is refused with a warden-only pointer."""
    gate = {"id": "sec0", "reason": "security:secret-scan"}
    for actor in ("dev/dev", "disp/lead", "rev/r", "brian", ""):
        with pytest.raises(typer.Exit) as exc:
            guard.guard_security_gate_resolution(gate, actor)
        assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "warden-only" in err
    assert "warden/<name>" in err


def test_guard_security_gate_resolution_allows_warden_and_noops_non_security():
    """A warden may resolve a security gate; and a non-security gate is a no-op for any actor."""
    guard.guard_security_gate_resolution({"id": "sec0", "reason": "security:sbom"}, "warden/sec")
    guard.guard_security_gate_resolution({"id": "g0", "reason": "review abc"}, "dev/dev")


# ---- releaser-only release-hold: gate resolution (Release, bh-k2j8) ----------


def test_is_releaser():
    assert guard.is_releaser("releaser/rel")
    assert not guard.is_releaser("dev/dev")
    assert not guard.is_releaser("warden/sec")
    assert not guard.is_releaser("brian")


@pytest.mark.parametrize(
    "gate,expected",
    [
        ({"reason": "release-hold: bc-epic — release:breaking held"}, True),
        ({"description": "blocks bc-1\n\nReason: release-hold: bc-epic"}, True),
        ({"reason": "review abc123"}, False),
        ({"reason": "security:sbom"}, False),
        ({"description": "blocks bc-1\n\nReason: kickoff bc-1"}, False),
        ("not-a-dict", False),
        ({}, False),
    ],
)
def test_is_release_hold_gate(gate, expected):
    assert guard.is_release_hold_gate(gate) is expected


def test_guard_release_hold_gate_resolution_refuses_non_releaser(capsys):
    """A non-releaser resolving a release-hold: gate is refused with a releaser-only pointer."""
    gate = {"id": "rh0", "reason": "release-hold: bc-epic — release:breaking held"}
    for actor in ("dev/dev", "disp/lead", "warden/sec", "brian", ""):
        with pytest.raises(typer.Exit) as exc:
            guard.guard_release_hold_gate_resolution(gate, actor)
        assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "releaser-only" in err
    assert "releaser/<name>" in err


def test_guard_release_hold_gate_resolution_allows_releaser_and_noops_non_hold():
    """A releaser may resolve a release-hold gate; a non-hold gate is a no-op for any actor."""
    guard.guard_release_hold_gate_resolution(
        {"id": "rh0", "reason": "release-hold: bc-epic"}, "releaser/rel"
    )
    guard.guard_release_hold_gate_resolution({"id": "g0", "reason": "review abc"}, "dev/dev")


# ---- control-plane HQ-registry write partitioning (§2.1, bead .36) -----------


def test_is_controller():
    assert guard.is_controller("ctrl/gauge")
    assert not guard.is_controller("dir/ops")
    assert not guard.is_controller("super/root")
    assert not guard.is_controller("dev/dev")


@pytest.mark.parametrize(
    "section,partition",
    [
        ("managed_repos", guard.HQ_FLEET),
        ("orgs", guard.HQ_POLICY),
        ("providers", guard.HQ_POLICY),
        ("work", guard.HQ_HIVE_CONFIG),
        ("otel", guard.HQ_HIVE_CONFIG),
        ("totally-unknown", guard.HQ_HIVE_CONFIG),
    ],
)
def test_hq_partition_of_section(section, partition):
    assert guard.hq_partition_of_section(section) == partition


@pytest.mark.parametrize("partition", [guard.HQ_POLICY, guard.HQ_FLEET, guard.HQ_HIVE_CONFIG])
def test_guard_hq_registry_write_controller_denied_everywhere(partition, capsys):
    """Controller is READ-ONLY over every HQ partition (hard deny)."""
    with pytest.raises(typer.Exit) as exc:
        guard.guard_hq_registry_write(partition, "ctrl/gauge")
    assert exc.value.exit_code == 1
    assert "READ-ONLY" in capsys.readouterr().err


def test_guard_hq_registry_write_owner_and_supervisor_allowed():
    """The owning control seat may write its partition; the supervisor may write every partition."""
    guard.guard_hq_registry_write(guard.HQ_FLEET, "dir/ops")  # director owns fleet
    guard.guard_hq_registry_write(guard.HQ_HIVE_CONFIG, "cust/care")  # custodian owns hive config
    for p in (guard.HQ_POLICY, guard.HQ_FLEET, guard.HQ_HIVE_CONFIG):
        guard.guard_hq_registry_write(p, "super/root")  # org-root writes everything


def test_guard_hq_registry_write_non_control_exempt():
    """A non-control identity (developer/dispatcher/human) is not bound by the partitioning."""
    guard.guard_hq_registry_write(guard.HQ_POLICY, "dev/dev")
    guard.guard_hq_registry_write(guard.HQ_FLEET, "disp/lead")
    guard.guard_hq_registry_write(guard.HQ_HIVE_CONFIG, "brian")


def test_guard_hq_registry_write_mismatched_control_seat_warns_not_denied(monkeypatch):
    """A control seat writing OUTSIDE its partition is warned (soft) but allowed — not denied."""
    warnings: list[tuple] = []

    class _Logger:
        def warning(self, event, **kw):
            warnings.append((event, kw))

    monkeypatch.setattr("beadhive.log.get_logger", lambda *_a, **_k: _Logger())
    guard.guard_hq_registry_write(guard.HQ_POLICY, "dir/ops")  # director writing policy: no raise
    assert [e for e, _ in warnings] == ["hq_registry_partition_violation"]
    assert warnings[0][1]["partition"] == guard.HQ_POLICY
