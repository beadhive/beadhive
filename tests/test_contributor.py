"""Tests for the **contributor** seat (bh-uxam.3) — the two hive-scoped duties:

* **DOSSIER** — the four-layer contribution profile → explicit go/no-go + authorship strategy.
  A "no AI PRs" upstream yields NO-GO (an advisory, not a silent proceed); build/store/load keyed
  by the hive triplet, refreshed when stale.
* **OUTBOUND EDITOR** — queue (`outbound:pending`) → dedupe (`bd find-duplicates`) → gated push.
  A non-contributor seat is refused; a bare/multi-item ("dirty") push is refused; an ungated push
  is refused; a gated single-item push flips `outbound:pending` → `publish:approved` and stamps
  `external_ref`.

The bd layer is faked (the `_Recorder` pattern from test_report.py) so the queue→dedupe→gated-push
wiring is asserted without a real beads DB or network.
"""

from __future__ import annotations

from collections import namedtuple

import pytest

from beadhive import contributor, guard
from beadhive.state import OUTBOUND_PENDING, PUBLISH_APPROVED

Completed = namedtuple("Completed", "returncode stdout stderr")

_ENTRY = {
    "provider": "github",
    "org": "acme",
    "repo": "widget",
    "prefix": "fork-widget",
    "kind": "external",
    "upstream": "acme/widget",
    "contribution": "pull",
}


# ---------------------------------------------------------------------------
# Layer 4 — AI-PR posture → verdict + authorship strategy
# ---------------------------------------------------------------------------


def test_no_ai_prs_yields_no_go():
    text = "Please note: we do not accept AI-generated pull requests in this project."
    posture = contributor.detect_ai_posture(text)
    assert posture == contributor.POSTURE_FORBIDDEN
    assert contributor.verdict_for(posture) == contributor.VERDICT_NO_GO
    # NO-GO is advisory — the strategy is explicit, not a silent proceed and not an auto-block.
    assert "NO-GO" in contributor.authorship_strategy_for(posture)


def test_restricted_posture_is_go_with_disclosure():
    posture = contributor.detect_ai_posture("AI-assisted contributions must disclose the tooling.")
    assert posture == contributor.POSTURE_RESTRICTED
    assert contributor.verdict_for(posture) == contributor.VERDICT_GO
    assert "disclos" in contributor.authorship_strategy_for(posture).lower()


def test_unknown_posture_defaults_to_advisory_go():
    posture = contributor.detect_ai_posture("Run the tests and open a PR.")
    assert posture == contributor.POSTURE_UNKNOWN
    assert contributor.verdict_for(posture) == contributor.VERDICT_GO


def test_forbidden_wins_over_other_signals():
    # A doc that both invites disclosure AND bans AI must fail safe toward NO-GO.
    text = "AI-assisted work should disclose tooling. However, no AI-generated contributions."
    assert contributor.detect_ai_posture(text) == contributor.POSTURE_FORBIDDEN


# ---------------------------------------------------------------------------
# Layer 1 — explicit requirements (mechanical scan)
# ---------------------------------------------------------------------------


def test_scan_requirements_detects_artifacts():
    files = {
        "CONTRIBUTING.md": "All commits need a Signed-off-by line (DCO).",
        ".github/PULL_REQUEST_TEMPLATE.md": "## Summary",
        ".github/ISSUE_TEMPLATE": "",
        "CODE_OF_CONDUCT.md": "Be excellent.",
        ".pre-commit-config.yaml": "repos: []",
        ".github/workflows": "",
    }
    req = contributor.scan_requirements(files)
    assert req["contributing"] == "CONTRIBUTING.md"
    assert req["pr_template"] == ".github/PULL_REQUEST_TEMPLATE.md"
    assert req["issue_template"] == ".github/ISSUE_TEMPLATE"
    assert req["code_of_conduct"] == "CODE_OF_CONDUCT.md"
    assert req["dco_sign_off"] is True
    assert req["style_lint_format"] is True
    assert req["test_and_ci"] is True


