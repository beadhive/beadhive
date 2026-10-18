"""Explicit public-store publication to its Git Dolt ref (bh-lniwf.8) — hermetic.

The Engine is faked; the real push against bd, Dolt and Git (including the non-fast-forward
refusal over another corpus) is proven in tests/test_two_store_e2e_int.py.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import public_publish
from beadhive.public_publish import PublishError
from beadhive.store_target import BEADS_DIR_ENV, StoreMissing, StoreNotConfigured

INTERNAL = "s3://acme-beads/internal/github/acme/widget"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    (root / "github" / "acme" / "widget").mkdir(parents=True)
    public_root = tmp_path / "stores" / "widget-public"
    (public_root / ".beads").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    monkeypatch.delenv(BEADS_DIR_ENV, raising=False)
    return public_root


def _entry(public_root, **public):
    return {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "widget",
        "dolt_remote": {"name": "widget-private", "url": INTERNAL},
        "stores": {
            "internal": {"remote": "widget-private"},
            "public": {
                "prefix": "widpub",
                "path": str(public_root),
                "remote": "git-origin",
                "source": "github-issues",
                **public,
            },
        },
    }


def _done(rc=0, stderr=""):
    return subprocess.CompletedProcess(["bd"], rc, stdout="", stderr=stderr)


class FakeEngine:
    def __init__(self, *, add_rc=0, push_rc=0, push_err=""):
        self.add_rc, self.push_rc, self.push_err = add_rc, push_rc, push_err
        self.calls: list[tuple] = []

    def add_remote(self, cwd, name, url, *, extra_env=None):
        self.calls.append(("add", str(cwd), name, url, dict(extra_env or {})))
        return _done(self.add_rc, "remote 'git-origin' is already registered elsewhere")

    def push_state(self, cwd, actor="", message="", *, remote="", force=False, extra_env=None):
        self.calls.append(("push", str(cwd), remote, force, dict(extra_env or {})))
        return _done(self.push_rc, self.push_err)


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        ("/srv/git/widget.git", "git+file:///srv/git/widget.git"),
        ("file:///srv/git/widget.git", "git+file:///srv/git/widget.git"),
        ("https://github.com/acme/widget.git", "git+https://github.com/acme/widget.git"),
        ("ssh://git@github.com/acme/widget.git", "git+ssh://git@github.com/acme/widget.git"),
        ("git@github.com:acme/widget.git", "git+ssh://git@github.com/acme/widget.git"),
        ("git+ssh://git@github.com/acme/widget.git", "git+ssh://git@github.com/acme/widget.git"),
    ],
)
def test_git_origin_urls_map_to_dolt_git_remotes(raw, url):
    assert public_publish.dolt_git_url(raw) == url


def test_git_origin_publishes_to_the_code_origin_with_the_store_selected(ws):
    eng = FakeEngine()
    report = public_publish.publish_public(
        _entry(ws), eng=eng, origin_url="https://github.com/acme/widget.git"
    )
    assert report.pushed and report.remote == "git-origin"
    url = "git+https://github.com/acme/widget.git"
    selected = {BEADS_DIR_ENV: str(ws / ".beads")}
    assert eng.calls == [
        ("add", str(ws), "git-origin", url, selected),
        ("push", str(ws), "git-origin", False, selected),  # never forced
    ]


def test_a_nested_git_descriptor_is_the_destination(ws):
    nested = {"name": "widget-pub", "url": "git+https://github.com/acme/widget-beads.git"}
    eng = FakeEngine()
    public_publish.publish_public(_entry(ws, remote="widget-pub", dolt_remote=nested), eng=eng)
    assert [c[3] for c in eng.calls if c[0] == "add"] == [nested["url"]]


def test_dry_run_plans_and_touches_nothing(ws):
    eng = FakeEngine()
    report = public_publish.publish_public(_entry(ws), eng=eng, dry_run=True, origin_url="/o.git")
    assert report.dry_run and not report.pushed and eng.calls == []


@pytest.mark.parametrize("drop", ["stores", "public"])
def test_a_hive_without_a_public_store_is_never_published(ws, drop):
    entry = _entry(ws)
    if drop == "stores":
        del entry["stores"]
    else:
        del entry["stores"]["public"]
    eng = FakeEngine()
    with pytest.raises(StoreNotConfigured):
        public_publish.publish_public(entry, eng=eng, origin_url="/o.git")
    assert eng.calls == []


def test_a_non_git_destination_is_refused(ws):
    nested = {"name": "widget-pub", "url": "s3://acme-beads/public/github/acme/widget"}
    eng = FakeEngine()
    with pytest.raises(PublishError, match="only to a Git Dolt ref"):
        public_publish.publish_public(_entry(ws, remote="widget-pub", dolt_remote=nested), eng=eng)
    assert eng.calls == []


def test_the_internal_stores_remote_is_never_a_publication_target(ws):
    entry = _entry(ws)
    entry["dolt_remote"] = {"name": "widget-private", "url": "git+file:///srv/git/widget.git"}
    with pytest.raises(PublishError, match="internal store's remote"):
        public_publish.plan_publication(entry, origin_url="/srv/git/widget.git")


def test_a_checkout_without_a_git_origin_is_refused(ws):
    with pytest.raises(PublishError, match="has no Git origin"):
        public_publish.plan_publication(_entry(ws), origin_url="")


def test_an_uninitialised_public_store_is_refused_before_any_call(ws):
    (ws / ".beads").rmdir()
    eng = FakeEngine()
    with pytest.raises(StoreMissing):
        public_publish.publish_public(_entry(ws), eng=eng, origin_url="/o.git")
    assert eng.calls == []


def test_a_rejected_push_is_reported_redacted_and_never_forced(ws):
    eng = FakeEngine(push_rc=1, push_err="! [rejected] main -> main (non-fast-forward)")
    with pytest.raises(PublishError) as caught:
        public_publish.publish_public(_entry(ws), eng=eng, origin_url="/o.git")
    assert "non-fast-forward" in str(caught.value) and "nothing was forced" in str(caught.value)
    assert all(not call[3] for call in eng.calls if call[0] == "push")


def test_a_registration_conflict_stops_before_the_push(ws):
    eng = FakeEngine(add_rc=1)
    with pytest.raises(PublishError, match="could not register"):
        public_publish.publish_public(_entry(ws), eng=eng, origin_url="/o.git")
    assert [call[0] for call in eng.calls] == ["add"]
