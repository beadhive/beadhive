"""Real-Git policy coverage for parentless top-level epic composition wrappers."""

from __future__ import annotations

import json
import subprocess

import pytest
import typer

from beadhive import host, registry, work_logic, worktree
from test_work import (
    CONFIG_YAML,
    _commit,
    _git,
    _minted_host_identity,
    _start_and_land_children,
    _wt_of,
    fakebd,
    hive,
    work,
)

__all__ = ["_minted_host_identity", "fakebd", "hive"]


@pytest.fixture(autouse=True)
def _real_ssh_signing_identity(hive, monkeypatch, tmp_path):
    """Give the hermetic dispatcher seat a real host key for signed wrapper commits."""
    key = tmp_path / "top-level-composition-signer"
    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "agents@test.dev",
            "-f",
            str(key),
            "-q",
        ],
        check=True,
    )
    public_key = key.with_suffix(".pub")
    allowed_signers = tmp_path / "allowed_signers"
    allowed_signers.write_text(
        f'agents@test.dev namespaces="git" {public_key.read_text().strip()}\n'
    )
    _git("config", "gpg.ssh.allowedSignersFile", str(allowed_signers), cwd=hive.main)
    monkeypatch.setattr(host, "signing_key", lambda: str(public_key))


def _wrap_reviewed_top_level_epic_over_advanced_integration(
    hive,
    fakebd,
    *,
    epic="mr-top-level-composed",
    count=2,
    subject="",
    reverse_parents=False,
    direct_noise=False,
    integration="main",
):
    """Build the canonical signed top-level recovery graph with ordered real-Git parents."""
    seat = _start_and_land_children(hive, fakebd, epic=epic, count=count)
    branch = f"wt/bead/epic/{epic}"
    if direct_noise:
        _commit(seat, "chore: unreviewed top-level noise", fname="top-level-noise.txt")
    reviewed_tip = _git("rev-parse", branch, cwd=hive.main).stdout.strip()

    _commit(hive.main, "feat: advance configured integration", fname="integration-advance.txt")
    integration_tip = _git("rev-parse", integration, cwd=hive.main).stdout.strip()
    message = subject or f"chore(merge): compose {epic} onto {integration}"
    if reverse_parents:
        _git("merge", "--no-ff", integration_tip, "-m", message, cwd=seat)
    else:
        _git("checkout", "-q", "--detach", reviewed_tip, cwd=seat)
        _git("branch", "-f", branch, integration_tip, cwd=hive.main)
        _git("checkout", "-q", branch, cwd=seat)
        _git("merge", "--no-ff", reviewed_tip, "-m", message, cwd=seat)

    wrapper = _git("rev-parse", branch, cwd=hive.main).stdout.strip()
    actual_parents = _git("show", "-s", "--format=%P", wrapper, cwd=hive.main).stdout.split()
    expected_parents = (
        [reviewed_tip, integration_tip] if reverse_parents else [integration_tip, reviewed_tip]
    )
    assert actual_parents == expected_parents
    assert (
        "gpgsig -----BEGIN SSH SIGNATURE-----"
        in _git("cat-file", "-p", wrapper, cwd=hive.main).stdout
    )
    return seat


def _wrap_reviewed_side_that_absorbed_advanced_main(hive, fakebd, *, epic: str):
    """Reproduce the real bh-j5uyb graph: old first-parent spine, new main via a child side."""
    seat = _start_and_land_children(hive, fakebd, epic=epic, count=2)
    branch = f"wt/bead/epic/{epic}"
    boundary = _git("rev-parse", "main", cwd=hive.main).stdout.strip()

    child = f"{epic}.3"
    fakebd.seed(child, title="main ancestry reconciliation", parent=epic)
    work.claim(bead=child, as_="dev/reconcile", hive="myrepo")
    child_seat = _wt_of(hive, child)

    _commit(hive.main, "feat: advance integration after epic start", fname="advanced-main.txt")
    integration_tip = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    _commit(child_seat, "fix: reconcile reviewed work with main", fname="reconciled.txt")
    _git(
        "merge",
        "--no-ff",
        "main",
        "-m",
        "chore(test): absorb advanced main lineage",
        cwd=child_seat,
    )
    work.submit(bead=child, as_="dev/reconcile", hive="myrepo")
    fakebd.resolve_review(child)
    work.merge(bead=child, hive="myrepo", rm=False, molecule=False)
    reviewed_tip = _git("rev-parse", branch, cwd=hive.main).stdout.strip()

    _git("checkout", "-q", "--detach", reviewed_tip, cwd=seat)
    _git("branch", "-f", branch, integration_tip, cwd=hive.main)
    _git("checkout", "-q", branch, cwd=seat)
    _git(
        "merge",
        "--no-ff",
        reviewed_tip,
        "-m",
        f"chore(merge): compose {epic} onto main",
        cwd=seat,
    )
    return seat, boundary, integration_tip, reviewed_tip


