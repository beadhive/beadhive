"""``refs/bh/epoch`` — the epoch fence + atomic data push (bh-ytbb.7).

The acceptance bar says the stale-epoch rejection is **verified by test, not by inspection**,
so this file drives real git against scratch bare repos rather than asserting on an argv:

  * ``test_a_stale_epoch_rejects_the_whole_push_and_no_data_lands`` — the core property.
  * ``test_without_atomic_the_same_push_leaks_data`` — the CONTROL that proves the previous
    test is measuring ``--atomic`` and not some unrelated refusal. Without it, a green
    fence test could pass for the wrong reason forever.
  * ``--atomic`` support is probed, and a forge WITHOUT it is simulated for real via
    ``receive.advertiseAtomic=false`` on the scratch remote — an actual non-advertising
    server, not a monkeypatched return value.

Two hosts are modeled as two working clones of one bare "hive remote", all under the test's
own ``tmp_path``: local paths only, never a network remote, never the operator's HQ.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import engine, gitref, host_fence

HOST_A = "aaaaaaaa-1111-4111-8111-111111111111"
HOST_B = "bbbbbbbb-2222-4222-8222-222222222222"


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _clone(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    _git(["commit", "-q", "--allow-empty", "-m", "init"], path)
    return path


@pytest.fixture
def hive_remote(tmp_path):
    """The hive's own remote — where BOTH refs/dolt/data and refs/bh/epoch live."""
    path = tmp_path / "hive.git"
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return str(path)


@pytest.fixture
def host_a(tmp_path):
    return _clone(tmp_path, "host-a")


@pytest.fixture
def host_b(tmp_path):
    return _clone(tmp_path, "host-b")


def _stage_data(cwd, message):
    """Advance this clone's local `refs/dolt/data` to a fresh commit, standing in for a bd
    write. (Real `refs/dolt/data` is Dolt-managed; what matters to the fence is only that it
    is a ref beside the epoch ref, so a commit is a faithful stand-in.)"""
    _git(["commit", "-q", "--allow-empty", "-m", message], cwd)
    sha = _git(["rev-parse", "HEAD"], cwd).stdout.strip()
    _git(["update-ref", host_fence.DATA_REF, sha], cwd)
    return sha


def _remote_data(remote, cwd):
    out = _git(["ls-remote", remote, host_fence.DATA_REF], cwd).stdout.strip()
    return out.split()[0] if out else ""


def _seed(hive_remote, host_a):
    """Host A adopts: installs the epoch fence, then lands a first data push."""
    held = host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    data = _stage_data(host_a, "data-v1")
    outcome = host_fence.fenced_push(hive_remote, held=held, cwd=host_a, atomic=True)
    assert outcome.ok
    return held, data


# ---- the fence ref itself ---------------------------------------------------------


def test_the_fence_lives_outside_the_dolt_data_ref():
    """It must never be reachable by a Dolt merge — a sibling ref, not a row in the DB."""
    assert host_fence.EPOCH_REF == "refs/bh/epoch"
    assert not host_fence.EPOCH_REF.startswith("refs/dolt/")
    assert not host_fence.DATA_REF.startswith(host_fence.EPOCH_REF)
    assert not host_fence.EPOCH_REF.startswith(host_fence.DATA_REF)


def test_the_default_data_ref_matches_the_engines_state_channel(tmp_path):
    """Keeps the fallback default honest against the Engine seam rather than a stale copy."""
    assert host_fence.DATA_REF == engine.BdEngine().state_channel(tmp_path)


def test_installing_a_fence_writes_the_documented_record(hive_remote, host_a):
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=3, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    _sha, fence = host_fence.read_fence(hive_remote, cwd=host_a)
    assert (fence.epoch, fence.host_id, fence.seq) == (3, HOST_A, 0)


def test_installing_a_fence_over_a_moved_ref_is_rejected(hive_remote, host_a, host_b):
    held = host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=2, host_id=HOST_B),
        expected=held,
        cwd=host_b,
    )
    with pytest.raises(host_fence.FenceRejected):
        host_fence.install_fence(
            hive_remote,
            host_fence.EpochFence(epoch=2, host_id=HOST_A),
            expected=held,
            cwd=host_a,
        )


# ---- THE core property ------------------------------------------------------------