def test_scan_requirements_empty_when_nothing_present():
    req = contributor.scan_requirements({})
    assert req["contributing"] == ""
    assert req["dco_sign_off"] is False
    assert req["style_lint_format"] is False
    assert req["test_and_ci"] is False


def test_read_upstream_files_reads_fixture_dir(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("no AI-generated PRs please")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "workflows").mkdir()
    files = contributor.read_upstream_files(tmp_path)
    assert "CONTRIBUTING.md" in files
    assert ".github/workflows" in files  # directory marker
    assert "AI-generated" in files["CONTRIBUTING.md"]


# ---------------------------------------------------------------------------
# Dossier build + storage (keyed by triplet, refreshed when stale)
# ---------------------------------------------------------------------------


def _cfg():
    return {"managed_repos": [dict(_ENTRY)], "git_workspace": {"hive_match": "flexible"}}


def test_build_dossier_from_injected_reader():
    def fake_reader(_root):
        return {"CONTRIBUTING.md": "We do not accept AI PRs.", ".github/workflows": ""}

    dossier = contributor.build_dossier("fork-widget", cfg=_cfg(), reader=fake_reader)
    assert dossier.hive == "github/acme/widget"
    assert dossier.upstream == "acme/widget"
    assert dossier.verdict == contributor.VERDICT_NO_GO
    assert dossier.posture == contributor.POSTURE_FORBIDDEN
    assert dossier.requirements["test_and_ci"] is True
    # Layers 2-3 are seat-enriched — the mechanical build seeds them empty.
    assert dossier.conventions == []
    assert dossier.pushback == []


def test_store_and_load_dossier_roundtrip(tmp_path, monkeypatch):
    from beadhive import config

    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path)
    dossier = contributor.Dossier(
        hive="github/acme/widget",
        upstream="acme/widget",
        built_at=contributor._now(),
        requirements={"contributing": "CONTRIBUTING.md"},
        posture=contributor.POSTURE_WELCOME,
        verdict=contributor.VERDICT_GO,
    )
    contributor.store_dossier(dossier)
    loaded = contributor.load_dossier("github/acme/widget")
    assert loaded is not None
    assert loaded.upstream == "acme/widget"
    assert loaded.posture == contributor.POSTURE_WELCOME
    # A second hive's dossier does not clobber the first.
    other = contributor.Dossier(
        hive="github/acme/gadget", upstream="acme/gadget", built_at=contributor._now()
    )
    contributor.store_dossier(other)
    assert contributor.load_dossier("github/acme/widget") is not None
    assert contributor.load_dossier("github/acme/gadget") is not None


def test_load_dossier_absent_is_none(tmp_path, monkeypatch):
    from beadhive import config

    monkeypatch.setattr(config, "cache_dir", lambda: tmp_path)
    assert contributor.load_dossier("github/acme/nope") is None


def test_is_stale_by_ttl():
    fresh = contributor.Dossier(hive="h", upstream="u", built_at=contributor._now())
    assert contributor.is_stale(None) is True
    assert contributor.is_stale(fresh, ttl=3600) is False
    assert contributor.is_stale(fresh, ttl=0) is True  # always rebuild
    assert contributor.is_stale(fresh, ttl=-1) is False  # never expire
    old = contributor.Dossier(hive="h", upstream="u", built_at="2000-01-01T00:00:00Z")
    assert contributor.is_stale(old, ttl=3600) is True


# ---------------------------------------------------------------------------
# Outbound editor — fake bd (queue → dedupe → gated push)
# ---------------------------------------------------------------------------


