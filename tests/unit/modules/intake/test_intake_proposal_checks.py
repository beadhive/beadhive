"""The deterministic pre-apply proposal checks (bh-xhww6). Pure: every fact is a fake.

Each check fails closed and names the fix: a closed or missing owner target, a fixing commit not
on the integration branch, a claimed fix version the repository disagrees with (the computed one
is reported, and adopting it overwrites the claim), a dangling, cyclic or chained ``share_with``,
an inconsistent disposition, a leaking or over-long reporter text, and a proposal gone stale by
its thread seq or by a change to a path its evidence cites.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake.contracts import (
    CheckContext,
    CheckKind,
    LeakPolicy,
    MentionFact,
    Proposal,
    ProposalDisposition,
    ProposalStatus,
    ProposalTarget,
    ReviewVerdict,
    TargetFact,
    TargetRole,
    add_review,
    adopt_computed_fix,
    check_proposal,
    check_run,
    cited_paths,
    mentions,
    new_proposal,
    version_key,
)
from beadhive.testing.intake import FakeRepoFacts, RecordingSanitizer

D = ProposalDisposition
BRANCH = "main"
ANALYSED = "a0a0a0a"
FIX, FIX2, STRAY = "f1f1f1f", "f2f2f2f", "deadbee"


def repo(**overrides) -> FakeRepoFacts:
    facts = {
        "branches": {BRANCH: frozenset({ANALYSED, FIX, FIX2})},
        "releases": (
            ("v0.9.0", frozenset({ANALYSED})),
            ("v0.10.0", frozenset({ANALYSED, FIX})),
            ("v0.11.0", frozenset({ANALYSED, FIX, FIX2})),
        ),
        "changes": {(ANALYSED, BRANCH): ("docs/other.md",)},
    }
    facts.update(overrides)
    return FakeRepoFacts(**facts)


def context(facts: FakeRepoFacts | None = None, **overrides) -> CheckContext:
    values = {
        "branch": BRANCH,
        "repo": facts if facts is not None else repo(),
        "leak_policy": LeakPolicy(
            bead_prefixes=frozenset({"bh"}), private_dimensions=frozenset({"intake"})
        ),
        "sanitizer": RecordingSanitizer.of(["hunter2-token"]),
        "targets": {
            "bh-open": TargetFact("bh-open", "in_progress"),
            "bh-done": TargetFact("bh-done", "closed", commits=(FIX,)),
            "bh-wontfix": TargetFact("bh-wontfix", "closed"),
            "bh-off": TargetFact("bh-off", "closed", commits=(STRAY,)),
        },
        "thread_seqs": {"r-1": 4},
    }
    values.update(overrides)
    return CheckContext(**values)


def tracked(**changes) -> Proposal:
    proposal = new_proposal(
        D.TRACKED,
        targets=(ProposalTarget("bh-open"),),
        type="bug",
        priority="2",
        reporter_text="Thanks, this is on our list.",
        evidence=("src/login.py:12",),
        analysed_commit=ANALYSED,
        thread_seq=4,
    )
    return dataclasses.replace(proposal, **changes)


def fixed(**changes) -> Proposal:
    base = {
        "disposition": D.FIXED,
        "targets": (ProposalTarget("bh-done"),),
        "type": "",
        "priority": "",
        "reporter_text": "This was fixed in a release.",
    }
    return tracked(**{**base, **changes})


def kinds(proposal: Proposal, ctx: CheckContext | None = None, report: str = "r-1"):
    return check_proposal(report, proposal, ctx or context()).kinds()


def test_a_sound_tracked_proposal_passes_every_check() -> None:
    check = check_proposal("r-1", tracked(), context())
    assert check.passed and not check.stale and check.computed_fixed_in is None


# -- targets -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "kind"),
    [("bh-ghost", CheckKind.OWNER_MISSING), ("bh-done", CheckKind.OWNER_CLOSED)],
)
def test_a_missing_or_closed_owner_target_fails(target, kind) -> None:
    check = check_proposal("r-1", tracked(targets=(ProposalTarget(target),)), context())
    assert check.kinds() == {kind}
    (finding,) = check.findings
    assert target in finding.detail and finding.fix


def test_a_missing_related_target_fails_but_a_closed_one_is_fine() -> None:
    targets = (ProposalTarget("bh-open"), ProposalTarget("bh-done", TargetRole.RELATED))
    assert kinds(tracked(targets=targets)) == set()
    targets = (ProposalTarget("bh-open"), ProposalTarget("bh-nope", TargetRole.RELATED))
    assert kinds(tracked(targets=targets)) == {CheckKind.TARGET_MISSING}


def test_a_fixed_claim_whose_fixing_bead_is_still_open_fails() -> None:
    assert CheckKind.FIX_OPEN in kinds(fixed(targets=(ProposalTarget("bh-open"),)))


# -- fix claims: ancestry and the computed version ---------------------------------------------


def test_a_fixed_claim_computes_its_version_from_the_closed_fixing_bead() -> None:
    facts = repo()
    check = check_proposal("r-1", fixed(), context(facts))
    assert check.passed and check.computed_fixed_in == "v0.10.0"
    assert ("is_ancestor", FIX, BRANCH) in facts.asked


def test_a_fixed_claim_whose_commit_is_not_on_the_integration_branch_fails() -> None:
    check = check_proposal("r-1", fixed(targets=(ProposalTarget("bh-off"),)), context())
    assert check.kinds() == {CheckKind.FIX_NOT_ON_BRANCH}
    assert STRAY in check.findings[0].detail and check.computed_fixed_in is None
    by_commit = fixed(targets=(), fix_commit=STRAY)
    assert kinds(by_commit) == {CheckKind.FIX_NOT_ON_BRANCH}


def test_a_commit_git_cannot_judge_fails_closed() -> None:
    facts = repo(unknown=frozenset({FIX}))
    check = check_proposal("r-1", fixed(), context(facts))
    assert check.kinds() == {CheckKind.FIX_NOT_ON_BRANCH}
    assert "cannot be verified" in check.findings[0].detail


def test_a_fixed_claim_with_no_commit_at_all_is_unproven() -> None:
    assert kinds(fixed(targets=(ProposalTarget("bh-wontfix"),))) == {CheckKind.FIX_UNPROVEN}
    assert kinds(fixed(targets=())) == {CheckKind.FIX_UNPROVEN}


def test_a_mismatched_fixed_in_fails_and_reports_the_computed_version() -> None:
    claim = fixed(fixed_in="v0.9.0")
    check = check_proposal("r-1", claim, context())
    assert check.kinds() == {CheckKind.FIX_VERSION}
    (finding,) = check.findings
    assert "v0.9.0" in finding.detail and "v0.10.0" in finding.detail
    assert "v0.10.0" in finding.fix
    assert check.computed_fixed_in == "v0.10.0"


def test_the_computed_version_overwrites_the_claim_and_unverifies_it() -> None:
    claim = add_review(fixed(fixed_in="v0.9.0"), "rev/one", ReviewVerdict.CONFIRMED)
    assert claim.status is ProposalStatus.VERIFIED
    adopted = adopt_computed_fix(claim, check_proposal("r-1", claim, context()))
    assert adopted.fixed_in == "v0.10.0" and adopted.status is ProposalStatus.PROPOSED
    assert check_proposal("r-1", adopted, context()).passed
    agreeing = fixed(fixed_in="v0.10.0")
    assert adopt_computed_fix(agreeing, check_proposal("r-1", agreeing, context())) is agreeing


def test_the_fix_version_is_the_release_holding_every_fixing_commit() -> None:
    both = fixed(fix_commit=FIX2)  # the bead's FIX is in v0.10.0, FIX2 only in v0.11.0
    assert check_proposal("r-1", both, context()).computed_fixed_in == "v0.11.0"


def test_a_merged_but_unreleased_fix_computes_no_version() -> None:
    facts = repo(releases=())
    check = check_proposal("r-1", fixed(), context(facts))
    assert check.passed and check.computed_fixed_in == ""
    check = check_proposal("r-1", fixed(fixed_in="v0.10.0"), context(facts))
    assert check.kinds() == {CheckKind.FIX_VERSION} and "unreleased" in check.findings[0].detail


def test_a_partial_fix_checks_its_cited_commit_and_tracks_open_residual_work() -> None:
    partial = tracked(disposition=D.PARTIALLY_FIXED, fix_commit=FIX, fixed_in="v0.10.0")
    check = check_proposal("r-1", partial, context())
    assert check.passed and check.computed_fixed_in == "v0.10.0"
    assert kinds(dataclasses.replace(partial, fix_commit=STRAY)) == {CheckKind.FIX_NOT_ON_BRANCH}
    assert kinds(dataclasses.replace(partial, fix_commit="")) == {CheckKind.FIX_UNPROVEN}


def test_version_key_sorts_as_git_version_refname() -> None:
    tags = ["v0.10.0", "v0.9.0", "v1.0.0", "v0.9.10"]
    assert sorted(tags, key=version_key) == ["v0.9.0", "v0.9.10", "v0.10.0", "v1.0.0"]


# -- families ----------------------------------------------------------------------------------


def dup(of: str, **changes) -> Proposal:
    base = {
        "disposition": D.DUPLICATE_OF,
        "targets": (),
        "type": "",
        "priority": "",
        "share_with": of,
    }
    return tracked(**{**base, **changes})


def test_a_family_resolving_to_one_owning_canonical_passes() -> None:
    run = {"r-1": tracked(), "r-2": dup("r-1"), "r-3": dup("r-1")}
    checks = check_run(run, context())
    assert [c.report for c in checks] == ["r-1", "r-2", "r-3"]
    assert all(c.passed for c in checks), [c.findings for c in checks]


def test_a_dangling_share_with_fails() -> None:
    run = {"r-2": dup("r-9")}
    (check,) = check_run(run, context())
    assert check.kinds() == {CheckKind.SHARE_DANGLING} and "r-9" in check.findings[0].detail


@pytest.mark.parametrize(
    "run",
    [
        {"r-2": dup("r-2")},
        {"r-2": dup("r-3"), "r-3": dup("r-2")},
        {"r-2": dup("r-3"), "r-3": dup("r-4"), "r-4": dup("r-2")},
    ],
    ids=["self", "pair", "triangle"],
)
def test_a_cyclic_share_with_fails(run) -> None:
    for check in check_run(run, context()):
        assert CheckKind.SHARE_CYCLE in check.kinds(), check


def test_a_share_with_through_a_second_canonical_fails_and_names_the_real_one() -> None:
    run = {"r-1": tracked(), "r-2": dup("r-1"), "r-3": dup("r-2")}
    checks = {c.report: c for c in check_run(run, context())}
    assert checks["r-2"].passed
    assert checks["r-3"].kinds() == {CheckKind.SHARE_CHAIN}
    assert "r-1" in checks["r-3"].findings[0].fix


def test_the_canonical_must_hold_the_owner_disposition() -> None:
    canonical = tracked(disposition=D.NEEDS_INFO, targets=(), reporter_text="Which version?")
    run = {"r-1": canonical, "r-2": dup("r-1")}
    checks = {c.report: c for c in check_run(run, context())}
    assert checks["r-1"].passed
    assert checks["r-2"].kinds() == {CheckKind.CANONICAL_DISPOSITION}


def test_a_run_asks_each_repository_question_once() -> None:
    facts = repo()
    run = {f"r-{n}": fixed() for n in range(1, 6)}
    assert all(c.passed for c in check_run(run, context(facts)))
    assert len(facts.asked) == len(set(facts.asked)) == 3  # ancestry, tag, changed paths


# -- field consistency -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("proposal", "says"),
    [
        (tracked(disposition=D.NEW, targets=(), fixed_in="v0.10.0"), "fixed_in"),
        (tracked(disposition=D.NEW, targets=(), fix_commit=FIX), "fixed_in"),
        (fixed(type="bug"), "type or priority"),
        (fixed(priority="1"), "type or priority"),
        (tracked(targets=()), "owner target"),
        (tracked(disposition=D.NEW), "owner target"),
        (tracked(share_with="r-2"), "share_with"),
        (dup(""), "share_with"),
        (dup("r-2", targets=(ProposalTarget("bh-open"),)), "canonical holds it"),
        (tracked(fixed_in="v0.10.0"), "fixed_in"),
        (tracked(disposition=D.REJECT, targets=(), reporter_text=""), "reporter_text"),
        (tracked(disposition=D.NEEDS_INFO, targets=(), reporter_text=" "), "reporter_text"),
    ],
    ids=[
        "new-fixed_in",
        "new-fix_commit",
        "fixed-type",
        "fixed-priority",
        "tracked-no-owner",
        "new-with-owner",
        "share-without-duplicate",
        "duplicate-without-share",
        "duplicate-with-owner",
        "tracked-fixed_in",
        "reject-no-text",
        "needs-info-no-text",
    ],
)
def test_an_inconsistent_disposition_fails(proposal: Proposal, says: str) -> None:
    check = check_proposal("r-1", proposal, context(), run={"r-2": tracked()})
    findings = [f for f in check.findings if f.kind is CheckKind.INCONSISTENT_FIELDS]
    assert findings and says in findings[0].detail, check.findings
    assert findings[0].fix


def test_new_work_is_consistent_with_a_type_and_no_fix() -> None:
    assert kinds(tracked(disposition=D.NEW, targets=())) == set()


# -- reporter text -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Tracked as bh-abc12.",
        "Labelled intake:accepted.",
        "Handled by dev/pcore.",
        "See wt/bead/bh-abc12.",
        "The key hunter2-token leaked.",
    ],
)
def test_reporter_text_that_leaks_fails(text: str) -> None:
    check = check_proposal("r-1", tracked(reporter_text=text), context())
    assert check.kinds() == {CheckKind.REPORTER_TEXT_LEAK}
    assert "hunter2-token" not in check.findings[0].detail  # a secret is never echoed


def test_reporter_text_with_no_sanitizer_bound_fails_closed() -> None:
    assert kinds(tracked(), context(sanitizer=None)) == {CheckKind.REPORTER_TEXT_LEAK}


def test_reporter_text_over_the_limit_fails() -> None:
    long = tracked(reporter_text="word " * 30)
    assert kinds(long, context(reporter_text_limit=100)) == {CheckKind.REPORTER_TEXT_LENGTH}
    assert kinds(long, context(reporter_text_limit=150)) == set()


# -- staleness ---------------------------------------------------------------------------------


def test_stale_by_thread_seq() -> None:
    check = check_proposal("r-1", tracked(thread_seq=3), context())
    assert check.kinds() == {CheckKind.STALE_THREAD} and check.stale
    assert check_proposal("r-2", tracked(thread_seq=0), context()).passed  # no thread: seq 0


def test_stale_by_a_changed_cited_path() -> None:
    facts = repo(changes={(ANALYSED, BRANCH): ("README.md", "src/login.py")})
    check = check_proposal("r-1", tracked(), context(facts))
    assert check.kinds() == {CheckKind.STALE_PATHS} and check.stale
    assert "src/login.py" in check.findings[0].detail
    assert check_proposal("r-1", tracked(evidence=("src/session.py",)), context(facts)).passed


def test_a_cited_directory_is_stale_when_a_file_under_it_changed() -> None:
    facts = repo(changes={(ANALYSED, BRANCH): ("src/auth/token.py",)})
    assert kinds(tracked(evidence=("the src/auth/ module",)), context(facts)) == {
        CheckKind.STALE_PATHS
    }
    assert kinds(tracked(evidence=("src/authz.py",)), context(facts)) == set()


def test_staleness_that_cannot_be_established_fails_closed() -> None:
    assert kinds(tracked(analysed_commit="")) == {CheckKind.STALE_UNKNOWN}
    assert kinds(tracked(), context(repo(changes={}))) == {CheckKind.STALE_UNKNOWN}
    # with no cited path there is nothing to diff, so no analysed commit is needed
    assert kinds(tracked(analysed_commit="", evidence=("thread comment 3",))) == set()


def test_cited_paths_reads_file_line_forms_and_skips_shas_urls_versions_and_bead_ids() -> None:
    evidence = (
        "src/login.py:12",
        "see ./docs/guide.md#L3-L9 and (pyproject.toml).",
        "commit f1f1f1f1, release v0.34.0, bead bh-hrx2r.1",
        "https://github.com/acme/widgets/issues/7",
        "src/auth/",
    )
    assert cited_paths(evidence) == (
        "docs/guide.md",
        "pyproject.toml",
        "src/auth",
        "src/login.py",
    )


# -- the #N a reply mentions (bh-iamru) ---------------------------------------------------------

ISSUES = {
    "#7": MentionFact("#7", "https://github.com/acme/widgets/issues/7"),  # no report here
    "#21": MentionFact("#21", "https://github.com/acme/widgets/issues/21", report="r-1"),
    "#105": MentionFact("#105", "https://github.com/acme/widgets/issues/105", report="r-2"),
}


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("See #105 and #21, then #105 again.", ("#21", "#105")),
        ("owner/repo#5 a#3 &#123; page.html#12 (#7)", ("#7",)),  # never a cross-ref or a fragment
        ("`#9` and ```\n#10\n``` but #11", ("#11",)),  # code is never autolinked
        ("https://github.com/a/b/issues/4#issuecomment-5", ()),
        ("Step #2 of 3; issue #0 is not one", ("#2",)),
        ("", ()),
    ],
)
def test_mentions_are_the_issue_references_the_tracker_would_autolink(text, named) -> None:
    assert mentions(text) == named


def test_a_mention_of_an_issue_of_the_project_passes_and_relates_its_report() -> None:
    proposal = tracked(reporter_text="This is the same as #105; see also #7 and #21.")
    check = check_proposal("r-1", proposal, context(issues=ISSUES))
    assert check.passed
    assert check.relates == ("r-2",)  # never the report itself, never an unbound issue


def test_a_mention_of_a_missing_issue_is_refused_with_its_fix() -> None:
    proposal = tracked(reporter_text="Fixed by #7; see #999 and #1000.")
    check = check_proposal("r-1", proposal, context(issues=ISSUES))
    assert check.kinds() == {CheckKind.MENTION_MISSING}
    (finding,) = check.findings
    assert "#999, #1000" in finding.detail and "#7" not in finding.detail
    assert "existing issue" in finding.fix


def test_a_mention_without_the_projects_issues_is_unverified_and_fails_closed() -> None:
    proposal = tracked(reporter_text="Same as #105.")
    gap = context(issues_gap="no tracker with issues.read is bound")
    check = check_proposal("r-1", proposal, gap)
    assert check.kinds() == {CheckKind.MENTION_UNVERIFIED}
    assert "no tracker with issues.read is bound" in check.findings[0].detail
    # text that mentions nothing needs no listing at all
    assert check_proposal("r-1", tracked(), context()).passed