def test_a_stale_epoch_rejects_the_whole_push_and_no_data_lands(hive_remote, host_a, host_b):
    """A second host advances the epoch out from under A; A's fenced push must fail with
    ZERO data landing — not a partial application."""
    held_a, data_v1 = _seed(hive_remote, host_a)

    # Host B adopts: CAS the fence to epoch 2. Host A still believes it holds `held_a`.
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=2, host_id=HOST_B),
        expected=held_a,
        cwd=host_b,
    )

    data_v2 = _stage_data(host_a, "data-v2-from-a-stale-primary")
    with pytest.raises(host_fence.FenceRejected) as excinfo:
        host_fence.fenced_push(hive_remote, held=held_a, cwd=host_a, atomic=True)

    assert "NO data landed" in str(excinfo.value)
    landed = _remote_data(hive_remote, host_a)
    assert landed == data_v1, "the stale primary's data must not be on the remote"
    assert landed != data_v2


def test_without_atomic_the_same_push_leaks_data(hive_remote, host_a, host_b):
    """CONTROL for the test above — the reason `--atomic` is in the formulation at all.

    The identical refspec pair, pushed WITHOUT `--atomic`, applies per-ref: the fence is
    rejected and the data lands anyway. This is the leak the fence exists to close; if this
    test ever goes green-by-passing (i.e. the data stops leaking without --atomic), the
    previous test is no longer proving anything and both need re-reading."""
    held_a, data_v1 = _seed(hive_remote, host_a)
    host_fence.install_fence(
        hive_remote,
        host_fence.EpochFence(epoch=2, host_id=HOST_B),
        expected=held_a,
        cwd=host_b,
    )
    data_v2 = _stage_data(host_a, "data-v2-from-a-stale-primary")

    # Raw git, deliberately: the ADR's formulation minus --atomic.
    res = _git(
        [
            "push",
            f"--force-with-lease={host_fence.EPOCH_REF}:{held_a}",
            hive_remote,
            host_fence.DATA_REF,
            host_fence.EPOCH_REF,
        ],
        host_a,
    )
    assert res.returncode != 0  # the fence ref itself was refused
    assert _remote_data(hive_remote, host_a) == data_v2 != data_v1  # ...but the data landed


def test_a_current_epoch_lands_the_data(hive_remote, host_a):
    held, _v1 = _seed(hive_remote, host_a)
    data_v2 = _stage_data(host_a, "data-v2")
    outcome = host_fence.fenced_push(hive_remote, held=held, cwd=host_a, atomic=True)
    assert outcome.ok and outcome.atomic
    assert _remote_data(hive_remote, host_a) == data_v2


# ---- probing --atomic support -------------------------------------------------------


def test_probe_reports_support_on_an_ordinary_remote(hive_remote, host_a):
    assert host_fence.probe_atomic(hive_remote, cwd=host_a) is True


def test_probe_creates_nothing(hive_remote, host_a):
    host_fence.probe_atomic(hive_remote, cwd=host_a)
    refs = _git(["ls-remote", hive_remote], host_a).stdout
    assert "atomic-probe" not in refs


def test_probe_reports_no_support_against_a_real_non_advertising_server(tmp_path, host_a):
    """A REAL server that does not advertise the capability — `receive.advertiseAtomic=false`
    is git's own switch for exactly this, so the probe is exercised end-to-end instead of
    against a monkeypatched return value."""
    old = tmp_path / "old-forge.git"
    _git(["init", "--bare", "-q", str(old)], tmp_path)
    _git(["config", "receive.advertiseAtomic", "false"], old)
    assert host_fence.probe_atomic(str(old), cwd=host_a) is False


def test_probe_raises_rather_than_guessing_when_the_remote_is_unreachable(tmp_path, host_a):
    with pytest.raises(gitref.RemoteUnreachable):
        host_fence.probe_atomic(str(tmp_path / "nope.git"), cwd=host_a)


def test_recorded_forge_defaults_cover_gitea_and_default_unknown_forges_closed():
    assert host_fence.atomic_default("gitea") is True
    assert host_fence.atomic_default("github") is True
    assert host_fence.atomic_default("some-unknown-forge") is False


# ---- where refs/dolt/data actually lives ---------------------------------------------


def test_transport_repos_finds_dolts_hidden_staging_repo(tmp_path):
    """The measured topology (bh-ytbb.7): the hive checkout has NO local refs/dolt/data —
    bd/Dolt stages the push through a hidden bare repo under .beads/embeddeddolt/…, and the
    ADR's push formulation has to run from there."""
    hive = tmp_path / "hive"
    staged = hive / ".beads/embeddeddolt/bh/.dolt/git-remote-cache/deadbeef/repo.git"
    staged.mkdir(parents=True)
    legacy = hive / ".beads/embeddeddolt/beads/.dolt/git-remote-cache/cafe/repo.git"
    legacy.mkdir(parents=True)
    assert host_fence.transport_repos(hive) == sorted([legacy, staged])


