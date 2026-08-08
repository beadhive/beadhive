"""`bh doctor`'s release-channel drift warnings (bh-7daa6.6) — the policy layer.

`channels.scan` measures (tests/test_channels.py); this module owns the decisions that hang off
the measurement: which findings warn, what the thresholds are, and — the acceptance criterion the
whole bead rests on — that none of it can gate anything.

Uses the real `hive` fixture from test_work, so the warnings run against the same registered-hive
shape doctor sees in production.
"""

from __future__ import annotations

import time

import pytest

from beadhive import config, doctor
from beadhive.run import run
from test_work import _git, fakebd, hive  # noqa: F401 — fixtures resolved by name

DAY = 86400


def _release(main, tag, *, ago_days):
    """Tag a fresh commit as a release, dated `ago_days` in the past."""
    when = f"{int(time.time()) - ago_days * DAY} +0000"
    (main / "f.txt").write_text(tag)
    _git("add", "-A", cwd=main)
    run(
        ["git", "commit", "-qm", f"chore: {tag}"],
        cwd=str(main),
        check=True,
        capture=True,
        env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
    )
    run(
        ["git", "tag", "-a", tag, "-m", tag],
        cwd=str(main),
        check=True,
        capture=True,
        env={"GIT_COMMITTER_DATE": when},
    )


def _channel(main, name, ref="HEAD"):
    _git("update-ref", f"refs/remotes/origin/{name}", ref, cwd=main)


def _warns(cfg=None):
    cfg = cfg if cfg is not None else config.load()
    return doctor._channel_drift_warnings(cfg, cfg.get("managed_repos", []) or [])


def _seed(main, *releases):
    """Releases as (tag, ago_days), oldest first; both channels left on the newest."""
    for tag, ago in releases:
        _release(main, tag, ago_days=ago)
    _channel(main, "latest")
    _channel(main, "stable")


# ---- silence where the convention is not in use ------------------------------


def test_an_ordinary_hive_says_nothing(hive, fakebd):  # noqa: F811
    """The common case by a wide margin: a workspace whose repos have no channel branches. This
    must cost nothing and print nothing, or the check is a tax on every `bh doctor` run."""
    assert _warns() == []


def test_a_hive_with_no_checkout_is_skipped(hive, fakebd, monkeypatch):  # noqa: F811
    cfg = config.load()
    cfg["managed_repos"].append(
        {"provider": "github", "org": "myorg", "repo": "absent", "prefix": "ab", "kind": "personal"}
    )
    assert _warns(cfg) == []


# ---- the off-tag half: no threshold, always reported -------------------------


def test_a_hand_moved_channel_is_reported_with_no_threshold(hive, fakebd):  # noqa: F811
    _seed(hive.main, ("v1.0.0", 1))
    _git("commit", "-q", "--allow-empty", "-m", "chore: moved by hand", cwd=hive.main)
    _channel(hive.main, "stable")

    (w,) = _warns()
    assert "release channel 'stable'" in w
    assert "carries no release tag" in w
    assert "moved outside the automation" in w
    # Names the sha so the reader can place it, and points at the ADR rather than restating it.
    head = _git("rev-parse", "HEAD", cwd=hive.main).stdout.strip()
    assert head[:9] in w
    assert "release-channel-branches-adr.md" in w


def test_off_tag_does_not_offer_an_automatic_repair(hive, fakebd):  # noqa: F811
    """Decision 1 makes the channels forward-only, so there IS no correct automatic fix — the
    warning has to say so rather than imply bh will sort it out."""
    _seed(hive.main, ("v1.0.0", 1))
    _git("commit", "-q", "--allow-empty", "-m", "chore: moved by hand", cwd=hive.main)
    _channel(hive.main, "latest")

    (w,) = _warns()
    assert "reconcile it out of band" in w
    assert "will not move it for you" in w


def test_off_tag_fires_even_when_the_channel_is_perfectly_current(hive, fakebd):  # noqa: F811
    """No staleness at all — `stable` is level with `latest` — and it still warns, because being
    off a release tag is wrong on its own terms."""
    _seed(hive.main, ("v1.0.0", 0))
    _git("commit", "-q", "--allow-empty", "-m", "chore: both moved by hand", cwd=hive.main)
    _channel(hive.main, "latest")
    _channel(hive.main, "stable")

    assert len(_warns()) == 2


# ---- the lag half: threshold-driven ------------------------------------------


def test_a_soaking_stable_under_the_threshold_is_silent(hive, fakebd):  # noqa: F811
    """The state the channel EXISTS for. A warning here would fire on every healthy soak and be
    muted within a week."""
    _seed(hive.main, ("v1.0.0", 20))
    _release(hive.main, "v1.1.0", ago_days=2)
    _channel(hive.main, "latest")

    assert _warns() == []


def test_stable_past_the_age_threshold_is_reported(hive, fakebd):  # noqa: F811
    _seed(hive.main, ("v1.0.0", 60))
    _release(hive.main, "v1.1.0", ago_days=30)
    _channel(hive.main, "latest")

    (w,) = _warns()
    assert "1 release(s) behind 'latest' (v1.0.0 → v1.1.0)" in w
    assert "oldest unpromoted release v1.1.0 has been sitting for 30 days" in w
    assert "release.channel_stale_days=14" in w


