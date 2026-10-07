"""Key-less trusted-mode publication (bh-l4q0s, epic bh-taa04).

With ``hq.authority_mode: trusted`` (operator settings or ``BH_HQ_AUTHORITY_MODE``) every
operator mutation — grant, observe, lifecycle, bind-beadyard, rebind, release-upgrade apply and
fleet-config publish — runs without ``--operator-key`` and writes the unsigned marker; a key
still signs. Outside trusted mode a key-less mutation refuses with the unchanged error. The Git
receive guard admits unsigned commits only on a server provisioned by a trusted operator.
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from typer.testing import CliRunner

import test_hq_authority_backend as git_hq
from beadhive import frame_release_upgrade, hq_authority_enforce, hq_operator_settings
from beadhive import host_heartbeat as hb
from beadhive import hq_authority_guard as guard
from beadhive.cli import app
from beadhive.hq_control_plane import ControlPlaneError, SqlControlPlane, fingerprint
from beadhive.modules.config.domain.ports import FleetConfigDocument

MODE_ENV = hq_authority_enforce.MODE_ENV

# The real SSH-signed Git HQ, provisioned here by a TRUSTED operator (see _prepared_hqs).
backend, publish, publish_result, apply = (
    git_hq.backend,
    git_hq.publish,
    git_hq.publish_result,
    git_hq.apply,
)


@pytest.fixture(autouse=True)
def _signed_baseline(monkeypatch):
    for name in (MODE_ENV, hq_authority_enforce.ENFORCE_ENV, hq_operator_settings.ENV):
        monkeypatch.delenv(name, raising=False)
    hq_authority_enforce.reset_cache()
    yield
    hq_authority_enforce.reset_cache()


@pytest.fixture(scope="module")
def _prepared_hqs(tmp_path_factory):
    """Override of the backend module's builder: the guard is installed by a trusted operator
    (the grant that provisions the frame is still signed with the key)."""
    prepared = {}

    def get(prefixes, stale):
        if prefixes not in prepared:
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv(MODE_ENV, "trusted")
                prepared[prefixes] = git_hq._PreparedHq(
                    tmp_path_factory.mktemp("trusted-hq"), prefixes, stale
                )
        return prepared[prefixes]

    return get


# ---- the gate and the marker ------------------------------------------------------------------


def test_key_required_only_outside_trusted(monkeypatch):
    assert hq_authority_enforce.key_required("") and hq_authority_enforce.key_required(None)
    assert not hq_authority_enforce.key_required("/k")
    monkeypatch.setenv(MODE_ENV, "trusted")
    assert not hq_authority_enforce.key_required("")


def test_commit_tree_args_sign_with_a_key_else_write_the_unsigned_shape():
    args, message = hq_authority_enforce.commit_tree_args("t", "p", "/k", "subject\n")
    assert args == [
        "-c",
        "gpg.format=ssh",
        "-c",
        "user.signingkey=/k",
        "commit-tree",
        "-S",
        "t",
        "-p",
        "p",
    ]
    assert message == "subject\n"
    args, message = hq_authority_enforce.commit_tree_args("t", "", "", "subject\n")
    assert args == ["commit-tree", "--no-gpg-sign", "t"]
    assert message == f"subject\n\n{hq_authority_enforce.UNSIGNED_TRAILER}\n"


def test_guard_trailer_mirrors_the_package_marker():
    assert guard.UNSIGNED_TRAILER == hq_authority_enforce.UNSIGNED_TRAILER


def test_operator_settings_file_selects_the_publication_mode(monkeypatch, tmp_path):
    settings = tmp_path / "operator.yaml"
    settings.write_text("hq:\n  authority_mode: trusted\n")
    assert hq_authority_enforce.mode() == "signed"
    with hq_authority_enforce.operator_settings(settings):
        assert hq_authority_enforce.mode() == "trusted"
    assert hq_authority_enforce.mode() == "signed"
    with hq_authority_enforce.operator_settings(None):
        assert hq_authority_enforce.mode() == "signed"


def _repo(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input="{}",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "mktree"],
        cwd=repo,
        input=f"100644 blob {blob}\tauthority.json\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    def commit(message):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit-tree",
                "--no-gpg-sign",
                tree,
            ],
            cwd=repo,
            input=message,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    return repo, commit


def test_guard_admits_unsigned_commits_only_when_provisioned_trusted(monkeypatch, tmp_path):
    repo, commit = _repo(tmp_path)
    monkeypatch.chdir(repo)
    marked = commit(f"HQ authority revision 2\n\n{guard.UNSIGNED_TRAILER}\n")
    bare = commit("HQ authority revision 2\n")
    signed_policy = {"operator_signers": str(tmp_path / "none")}
    with pytest.raises(ValueError, match="provisioned for signed authority"):
        guard.verify_operator_commit(marked, signed_policy, "authority")
    trusted_policy = {**signed_policy, "authority_mode": guard.TRUSTED}
    guard.verify_operator_commit(marked, trusted_policy, "authority")
    with pytest.raises(ValueError, match="lacks the trusted-mode trailer"):
        guard.verify_operator_commit(bare, trusted_policy, "configuration")


# ---- the CLI: every authority verb key-less in trusted, unchanged refusal in signed -----------


class FakePlane:
    authority_max_duration_s = None

    def __init__(self):
        self.calls = []

    def _seen(self, verb, operator_key):
        self.calls.append((verb, operator_key, hq_authority_enforce.mode()))
        return "rev"

    def grant(self, authority, public_key, desired, *, expected, operator_key):
        return self._seen("grant", operator_key)

    def accept_observation(self, frame, *, expected, operator_key, holder_identity=""):
        return self._seen("observe", operator_key)

    def renew(self, *, expected, operator_key, duration=None, ceiling=None):
        return self._seen("rebind", operator_key)

    def bind_beadyard(self, *, expected, operator_key):
        return self._seen("bind-beadyard", operator_key)


@pytest.fixture
def plane(monkeypatch):
    fake = FakePlane()
    monkeypatch.setattr(hq_operator_settings, "select_plane", lambda _settings=None: fake)
    return fake


def _record(tmp_path):
    record = tmp_path / "grant.json"
    authority = {
        "frame_id": "frame-1",
        "holder_identity": "host-1",
        "instance_ref": "vm-1",
        "key_fingerprint": "SHA256:x",
        "epoch": 1,
        "audience": "fleet",
        "config_revision": "config",
        "candidate_expires_at": time.time() + 60,
    }
    record.write_text(json.dumps({"authority": authority, "public_key": "k", "desired": {}}))
    return record


VERBS = ("grant", "observe", "rebind", "bind-beadyard")


def _invoke(verb, tmp_path, *extra):
    args = ["hq", "authority", verb, "--confirm", "--expected-revision", "r", *extra]
    if verb == "grant":
        args += ["--record", str(_record(tmp_path))]
    if verb == "observe":
        args += ["--frame", "frame-1"]
    return CliRunner().invoke(app, args)


@pytest.mark.parametrize("verb", VERBS)
def test_cli_keyless_refuses_outside_trusted_unchanged(plane, tmp_path, verb):
    result = _invoke(verb, tmp_path)
    assert result.exit_code == 1
    assert "mutation requires separate --operator-key" in result.output
    assert plane.calls == []


@pytest.mark.parametrize("verb", VERBS)
def test_cli_keyless_publishes_in_trusted(plane, tmp_path, monkeypatch, verb):
    monkeypatch.setenv(MODE_ENV, "trusted")
    result = _invoke(verb, tmp_path)
    assert result.exit_code == 0, result.output
    assert plane.calls == [(verb, "", "trusted")]
    # A supplied key still signs.
    assert _invoke(verb, tmp_path, "--operator-key", str(tmp_path / "k")).exit_code == 0
    assert plane.calls[-1][1] == str(tmp_path / "k")


def test_cli_operator_settings_file_enables_keyless(plane, tmp_path):
    settings = tmp_path / "operator.yaml"
    settings.write_text("hq:\n  authority_mode: trusted\n")
    result = _invoke("rebind", tmp_path, "--operator-settings", str(settings))
    assert result.exit_code == 0, result.output
    assert plane.calls == [("rebind", "", "trusted")]
    assert hq_authority_enforce.mode() == "signed"  # scoped to the one command


# ---- release-upgrade apply: the SQL publish path -------------------------------------------


class _Operator:
    def __init__(self):
        self.published = []

    def load(self, *, deadline=None):
        return "head", {"frames": {}}, None, {}

    def publish(self, state, **kwargs):
        self.published.append(kwargs["operator_key"])
        return "new-head"


def _upgrade_plane(monkeypatch):
    operator = _Operator()
    plane = SqlControlPlane.__new__(SqlControlPlane)
    plane._operator = lambda: operator
    plane._operator_deadline = lambda: time.monotonic() + 30
    plane.clock = time.time
    snapshot = type("Snapshot", (), {"commit_revision": "config-head"})()
    plane.config_store = lambda: type("Store", (), {"load_snapshot": lambda self: snapshot})()
    rotated = {"authority": {"frame_id": "frame-1"}, "state": "active"}
    state = {"frames": {"frame-1": {"candidate": None, "active": rotated}}}
    monkeypatch.setattr(frame_release_upgrade, "prepare", lambda *a, **k: (state, "digest"))
    monkeypatch.setattr(
        frame_release_upgrade, "route_for", lambda *_: ("p" * 24, "frame-1", "", "", 2, "")
    )
    request = {"epoch": 1, "release": {}, "profile": "p"}
    return plane, operator, request


def test_release_upgrade_apply_keyless_only_in_trusted(monkeypatch):
    plane, operator, request = _upgrade_plane(monkeypatch)

    def apply_upgrade(key):
        return frame_release_upgrade.release_upgrade(
            plane,
            "frame-1",
            "apply",
            request=request,
            plan_sha256="digest",
            operator_key=key,
            confirm=True,
        )

    with pytest.raises(ValueError, match="approved operator key"):
        apply_upgrade("")
    assert operator.published == []
    monkeypatch.setenv(MODE_ENV, "trusted")
    assert apply_upgrade("")["revision"] == "new-head"
    assert apply_upgrade("/k")["revision"] == "new-head"
    assert operator.published == ["", "/k"]


# ---- Git HQ, end to end ----------------------------------------------------------------------


def _header(b, sha):
    return git_hq.git(b["remote"], "cat-file", "commit", sha)


def _unsigned(b, sha):
    text = _header(b, sha)
    header, _, message = text.partition("\n\n")
    return hq_authority_enforce.unsigned_commit(header) and (
        hq_authority_enforce.UNSIGNED_TRAILER in message
    )


def test_git_server_provisioned_trusted_pins_the_mode_in_its_policy(backend):
    policy = json.loads((backend["remote"] / guard.POLICY).read_text())
    assert policy["authority_mode"] == guard.TRUSTED


def test_git_keyless_operator_commands_trusted_accepts_signed_refuses(backend, monkeypatch):
    b = backend
    plane = b["plane"]
    for seq in (1, 2):
        b["accept"](seq)  # signed observations while the signed sandboxed frame beats
    publish(b, b["lease"](3))

    # Outside trusted mode a key-less mutation refuses before any write.
    with pytest.raises(ControlPlaneError, match="requires separate --operator-key"):
        plane.accept_observation("frame-one", expected=plane._read()[0], operator_key="")
    with pytest.raises(ControlPlaneError, match="separate operator key"):
        apply(b, "admit", operator_key="")

    monkeypatch.setenv(MODE_ENV, "trusted")
    observed = plane.accept_observation("frame-one", expected=plane._read()[0], operator_key="")
    assert _unsigned(b, observed)
    apply(b, "admit", operator_key="")
    apply(b, "cordon", operator_key="")
    _revision, desired, _ = plane.read_eligibility(b["manifest"])
    assert desired["state"] == "active" and desired["cordoned"] is True
    apply(b, "resume", operator_key="")
    rebound = plane.renew(expected=plane._read()[0], operator_key="")
    assert _unsigned(b, rebound)
    other = git_hq.key(b["tmp"] / "second-runtime")
    granted = plane.grant(
        hb.ObservationAuthority(
            "frame-two",
            "host-two",
            "vm-two",
            fingerprint(other),
            1,
            "fleet-one",
            "config-one",
            time.time() + 3600,
        ),
        other,
        b["desired"],
        expected=rebound,
        operator_key="",
    )
    assert _unsigned(b, granted)

    # A trusted frame reads and honours the unsigned record.
    revision, desired, _ = plane.read_eligibility(b["manifest"])
    assert revision == granted and desired["state"] == "active" and not desired["cordoned"]

    # A signed frame refuses it fail-closed with the actionable message — in-process and in
    # the real sandboxed frame process.
    monkeypatch.delenv(MODE_ENV)
    with pytest.raises(ControlPlaneError, match="UNSIGNED.*hq.authority_mode: trusted"):
        plane.read_eligibility(b["manifest"])
    refused = publish_result(b, b["lease"](4, state_seen="active"))
    assert refused.returncode != 0 and "UNSIGNED" in refused.stderr

    # Switching back: a trusted operator rebinds WITH the key; signed frames are eligible.
    monkeypatch.setenv(MODE_ENV, "trusted")
    signed = plane.renew(expected=granted, operator_key=str(b["operator"]))
    assert not _unsigned(b, signed)
    monkeypatch.delenv(MODE_ENV)
    revision, desired, _ = plane.read_eligibility(b["manifest"])
    assert revision == signed and desired["state"] == "active" and not desired["cordoned"]
    publish(b, b["lease"](4, state_seen="active"))


def test_git_keyless_fleet_config_publish(backend, monkeypatch):
    b = backend
    documents = (FleetConfigDocument("fleet.yaml", "schema_version: 1\nmanaged_repos: []\n"),)
    store = b["plane"].config_store()
    with pytest.raises(ValueError, match="requires operator key"):
        store.publish_snapshot(documents, expected_revision="")
    monkeypatch.setenv(MODE_ENV, "trusted")
    published = store.publish_snapshot(documents, expected_revision="")
    assert published.documents == documents
    assert _unsigned(b, published.commit_revision)
    monkeypatch.delenv(MODE_ENV)
    with pytest.raises(ValueError, match="UNSIGNED"):
        store.load_snapshot()
    monkeypatch.setenv(MODE_ENV, "trusted")
    signed = (
        b["plane"]
        .config_store(operator_key=str(b["operator"]))
        .publish_snapshot(documents, expected_revision=published.commit_revision)
    )
    monkeypatch.delenv(MODE_ENV)
    assert store.load_snapshot().commit_revision == signed.commit_revision
