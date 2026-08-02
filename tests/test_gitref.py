"""``gitref`` — JSON records at git refs, updated by compare-and-swap (bh-ytbb.6).

These tests pin the two git behaviours the whole multi-host write fence rests on, against a
REAL scratch bare repo (never a mock, never the operator's HQ):

  * ``--force-with-lease=<ref>:<expected>`` rejects a push when the remote moved.
  * ``--force-with-lease=<ref>:`` (empty expected) asserts the ref does NOT exist yet.

If a future git changed either, this file fails rather than the fence silently weakening.

Every remote here is a bare repo under the test's own ``tmp_path``, wired as a plain local
path — no network remote, and nothing that can resolve to ``~/.beadhive/hq``.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import gitref


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


@pytest.fixture
def remote(tmp_path):
    """A scratch BARE repo standing in for the HQ/hive remote."""
    path = tmp_path / "remote.git"
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return str(path)


@pytest.fixture
def clone(tmp_path):
    """A scratch working repo (the object db `hash-object -w` writes into)."""
    path = tmp_path / "clone"
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    return path


REF = "refs/bh/lease/tt"


# ---- encoding ------------------------------------------------------------------


def test_encode_is_canonical_so_the_sha_is_a_function_of_content():
    a = gitref.encode({"b": 2, "a": 1})
    b = gitref.encode({"a": 1, "b": 2})
    assert a == b == '{"a":1,"b":2}\n'


def test_decode_rejects_a_non_object_record():
    with pytest.raises(ValueError):
        gitref.decode("[1, 2]")


# ---- CAS against a real remote --------------------------------------------------


def test_cas_from_absent_creates_the_ref(remote, clone):
    result = gitref.cas(remote, REF, {"epoch": 1}, expected=gitref.ABSENT, cwd=clone)
    assert result.ok, result.detail
    sha, record = gitref.read_remote(remote, REF, cwd=clone)
    assert sha == result.sha
    assert record == {"epoch": 1}


def test_cas_from_absent_is_rejected_when_the_ref_already_exists(remote, clone):
    first = gitref.cas(remote, REF, {"epoch": 1}, expected=gitref.ABSENT, cwd=clone)
    second = gitref.cas(remote, REF, {"epoch": 99}, expected=gitref.ABSENT, cwd=clone)
    assert first.ok
    assert not second.ok
    # nothing landed: the ref is still the first value
    _sha, record = gitref.read_remote(remote, REF, cwd=clone)
    assert record == {"epoch": 1}


def test_cas_from_the_held_value_succeeds(remote, clone):
    first = gitref.cas(remote, REF, {"epoch": 1}, expected=gitref.ABSENT, cwd=clone)
    second = gitref.cas(remote, REF, {"epoch": 2}, expected=first.sha, cwd=clone)
    assert second.ok, second.detail
    _sha, record = gitref.read_remote(remote, REF, cwd=clone)
    assert record == {"epoch": 2}


def test_cas_from_a_stale_value_is_rejected_and_carries_gits_message(remote, clone):
    first = gitref.cas(remote, REF, {"epoch": 1}, expected=gitref.ABSENT, cwd=clone)
    gitref.cas(remote, REF, {"epoch": 2}, expected=first.sha, cwd=clone)  # someone else moved it
    stale = gitref.cas(remote, REF, {"epoch": 5}, expected=first.sha, cwd=clone)
    assert not stale.ok
    assert "rejected" in stale.detail or "stale info" in stale.detail
    _sha, record = gitref.read_remote(remote, REF, cwd=clone)
    assert record == {"epoch": 2}


def test_read_remote_reports_an_absent_ref_as_none(remote, clone):
    assert gitref.read_remote(remote, REF, cwd=clone) == ("", None)


def test_read_remote_works_from_a_clone_that_never_saw_the_object(remote, clone, tmp_path):
    gitref.cas(remote, REF, {"epoch": 7}, expected=gitref.ABSENT, cwd=clone)
    other = tmp_path / "other"
    other.mkdir()
    _git(["init", "-q"], other)
    _sha, record = gitref.read_remote(remote, REF, cwd=other)
    assert record == {"epoch": 7}


def test_read_remote_leaves_no_local_ref_behind(remote, clone):
    """A READ must not install a local ref a later cache read could mistake for our own
    state — the fetch is by explicit refspec with no destination (FETCH_HEAD only)."""
    gitref.cas(remote, REF, {"epoch": 3}, expected=gitref.ABSENT, cwd=clone)
    gitref.read_remote(remote, REF, cwd=clone)
    refs = _git(["for-each-ref", "--format=%(refname)"], clone).stdout
    assert REF not in refs


def test_remote_sha_raises_when_the_remote_is_unreachable(clone, tmp_path):
    with pytest.raises(gitref.RemoteUnreachable):
        gitref.remote_sha(str(tmp_path / "nope.git"), REF, cwd=clone)


# ---- local cache ----------------------------------------------------------------


def test_local_ref_round_trips(remote, clone):
    result = gitref.cas(remote, REF, {"epoch": 4}, expected=gitref.ABSENT, cwd=clone)
    gitref.set_local(REF, result.sha, cwd=clone)
    sha, record = gitref.read_local(REF, cwd=clone)
    assert (sha, record) == (result.sha, {"epoch": 4})
    gitref.delete_local(REF, cwd=clone)
    assert gitref.read_local(REF, cwd=clone) == ("", None)


def test_read_local_reports_an_absent_ref_as_none(clone):
    assert gitref.read_local(REF, cwd=clone) == ("", None)