class _FakeBd:
    """A recording fake for the bd invocations the outbound editor makes. Serves canned responses
    for `list` (the queue), `find-duplicates` (dedupe), `gate list` (publication gate), and `show`
    (the bead), and records every write (`github push`, `update`, `set-state`)."""

    def __init__(
        self, queue=None, dupes=None, gate_resolved=False, bead=None, gate_bead="fork-widget-1"
    ):
        self.queue = queue if queue is not None else []
        self.dupes = dupes if dupes is not None else []
        self.gate_resolved = gate_resolved
        self.bead = bead
        self.gate_bead = gate_bead
        self.calls: list[list[str]] = []

    def run(self, args, cwd, actor="", capture=False, text_input=None):
        self.calls.append(list(args))
        return Completed(0, "", "")

    def json(self, args, cwd):
        verb = args[0] if args else ""
        if verb == "list":
            return self.queue
        if verb == "find-duplicates":
            return {"pairs": self.dupes}
        if verb == "gate":
            status = "resolved" if self.gate_resolved else "open"
            desc = f"bh:publish {self.gate_bead} — human publication gate (external upstream)"
            return [{"id": "gate-1", "status": status, "description": desc}]
        return None

    def show(self, bead, cwd):
        return self.bead

    def did(self, *tokens) -> bool:
        """Whether a recorded call contains the contiguous token sequence."""
        for cmd in self.calls:
            for i in range(len(cmd)):
                if cmd[i : i + len(tokens)] == list(tokens):
                    return True
        return False


def _outbound_bead(bead_id="fork-widget-1", labels=None):
    return {
        "id": bead_id,
        "issue_type": "bug",
        "title": "upstream crash on empty input",
        "labels": labels if labels is not None else [OUTBOUND_PENDING],
    }


def _install_fake(monkeypatch, fake):
    monkeypatch.setattr(contributor.bd, "run", fake.run)
    monkeypatch.setattr(contributor.bd, "json", fake.json)
    monkeypatch.setattr(contributor.bd, "show", fake.show)


def test_outbound_queue_lists_pending_and_dedupes(monkeypatch):
    rows = [_outbound_bead("fork-widget-1"), _outbound_bead("fork-widget-2")]
    dupes = [{"issue_a_id": "fork-widget-1", "issue_b_id": "fork-widget-2", "similarity": 0.9}]
    fake = _FakeBd(queue=rows, dupes=dupes)
    _install_fake(monkeypatch, fake)
    # find_dupes runs through triage.bd.json too — point it at the same fake.
    from beadhive import triage

    monkeypatch.setattr(triage.bd, "json", fake.json)
    payload = contributor.outbound_queue("/tmp/hive")
    assert [r["id"] for r in payload["rows"]] == ["fork-widget-1", "fork-widget-2"]
    assert payload["dupes"] == dupes


def test_outbound_queue_excludes_already_published(monkeypatch):
    rows = [
        _outbound_bead("fork-widget-1"),
        _outbound_bead("fork-widget-2", labels=[OUTBOUND_PENDING, PUBLISH_APPROVED]),
    ]
    fake = _FakeBd(queue=rows)
    _install_fake(monkeypatch, fake)
    from beadhive import triage

    monkeypatch.setattr(triage.bd, "json", fake.json)
    payload = contributor.outbound_queue("/tmp/hive")
    assert [r["id"] for r in payload["rows"]] == ["fork-widget-1"]


def test_publish_refused_for_non_contributor(monkeypatch):
    fake = _FakeBd(bead=_outbound_bead(), gate_resolved=True)
    _install_fake(monkeypatch, fake)
    code, error, _msg = contributor.publish("/tmp/hive", "fork-widget-1", "dev/dev1")
    assert code == 1
    assert "contributor seat" in error
    # Refused BEFORE any push — no outward write happened.
    assert not fake.did("github", "push")


def test_publish_refused_when_ungated(monkeypatch):
    fake = _FakeBd(bead=_outbound_bead(), gate_resolved=False)
    _install_fake(monkeypatch, fake)
    code, error, _msg = contributor.publish("/tmp/hive", "fork-widget-1", "contrib/ann")
    assert code == 1
    assert "publication gate" in error
    assert not fake.did("github", "push")


def test_publish_refused_when_not_outbound_candidate(monkeypatch):
    # Already published (dirty) → refused, no push.
    fake = _FakeBd(
        bead=_outbound_bead(labels=[OUTBOUND_PENDING, PUBLISH_APPROVED]), gate_resolved=True
    )
    _install_fake(monkeypatch, fake)
    code, error, _msg = contributor.publish("/tmp/hive", "fork-widget-1", "contrib/ann")
    assert code == 1
    assert "not an outbound candidate" in error
    assert not fake.did("github", "push")


