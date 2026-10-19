"""Explicit public-store publication to its Git Dolt ref (bh-lniwf.8) — hermetic.

The Engine is faked; the real push against bd, Dolt and Git (including the non-fast-forward
refusal over another corpus) is proven in tests/test_two_store_e2e_int.py.
"""

from __future__ import annotations

import json
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


# --- guarded REPLACE of refs/dolt/data (bh-31rf7): real bare git remotes, no network ---------


def _git(cwd, *args):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ["PATH"],
    }
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env
    )
    return out.stdout.strip()


def _refs(bare):
    return dict(
        line.split(" ", 1)[::-1]
        for line in _git(bare, "for-each-ref", "--format=%(objectname) %(refname)").splitlines()
    )


@pytest.fixture
def origin(tmp_path):
    """A bare code origin: a branch, a tag and a legacy, unrelated refs/dolt/data history."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "code.txt").write_text("code")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "code")
    _git(seed, "tag", "v1")
    _git(seed, "push", "-q", str(bare), "main", "v1")
    _git(seed, "checkout", "-q", "--orphan", "legacy")
    (seed / "code.txt").write_text("legacy dolt corpus")
    _git(seed, "commit", "-q", "-am", "legacy")
    _git(seed, "push", "-q", str(bare), "HEAD:refs/dolt/data")
    # the unrelated public history that the replacement will force into place
    _git(seed, "checkout", "-q", "--orphan", "public")
    (seed / "code.txt").write_text("audited public store")
    _git(seed, "commit", "-q", "-am", "public")
    return bare, seed


class ForcingEngine(FakeEngine):
    """Stands in for `bd dolt push --force`: forces the public history over refs/dolt/data."""

    def __init__(self, bare, seed, **kw):
        super().__init__(**kw)
        self.bare, self.seed = bare, seed

    def push_state(self, cwd, actor="", message="", *, remote="", force=False, extra_env=None):
        res = super().push_state(
            cwd, actor, message, remote=remote, force=force, extra_env=extra_env
        )
        if res.returncode == 0:
            flag = ["--force"] if force else []
            moved = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.seed),
                    "push",
                    "-q",
                    *flag,
                    str(self.bare),
                    "HEAD:refs/dolt/data",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            return _done(moved.returncode, moved.stderr)
        return res


def _replace(ws, bare, eng, **kw):
    kw.setdefault("lease", "")
    kw.setdefault("confirmed", False)
    return public_publish.publish_public(_entry(ws), eng=eng, origin_url=str(bare), **kw)


def test_default_mode_still_refuses_a_non_fast_forward(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    eng = ForcingEngine(bare, seed)
    with pytest.raises(PublishError, match="non-fast-forward|failed"):
        public_publish.publish_public(_entry(ws), eng=eng, origin_url=str(bare))
    assert _refs(bare) == before
    assert all(not call[3] for call in eng.calls if call[0] == "push")  # never forced


def test_replace_dry_run_names_everything_and_writes_nothing(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    eng = ForcingEngine(bare, seed)
    report = _replace(ws, bare, eng, dry_run=True, lease=before["refs/dolt/data"])
    rep = report.replace
    assert report.dry_run and not report.pushed and eng.calls == []
    assert rep.ref == "refs/dolt/data" and rep.old_sha == before["refs/dolt/data"] and rep.matches
    assert {r for r, _ in rep.code_refs} == {"refs/heads/main", "refs/tags/v1"}
    assert "widpub" in rep.new_store and "rollback" in rep.rollback
    text = "\n".join(public_publish.render_report(report))
    # never-restore (bh-l3zvc): no surface advises re-pushing the old ref
    for surface in (text, rep.rollback, json.dumps(public_publish.payload(report))):
        assert "NEVER restore the old SHA" in surface and "private archive" in surface
        assert "restore the old SHA with a lease" not in surface
        assert "git push --force-with-lease" not in surface
    assert before["refs/dolt/data"] in text and "refs/heads/main" in text
    assert _refs(bare) == before
    doc = public_publish.payload(report)
    assert doc["command"] == "hive remote sync" and doc["replace"]["cas_match"] is True


def test_cas_mismatch_refuses_and_changes_nothing(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    eng = ForcingEngine(bare, seed)
    with pytest.raises(PublishError, match="compare-and-swap refused"):
        _replace(ws, bare, eng, lease="0" * 40, confirmed=True)
    assert eng.calls == [] and _refs(bare) == before


def test_replace_needs_confirmation_and_an_expected_sha(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    eng = ForcingEngine(bare, seed)
    with pytest.raises(PublishError, match="--confirm"):
        _replace(ws, bare, eng, lease=before["refs/dolt/data"])
    with pytest.raises(PublishError, match="only applies with --force-with-lease"):
        _replace(ws, bare, eng, confirmed=True)
    with pytest.raises(PublishError, match="full 40"):
        _replace(ws, bare, eng, lease="abc", confirmed=True)
    assert eng.calls == [] and _refs(bare) == before


def test_replace_moves_only_the_dolt_ref(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    eng = ForcingEngine(bare, seed)
    report = _replace(ws, bare, eng, lease=before["refs/dolt/data"], confirmed=True)
    after = _refs(bare)
    assert report.pushed and report.new_sha == after["refs/dolt/data"] != before["refs/dolt/data"]
    assert {k: v for k, v in after.items() if k != "refs/dolt/data"} == {
        k: v for k, v in before.items() if k != "refs/dolt/data"
    }  # branches and tags untouched; old data not parked under another ref
    assert [c[3] for c in eng.calls if c[0] == "push"] == [True]


def test_replace_is_refused_while_the_internal_store_uses_the_code_origin(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    entry = _entry(ws)
    entry["dolt_remote"] = {"name": "widget-private", "url": f"git+file://{bare}"}
    eng = ForcingEngine(bare, seed)
    with pytest.raises(PublishError, match="internal store"):
        public_publish.publish_public(
            entry,
            eng=eng,
            origin_url=str(bare),
            lease=before["refs/dolt/data"],
            confirmed=True,
        )
    assert eng.calls == [] and _refs(bare) == before


def test_replace_refuses_when_there_is_no_ref_to_replace(ws, tmp_path):
    bare = tmp_path / "empty.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    with pytest.raises(PublishError, match="nothing"):
        _replace(ws, bare, FakeEngine(), dry_run=True, lease="a" * 40)


def test_confirm_alone_is_refused_without_a_lease(ws):
    with pytest.raises(PublishError, match="only applies with --force-with-lease"):
        public_publish.publish_public(
            _entry(ws), eng=FakeEngine(), origin_url="/o.git", confirmed=True
        )


def test_plain_dry_run_names_the_replicas_current_ref(ws, origin):
    bare, seed = origin
    before = _refs(bare)
    eng = ForcingEngine(bare, seed)
    report = public_publish.publish_public(_entry(ws), eng=eng, dry_run=True, origin_url=str(bare))
    assert report.remote_sha == before["refs/dolt/data"] and eng.calls == []
    assert before["refs/dolt/data"] in "\n".join(public_publish.render_report(report))


def test_dolt_owned_marker_branch_is_not_a_protected_code_ref():
    refs = {
        "refs/dolt/data": "a" * 40,
        "refs/heads/main": "b" * 40,
        "refs/heads/__dolt_remote_info__": "c" * 40,
        "refs/tags/v1": "d" * 40,
    }
    assert public_publish._code_refs(refs) == (
        ("refs/heads/main", "b" * 40),
        ("refs/tags/v1", "d" * 40),
    )
