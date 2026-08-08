"""Release-channel drift detection (bh-7daa6.6) — `channels.scan` against real git.

Everything runs against a scratch repo built by :func:`_repo`: real annotated tags with controlled
tagger dates, real ``refs/remotes/origin/{latest,stable}`` planted with ``update-ref``. No network,
no fixtures shared with the doctor tests — this module is the measurement layer and is driven
directly, so a failure points at git-reading rather than at doctor's policy.
"""

from __future__ import annotations

import pytest

from beadhive import channels
from beadhive.run import run

DAY = 86400
T0 = 1_760_000_000  # arbitrary fixed epoch; every date below is T0 + n*DAY


def _git(*args, cwd, env=None):
    return run(["git", *args], cwd=str(cwd), check=True, capture=True, env=env)


def _repo(tmp_path, releases, *, latest=None, stable=None, annotated=True):
    """A repo with one commit per entry of `releases` = [(tag, day_offset), ...].

    Channel refs are planted as remote-tracking refs (what a fetched clone has), defaulting to the
    newest release for both. Pass a tag name, or a literal ``"HEAD-of-untagged"`` sentinel via
    `_untagged_commit` in the test, to place a channel elsewhere.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    at = {}
    for tag, day in releases:
        when = f"{T0 + day * DAY} +0000"
        (repo / "f.txt").write_text(tag)
        _git("add", "-A", cwd=repo)
        _git(
            "commit",
            "-qm",
            f"chore: {tag}",
            cwd=repo,
            env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
        )
        if annotated:
            _git("tag", "-a", tag, "-m", tag, cwd=repo, env={"GIT_COMMITTER_DATE": when})
        else:
            _git("tag", tag, cwd=repo)
        at[tag] = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    newest = releases[-1][0] if releases else None
    for name, target in (("latest", latest or newest), ("stable", stable or newest)):
        if target is not None:
            _git("update-ref", f"refs/remotes/origin/{name}", at.get(target, target), cwd=repo)
    return repo, at


# ---- the convention discriminator: silent unless clearly in use ---------------


def test_no_channel_refs_at_all_is_silent(tmp_path):
    repo, _ = _repo(tmp_path, [("v1.0.0", 0)])
    _git("update-ref", "-d", "refs/remotes/origin/latest", cwd=repo)
    _git("update-ref", "-d", "refs/remotes/origin/stable", cwd=repo)
    assert channels.scan(repo) is None


def test_a_lone_stable_branch_is_not_this_convention(tmp_path):
    """The false-positive that would make the check unusable: plenty of repos have a `stable`
    branch and have never heard of this ADR. Requiring `latest` too keeps us quiet there."""
    repo, _ = _repo(tmp_path, [("v1.0.0", 0)])
    _git("update-ref", "-d", "refs/remotes/origin/latest", cwd=repo)
    assert channels.scan(repo) is None


def test_channels_without_release_tags_are_silent(tmp_path):
    repo, _ = _repo(tmp_path, [("v1.0.0", 0)])
    _git("tag", "-d", "v1.0.0", cwd=repo)
    assert channels.scan(repo) is None


def test_not_a_git_repo_is_silent(tmp_path):
    (tmp_path / "plain").mkdir()
    assert channels.scan(tmp_path / "plain") is None


# ---- annotated tags: the dereference that would otherwise report EVERY channel off-tag ----


def test_annotated_tag_is_dereferenced_to_its_commit(tmp_path):
    """`refs/tags/v1.0.0` on this repo is a TAG OBJECT whose sha is not the commit the channel
    points at. Comparing raw ref shas would call a perfectly healthy channel off-tag."""
    repo, at = _repo(tmp_path, [("v1.0.0", 0)])
    tag_object = _git("rev-parse", "refs/tags/v1.0.0", cwd=repo).stdout.strip()
    assert tag_object != at["v1.0.0"], "fixture no longer builds an annotated tag"

    d = channels.scan(repo, now=T0)
    assert d["off_tag"] == []
    assert d["channels"]["latest"] == {"sha": at["v1.0.0"], "tag": "v1.0.0"}


def test_lightweight_tags_also_resolve(tmp_path):
    repo, at = _repo(tmp_path, [("v1.0.0", 0)], annotated=False)
    d = channels.scan(repo, now=T0)
    assert d["off_tag"] == []
    assert d["channels"]["stable"]["sha"] == at["v1.0.0"]


# ---- level channels ----------------------------------------------------------


def test_both_channels_on_the_newest_release_is_clean(tmp_path):
    repo, _ = _repo(tmp_path, [("v0.1.0", 0), ("v0.2.0", 3)])
    d = channels.scan(repo, now=T0 + 400 * DAY)
    assert d["off_tag"] == []
    assert d["behind_releases"] == 0
    assert d["behind_days"] == 0.0
    assert d["oldest_unpromoted"] is None


def test_age_is_not_measured_when_nothing_is_unpromoted(tmp_path):
    """A level `stable` never goes stale no matter how old the release is — the clock measures
    unpromoted releases, not the age of the current one."""
    repo, _ = _repo(tmp_path, [("v0.1.0", 0)])
    assert channels.scan(repo, now=T0 + 10_000 * DAY)["behind_days"] == 0.0


# ---- lag ---------------------------------------------------------------------


def test_lag_counts_releases_and_ages_the_OLDEST_unpromoted_one(tmp_path):
    """The clock starts at the first missed promotion, not the newest release. Measuring the
    newest would reset on every publish and read as fresh however long `stable` had been frozen:
    here the newest release is 1 day old but the rot is 20 days deep."""
    repo, _ = _repo(
        tmp_path,
        [("v0.1.0", 0), ("v0.2.0", 10), ("v0.3.0", 25), ("v0.4.0", 29)],
        stable="v0.1.0",
    )
    d = channels.scan(repo, now=T0 + 30 * DAY)
    assert d["behind_releases"] == 3
    assert d["oldest_unpromoted"] == "v0.2.0"
    assert d["behind_days"] == pytest.approx(20.0)


def test_lag_is_measured_against_latest_not_the_newest_tag(tmp_path):
    """ADR Consequence 2: a tag exists BEFORE the publish gate clears it, so the newest tag may
    name a version that never reached PyPI. `latest` is the newest published release by
    construction, so an unpublished v0.4.0 must not count against `stable`."""
    repo, _ = _repo(
        tmp_path,
        [("v0.1.0", 0), ("v0.2.0", 5), ("v0.3.0", 9), ("v0.4.0", 10)],
        latest="v0.3.0",
        stable="v0.2.0",
    )
    d = channels.scan(repo, now=T0 + 12 * DAY)
    assert d["behind_releases"] == 1
    assert d["oldest_unpromoted"] == "v0.3.0"


def test_versions_order_numerically_not_lexically(tmp_path):
    """v0.10.0 is newer than v0.9.0; a string sort says otherwise and would report -1 behind."""
    repo, _ = _repo(tmp_path, [("v0.9.0", 0), ("v0.10.0", 1)], stable="v0.9.0")
    d = channels.scan(repo, now=T0 + 1 * DAY)
    assert d["behind_releases"] == 1
    assert d["channels"]["latest"]["tag"] == "v0.10.0"


def test_non_release_tags_do_not_count_as_releases(tmp_path):
    repo, _ = _repo(tmp_path, [("v0.1.0", 0), ("v0.2.0", 2)], stable="v0.1.0")
    _git("tag", "-a", "v0.1.5rc1", "-m", "rc", "refs/tags/v0.2.0^{commit}", cwd=repo)
    _git("tag", "-a", "nightly", "-m", "n", "refs/tags/v0.2.0^{commit}", cwd=repo)
    assert channels.scan(repo, now=T0 + 3 * DAY)["behind_releases"] == 1


# ---- off-tag: the unambiguous half -------------------------------------------


def test_channel_moved_to_an_untagged_commit_is_off_tag(tmp_path):
    repo, _ = _repo(tmp_path, [("v0.1.0", 0), ("v0.2.0", 1)])
    (repo / "hand.txt").write_text("moved by hand\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "chore: hand push", cwd=repo)
    _git("update-ref", "refs/remotes/origin/stable", "HEAD", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    d = channels.scan(repo, now=T0 + 2 * DAY)
    assert d["off_tag"] == ["stable"]
    assert d["channels"]["stable"] == {"sha": head, "tag": ""}


def test_off_tag_suppresses_the_lag_numbers(tmp_path):
    """With a channel off the release line there is no position in the release order to measure
    lag from, so the tunable half reports nothing rather than something invented."""
    repo, _ = _repo(tmp_path, [("v0.1.0", 0), ("v0.2.0", 1)])
    _git("commit", "-q", "--allow-empty", "-m", "chore: hand push", cwd=repo)
    _git("update-ref", "refs/remotes/origin/latest", "HEAD", cwd=repo)
    d = channels.scan(repo, now=T0 + 900 * DAY)
    assert d["off_tag"] == ["latest"]
    assert d["behind_releases"] is None
    assert d["behind_days"] is None


def test_both_channels_off_tag_are_both_named(tmp_path):
    repo, _ = _repo(tmp_path, [("v0.1.0", 0)])
    _git("commit", "-q", "--allow-empty", "-m", "chore: hand push", cwd=repo)
    _git("update-ref", "refs/remotes/origin/latest", "HEAD", cwd=repo)
    _git("update-ref", "refs/remotes/origin/stable", "HEAD", cwd=repo)
    assert channels.scan(repo, now=T0)["off_tag"] == ["latest", "stable"]


def test_the_publish_gate_window_is_not_off_tag(tmp_path):
    """ADR Consequence 2's window — `latest` naming the PREVIOUS release while a newer tag exists
    but has not published — is a normal state and must never look like a hand-push."""
    repo, _ = _repo(tmp_path, [("v0.1.0", 0), ("v0.2.0", 1)], latest="v0.1.0", stable="v0.1.0")
    assert channels.scan(repo, now=T0 + 1 * DAY)["off_tag"] == []


# ---- misc --------------------------------------------------------------------


def test_local_branches_are_not_the_channel(tmp_path):
    """A maintainer's local `stable` is not the published channel; only remote-tracking refs are."""
    repo, _ = _repo(tmp_path, [("v0.1.0", 0), ("v0.2.0", 1)], stable="v0.1.0")
    _git("branch", "stable", "refs/tags/v0.2.0^{commit}", cwd=repo)
    assert channels.scan(repo, now=T0 + 1 * DAY)["behind_releases"] == 1


def test_remote_name_is_selectable(tmp_path):
    repo, at = _repo(tmp_path, [("v0.1.0", 0)])
    _git("update-ref", "refs/remotes/upstream/latest", at["v0.1.0"], cwd=repo)
    _git("update-ref", "refs/remotes/upstream/stable", at["v0.1.0"], cwd=repo)
    assert channels.scan(repo, remote="upstream", now=T0)["remote"] == "upstream"


def test_scan_makes_no_network_call(tmp_path, monkeypatch):
    """Doctor's standing rule (see `_hq_ahead_warnings`): no ls-remote, no fetch, no round trip."""
    repo, _ = _repo(tmp_path, [("v0.1.0", 0)])
    seen = []
    real = channels.run

    def spy(cmd, **kw):
        seen.append(list(cmd))
        return real(cmd, **kw)

    monkeypatch.setattr(channels, "run", spy)
    channels.scan(repo, now=T0)
    assert seen and all(c[1] == "for-each-ref" for c in seen)