def test_publish_gated_single_item_flips_state_and_stamps_ref(monkeypatch):
    fake = _FakeBd(bead=_outbound_bead(), gate_resolved=True)
    _install_fake(monkeypatch, fake)
    code, error, message = contributor.publish(
        "/tmp/hive", "fork-widget-1", "contrib/ann", external_ref="gh-42"
    )
    assert code == 0, error
    # Filed via the gated single-item path (never a bare sync).
    assert fake.did("github", "push", "--issues", "fork-widget-1")
    assert not fake.did("github", "sync")
    # external_ref stamped for the resolution watch (bh-haak).
    assert fake.did("update", "fork-widget-1", "--external-ref", "gh-42")
    # Flipped outbound:pending → publish:approved (event-sourced set-state).
    assert fake.did("set-state", "fork-widget-1", "publish=approved")
    assert PUBLISH_APPROVED in message


def test_publish_not_found(monkeypatch):
    fake = _FakeBd(bead=None, gate_resolved=True)
    _install_fake(monkeypatch, fake)
    code, error, _msg = contributor.publish("/tmp/hive", "fork-widget-x", "contrib/ann")
    assert code == 1
    assert "not found" in error


# ---------------------------------------------------------------------------
# Publication gate (contributor-only to open)
# ---------------------------------------------------------------------------


def test_open_publish_gate_refused_for_non_contributor(monkeypatch):
    fake = _FakeBd()
    _install_fake(monkeypatch, fake)
    code, error = contributor.open_publish_gate("/tmp/hive", "fork-widget-1", "dev/dev1")
    assert code == 1
    assert "contributor seat" in error
    assert not fake.did("gate", "create")


def test_open_publish_gate_creates_human_gate(monkeypatch):
    fake = _FakeBd()
    # gate list returns no open publish gate → a fresh one is created.
    fake.json = lambda args, cwd: [] if args[:1] == ["gate"] else None
    monkeypatch.setattr(contributor.bd, "run", fake.run)
    monkeypatch.setattr(contributor.bd, "json", fake.json)
    code, error = contributor.open_publish_gate("/tmp/hive", "fork-widget-1", "contrib/ann")
    assert code == 0, error
    assert fake.did("gate", "create", "--blocks", "fork-widget-1", "--type", "human")


def test_open_publish_gate_idempotent_when_already_open(monkeypatch):
    fake = _FakeBd(gate_resolved=False)  # gate list returns an OPEN publish gate
    _install_fake(monkeypatch, fake)
    code, error = contributor.open_publish_gate("/tmp/hive", "fork-widget-1", "contrib/ann")
    assert code == 0, error
    assert not fake.did("gate", "create")  # reused, not recreated


# ---------------------------------------------------------------------------
# Generalized write-guard — the shared publish decision (DRY)
# ---------------------------------------------------------------------------


def test_guard_publish_refusal_predicate():
    push1 = ["github", "push", "--issues", "fork-widget-1"]
    # contributor + single item → allowed.
    assert guard.publish_refusal(push1, "contrib/ann") is None
    # non-contributor → refused.
    assert guard.publish_refusal(push1, "dev/dev1") is not None
    # bare sync → refused even for a contributor.
    assert guard.publish_refusal(["github", "sync"], "contrib/ann") is not None
    # multi-item push → refused (dirty).
    assert (
        guard.publish_refusal(
            ["github", "push", "--issues", "a", "--issues", "b"], "contrib/ann"
        )
        is not None
    )
    # a non-publish verb is never gated here.
    assert guard.publish_refusal(["list"], "dev/dev1") is None


def test_guard_bd_raises_on_refusal():
    import typer

    with pytest.raises(typer.Exit):
        guard.guard_bd(["github", "sync"], "contrib/ann")
    # allowed path does not raise.
    guard.guard_bd(["github", "push", "--issues", "one"], "contrib/ann")