def test_top_level_epic_submit_accepts_root_first_wrapper_over_configured_main(
    hive, fakebd, capsys
):
    """A parentless epic binds its one recovery wrapper to the configured integration branch."""
    epic = "mr-top-level-composed"
    _wrap_reviewed_top_level_epic_over_advanced_integration(hive, fakebd, epic=epic)

    capsys.readouterr()
    work.show(bead=epic, view=["log"], json_out=True, hive="myrepo")
    policy = json.loads(capsys.readouterr().out)["history_policy"]
    assert policy == {
        "kind": "epic-reviewed-topology",
        "configured_max_commits": 10,
        "effective_max_commits": 5,
        "direct_children": 2,
        "integrated_children": 2,
        "basis": "5 linked/topology commit(s) from 2 reviewed direct-child integration(s)",
        "valid": True,
        "errors": [],
    }

    work.submit(bead=epic, as_="disp/lead", hive="myrepo")
    assert fakebd.states[epic]["review"] == "pending"


def test_top_level_epic_accepts_reviewed_first_parent_boundary_ancestral_to_advanced_main(
    hive, fakebd, capsys
):
    """The bh-j5uyb shape audits only commits above main despite its older reviewed spine."""
    epic = "mr-top-level-reviewed-main-ancestry"
    seat, boundary, integration_tip, reviewed_tip = _wrap_reviewed_side_that_absorbed_advanced_main(
        hive, fakebd, epic=epic
    )
    wrapper = _git("rev-parse", f"wt/bead/epic/{epic}", cwd=hive.main).stdout.strip()

    assert _git("show", "-s", "--format=%P", wrapper, cwd=hive.main).stdout.split() == [
        integration_tip,
        reviewed_tip,
    ]
    assert _git("merge-base", "--is-ancestor", boundary, integration_tip, cwd=hive.main)

    capsys.readouterr()
    work.show(bead=epic, view=["log"], json_out=True, hive="myrepo")
    policy = json.loads(capsys.readouterr().out)["history_policy"]
    assert policy["valid"], policy["errors"]
    assert policy["integrated_children"] == 3
    assert policy["direct_children"] == 3
    assert "linked/topology" in policy["basis"]

    work.submit(bead=epic, as_="disp/lead", hive="myrepo")
    assert fakebd.states[epic]["review"] == "pending"
    assert seat.exists()


def test_reviewed_side_rejects_excluded_boundary_not_ancestral_to_exact_integration_base(hive):
    """A missing range row is not trusted merely because it terminates the first-parent walk."""
    entry = registry.resolve_hive(work_logic.config.load(), "myrepo")
    integration_base = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
    tree = _git("rev-parse", f"{integration_base}^{{tree}}", cwd=hive.main).stdout.strip()
    unrelated_boundary = _git(
        "commit-tree", tree, "-m", "chore: unrelated boundary", cwd=hive.main
    ).stdout.strip()
    reviewed_tip = _git(
        "commit-tree",
        tree,
        "-p",
        unrelated_boundary,
        "-m",
        "feat: reviewed side",
        cwd=hive.main,
    ).stdout.strip()
    rows = worktree.commit_rows(entry, unrelated_boundary, reviewed_tip)

    spine, errors = work_logic._reviewed_side_spine(entry, rows, reviewed_tip, integration_base)

    assert spine == []
    assert errors == [f"first-parent spine leaves the review range at {unrelated_boundary[:8]}"]


def test_top_level_epic_composition_target_comes_from_nondefault_integration_branch(
    hive, fakebd, capsys
):
    """The top-level identity is configured policy, not a hard-coded ``main`` token."""
    hive.cfg_path.write_text(
        CONFIG_YAML.replace(
            'review_gate: "human"', 'review_gate: "human"\n  integration_branch: trunk'
        )
    )
    _git("branch", "-m", "main", "trunk", cwd=hive.main)
    epic = "mr-top-level-trunk"
    _wrap_reviewed_top_level_epic_over_advanced_integration(
        hive, fakebd, epic=epic, integration="trunk"
    )

    capsys.readouterr()
    work.show(bead=epic, view=["log"], json_out=True, hive="myrepo")
    policy = json.loads(capsys.readouterr().out)["history_policy"]
    assert policy["valid"]
    assert policy["integrated_children"] == 2

    work.submit(bead=epic, as_="disp/lead", hive="myrepo")
    assert fakebd.states[epic]["review"] == "pending"