def test_transport_repos_is_empty_for_a_hive_with_no_embedded_dolt(tmp_path):
    hive = tmp_path / "nodb-hive"
    (hive / ".beads").mkdir(parents=True)
    assert host_fence.transport_repos(hive) == []
    assert host_fence.transport_repos(tmp_path / "not-a-hive") == []


# ---- the non-atomic fallback ---------------------------------------------------------


def test_the_fallback_still_fences_and_bumps_the_push_counter(tmp_path, host_a):
    """Against a real non-advertising server: the fence is NOT dropped — it is CASed first,
    its `seq` advances, and only then does the data go out."""
    old = tmp_path / "old-forge.git"
    _git(["init", "--bare", "-q", str(old)], tmp_path)
    _git(["config", "receive.advertiseAtomic", "false"], old)

    held = host_fence.install_fence(
        str(old),
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    data = _stage_data(host_a, "data-v1")
    outcome = host_fence.fenced_push(str(old), held=held, cwd=host_a)  # atomic=None -> probes

    assert outcome.ok and outcome.atomic is False
    assert _remote_data(str(old), host_a) == data
    _sha, fence = host_fence.read_fence(str(old), cwd=host_a)
    assert (fence.epoch, fence.seq) == (1, 1)  # epoch STABLE, per-push counter bumped
    assert outcome.held != held  # next push must expect the new fence value


def test_the_fallback_refuses_a_stale_fence_before_touching_the_data(tmp_path, host_a, host_b):
    old = tmp_path / "old-forge.git"
    _git(["init", "--bare", "-q", str(old)], tmp_path)
    _git(["config", "receive.advertiseAtomic", "false"], old)

    held = host_fence.install_fence(
        str(old),
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    data_v1 = _stage_data(host_a, "data-v1")
    host_fence.fenced_push(str(old), held=held, cwd=host_a, atomic=False)

    # host B adopts underneath A
    current, _fence = host_fence.read_fence(str(old), cwd=host_b)
    host_fence.install_fence(
        str(old), host_fence.EpochFence(epoch=2, host_id=HOST_B), expected=current, cwd=host_b
    )

    data_v2 = _stage_data(host_a, "data-v2-from-a-stale-primary")
    with pytest.raises(host_fence.FenceRejected) as excinfo:
        host_fence.fenced_push(str(old), held=held, cwd=host_a, atomic=False)
    assert "no data landed" in str(excinfo.value)
    assert _remote_data(str(old), host_a) == data_v1 != data_v2


def test_the_fallback_refuses_when_no_fence_is_installed_at_all(tmp_path, host_a):
    """It must never invent a fence mid-push — an unfenced hive is an un-adopted one."""
    bare = tmp_path / "empty.git"
    _git(["init", "--bare", "-q", str(bare)], tmp_path)
    _stage_data(host_a, "data-v1")
    with pytest.raises(host_fence.FenceRejected, match="adopt this hive"):
        host_fence.fenced_push(str(bare), held=gitref.ABSENT, cwd=host_a, atomic=False)


def test_an_unsupported_forge_never_silently_drops_the_fence(tmp_path, host_a, host_b):
    """The acceptance bar's negative: absence of --atomic degrades to the documented
    fallback, it does NOT mean an unfenced push. A stale host writing to a non-atomic forge
    is still refused."""
    old = tmp_path / "old-forge.git"
    _git(["init", "--bare", "-q", str(old)], tmp_path)
    _git(["config", "receive.advertiseAtomic", "false"], old)
    held = host_fence.install_fence(
        str(old),
        host_fence.EpochFence(epoch=1, host_id=HOST_A),
        expected=gitref.ABSENT,
        cwd=host_a,
    )
    host_fence.install_fence(
        str(old), host_fence.EpochFence(epoch=2, host_id=HOST_B), expected=held, cwd=host_b
    )
    _stage_data(host_a, "data-from-a-stale-primary")
    with pytest.raises(host_fence.FenceRejected):
        host_fence.fenced_push(str(old), held=held, cwd=host_a)  # probes, gets False
    assert _remote_data(str(old), host_a) == ""  # nothing ever landed