def test_the_age_clock_starts_at_the_FIRST_missed_promotion(hive, fakebd):  # noqa: F811
    """A fresh release must not reset the clock — otherwise a repo that publishes weekly and
    promotes never would look healthy forever, which is the exact rot being hunted."""
    _seed(hive.main, ("v1.0.0", 90))
    _release(hive.main, "v1.1.0", ago_days=40)
    _release(hive.main, "v1.2.0", ago_days=0)
    _channel(hive.main, "latest")

    (w,) = _warns()
    assert "2 release(s) behind" in w
    assert "v1.1.0 has been sitting for 40 days" in w


def test_the_age_threshold_is_configurable_per_hive(hive, fakebd):  # noqa: F811
    """Cadence is a property of the repo, so the knob layers per-hive like the rest of release.*."""
    _seed(hive.main, ("v1.0.0", 30))
    _release(hive.main, "v1.1.0", ago_days=5)
    _channel(hive.main, "latest")

    assert _warns() == []  # 5 days < the 14-day default
    cfg = config.load()
    cfg["managed_repos"][0]["release"] = {"channel_stale_days": 3}
    assert len(_warns(cfg)) == 1


def test_zero_days_disables_the_age_check(hive, fakebd):  # noqa: F811
    _seed(hive.main, ("v1.0.0", 900))
    _release(hive.main, "v1.1.0", ago_days=800)
    _channel(hive.main, "latest")

    cfg = config.load()
    cfg["release"] = {"channel_stale_days": 0}
    assert _warns(cfg) == []


def test_the_release_count_check_is_off_by_default(hive, fakebd):  # noqa: F811
    """Three releases in an afternoon is an ordinary patch burst here, not neglect — which is why
    the count threshold ships disabled and the age clock carries the default."""
    _seed(hive.main, ("v1.0.0", 1))
    for tag in ("v1.0.1", "v1.0.2", "v1.0.3"):
        _release(hive.main, tag, ago_days=1)
    _channel(hive.main, "latest")

    assert _warns() == []


def test_the_release_count_check_fires_when_enabled(hive, fakebd):  # noqa: F811
    _seed(hive.main, ("v1.0.0", 1))
    for tag in ("v1.0.1", "v1.0.2"):
        _release(hive.main, tag, ago_days=1)
    _channel(hive.main, "latest")

    cfg = config.load()
    cfg["release"] = {"channel_stale_releases": 2}
    (w,) = _warns(cfg)
    assert "release.channel_stale_releases=2" in w
    # ORs with the age check rather than replacing it: the age half is under threshold here and
    # correctly absent from the reason list.
    assert "sitting for" not in w


def test_off_tag_suppresses_the_lag_warning_rather_than_stacking(hive, fakebd):  # noqa: F811
    """One finding per problem. An off-tag `latest` makes the lag number meaningless, so reporting
    both would be one real problem dressed as two."""
    _seed(hive.main, ("v1.0.0", 400))
    _release(hive.main, "v1.1.0", ago_days=300)
    _git("commit", "-q", "--allow-empty", "-m", "chore: moved by hand", cwd=hive.main)
    _channel(hive.main, "latest")

    (w,) = _warns()
    assert "carries no release tag" in w


# ---- it reports, it never gates ----------------------------------------------


@pytest.mark.parametrize("broken", ["off_tag", "stale"])
def test_doctor_still_exits_zero_with_a_channel_finding(hive, fakebd, capsys, broken):  # noqa: F811
    """The acceptance criterion: `stable` lagging is a NORMAL state during a soak, so no finding
    here may fail anything. Placing the check in doctor — which always exits 0 — makes that a
    property of the placement rather than a promise. Both findings are proved, since either could
    have been wired to a raise."""
    _seed(hive.main, ("v1.0.0", 400))
    if broken == "off_tag":
        _git("commit", "-q", "--allow-empty", "-m", "chore: moved by hand", cwd=hive.main)
        _channel(hive.main, "stable")
    else:
        _release(hive.main, "v1.1.0", ago_days=300)
        _channel(hive.main, "latest")

    doctor.doctor()  # returns None / raises nothing → `bh doctor` exits 0
    out = capsys.readouterr().out
    assert "# Warnings (" in out
    assert "release channel" in out


def test_the_finding_reaches_the_rendered_warnings_section(hive, fakebd):  # noqa: F811
    """Wired into `_data_warnings`, not just reachable — so it is counted in `# Warnings (N)`,
    the list a reader actually scans, and rides `bh doctor`'s structured payload for free."""
    _seed(hive.main, ("v1.0.0", 1))
    _git("commit", "-q", "--allow-empty", "-m", "chore: moved by hand", cwd=hive.main)
    _channel(hive.main, "stable")

    payload = doctor._collect(config.load())
    assert any("release channel 'stable'" in w for w in payload["warnings"])