@pytest.mark.parametrize(
    ("malformation", "expected"),
    [
        ("wrong-target", "names parent release, expected main"),
        ("reversed", "must use the exact integration base as first parent"),
        ("duplicate", "appears more than once"),
        ("stacked", "stacked reviewed-side wrapper"),
        ("direct-noise", "unaccounted direct epic commit"),
        ("missing-linkage", "not linked"),
        ("empty-reviewed-side", "empty reviewed side"),
        ("parent-count", "must have exactly two parents"),
    ],
)
def test_top_level_root_first_wrapper_rejects_noncanonical_or_unreviewed_history(
    hive, fakebd, capsys, malformation, expected
):
    """Top-level composition is one narrow allowance, not a subject-based history bypass."""
    epic = f"mr-top-level-{malformation}"
    branch = f"wt/bead/epic/{epic}"

    if malformation == "empty-reviewed-side":
        seat = _start_and_land_children(hive, fakebd, epic=epic, count=0)
        reviewed_tip = _git("rev-parse", branch, cwd=hive.main).stdout.strip()
        _commit(hive.main, "feat: advance configured main", fname="main-advance.txt")
        main_tip = _git("rev-parse", "main", cwd=hive.main).stdout.strip()
        tree = _git("rev-parse", f"{main_tip}^{{tree}}", cwd=hive.main).stdout.strip()
        wrapper = _git(
            "commit-tree",
            tree,
            "-p",
            main_tip,
            "-p",
            reviewed_tip,
            "-m",
            f"chore(merge): compose {epic} onto main",
            cwd=hive.main,
        ).stdout.strip()
        _git("reset", "--hard", wrapper, cwd=seat)
    else:
        seat = _wrap_reviewed_top_level_epic_over_advanced_integration(
            hive,
            fakebd,
            epic=epic,
            subject=(
                f"chore(merge): compose {epic} onto release"
                if malformation == "wrong-target"
                else ""
            ),
            reverse_parents=malformation == "reversed",
            direct_noise=malformation == "direct-noise",
        )
        wrapper = _git("rev-parse", branch, cwd=hive.main).stdout.strip()
        parents = _git("show", "-s", "--format=%P", wrapper, cwd=hive.main).stdout.split()
        main_tip, reviewed_tip = (
            (parents[1], parents[0]) if malformation == "reversed" else (parents[0], parents[1])
        )
        message = f"chore(merge): compose {epic} onto main"

        if malformation == "duplicate":
            tree = _git("rev-parse", f"{wrapper}^{{tree}}", cwd=hive.main).stdout.strip()
            malformed = _git(
                "commit-tree",
                tree,
                "-p",
                wrapper,
                "-p",
                reviewed_tip,
                "-m",
                message,
                cwd=hive.main,
            ).stdout.strip()
            _git("reset", "--hard", malformed, cwd=seat)
        elif malformation == "stacked":
            nested_base = _git("merge-base", main_tip, reviewed_tip, cwd=hive.main).stdout.strip()
            reviewed_tree = _git(
                "rev-parse", f"{reviewed_tip}^{{tree}}", cwd=hive.main
            ).stdout.strip()
            inner = _git(
                "commit-tree",
                reviewed_tree,
                "-p",
                reviewed_tip,
                "-p",
                nested_base,
                "-m",
                message,
                cwd=hive.main,
            ).stdout.strip()
            main_tree = _git("rev-parse", f"{main_tip}^{{tree}}", cwd=hive.main).stdout.strip()
            malformed = _git(
                "commit-tree",
                main_tree,
                "-p",
                main_tip,
                "-p",
                inner,
                "-m",
                message,
                cwd=hive.main,
            ).stdout.strip()
            _git("reset", "--hard", malformed, cwd=seat)
        elif malformation == "missing-linkage":
            fakebd.beads[f"{epic}.1"]["metadata"].pop("git.commits")
        elif malformation == "parent-count":
            tree = _git("rev-parse", f"{wrapper}^{{tree}}", cwd=hive.main).stdout.strip()
            third_parent = _git("rev-parse", f"{reviewed_tip}^1", cwd=hive.main).stdout.strip()
            malformed = _git(
                "commit-tree",
                tree,
                "-p",
                main_tip,
                "-p",
                reviewed_tip,
                "-p",
                third_parent,
                "-m",
                message,
                cwd=hive.main,
            ).stdout.strip()
            _git("reset", "--hard", malformed, cwd=seat)

    capsys.readouterr()
    work.show(bead=epic, view=["log"], json_out=True, hive="myrepo")
    policy = json.loads(capsys.readouterr().out)["history_policy"]
    assert not policy["valid"]
    assert any(expected in error for error in policy["errors"])

    with pytest.raises(typer.Exit):
        work.submit(bead=epic, as_="disp/lead", hive="myrepo")
    assert not fakebd.did("set-state", epic, "review=pending")
