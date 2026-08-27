"""bh-ijd4 — the two halves of a host git identity, and the promise never to overwrite one.

The measured gap these tests pin down (2026-08-05, origin Mac vs a freshly provisioned Linux
host): the VM passed all ten provisioning steps with an entirely empty global git config, while
``~/.ssh/id_ed25519`` — created for ``gh`` — sat there unknown to git. Supervised mode "inherits
the human's config"; on a host with no human there is nothing to inherit.

Isolation: the autouse ``_sandbox_global_git_config`` fixture (conftest) points
``GIT_CONFIG_GLOBAL`` at a scratch file with no identity, so every ``git config --global`` read
and write below is against that file and never the operator's own ``~/.gitconfig``.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import config, git_identity, host

# A real, syntactically valid ed25519 public key — generated once for these tests, never used to
# sign anything. Only PUBLIC material appears anywhere in this feature.
PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAVbRXj7OM9Mi56/SFeV6BZpZ513BTG4+9xh3T5jFLD+ test@example"
)


def _global(key: str) -> str:
    res = subprocess.run(
        ["git", "config", "--global", "--get", key], capture_output=True, text=True, check=False
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _set_global(key: str, value: str) -> None:
    subprocess.run(["git", "config", "--global", key, value], check=True)


def _fleet_identity(name="Fleet Op", email="op@example.com") -> None:
    """Put the operator's name/email where the chain says they live: fleet.yaml, which only
    exists on a host once HQ has been cloned."""
    config.hq_dir().mkdir(parents=True, exist_ok=True)
    config.fleet_path().write_text(
        'schema_version: 1\ndelimiter: ":"\nmanaged_repos: []\n'
        f"work:\n  identity:\n    name: {name}\n    email: {email}\n"
    )
    # What `bh hq clone` does right after landing a real fleet.yaml: drop the host config's now
    # stale fleet-shaped leaves, which would otherwise make every later load a ConfigError.
    config.reconcile_host_after_fleet()


@pytest.fixture
def host_key(tmp_path, monkeypatch):
    """A host whose ``host.yaml`` already names a signing key — the per-host half, settled."""
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text(PUBKEY + "\n")
    monkeypatch.setattr(host, "discover_signing_key", lambda: str(pub))
    return pub


# ---- the non-negotiable: a human's config is never overwritten -----------------


def test_an_existing_global_identity_is_kept_verbatim(host_key):
    """The origin Mac. Every key already set — bh must come out of this having changed
    NOTHING, and must say so rather than claiming it configured the host."""
    _set_global("user.name", "Brian Cripe")
    _set_global("user.email", "brian@xenophon.dev")
    _set_global("user.signingkey", "/Users/brian/.ssh/id_ed25519.pub")
    _set_global("gpg.format", "ssh")
    _set_global("commit.gpgsign", "true")
    _fleet_identity()

    fills = git_identity.establish()

    assert _global("user.name") == "Brian Cripe"  # NOT "Fleet Op"
    assert _global("user.email") == "brian@xenophon.dev"
    assert _global("user.signingkey") == "/Users/brian/.ssh/id_ed25519.pub"
    by_key = {f.key: f for f in fills}
    for key in ("user.name", "user.email", "user.signingkey", "gpg.format", "commit.gpgsign"):
        assert by_key[key].action == git_identity.KEPT, key


def test_a_bare_host_gets_the_configured_identity(host_key):
    """The VM. Nothing set, so every gap fills — from bh's config, and from the key the host
    already had."""
    _fleet_identity()

    git_identity.establish()

    assert _global("user.name") == "Fleet Op"
    assert _global("user.email") == "op@example.com"
    assert _global("user.signingkey") == str(host_key)
    assert _global("gpg.format") == "ssh"
    assert _global("commit.gpgsign") == "true"


def test_establish_is_idempotent(host_key):
    _fleet_identity()
    git_identity.establish()

    second = git_identity.establish()

    assert all(f.action == git_identity.KEPT for f in second), second


def test_dry_run_writes_nothing(host_key):
    _fleet_identity()

    fills = git_identity.establish(dry_run=True)

    assert _global("user.name") == ""
    assert _global("user.email") == ""
    assert any(f.action == git_identity.WOULD for f in fills)


# ---- never invent an identity ---------------------------------------------------


def test_identity_is_never_invented_from_the_environment(host_key, monkeypatch):
    """No config -> no name/email. Not $USER, not the hostname, not `gh`. A wrong author on a
    signed commit is worse than no commit, so the honest outcome is 'unresolved'."""
    monkeypatch.setenv("USER", "someone-else")

    fills = git_identity.establish()

    assert _global("user.name") == ""
    assert _global("user.email") == ""
    by_key = {f.key: f for f in fills}
    assert by_key["user.name"].action == git_identity.UNRESOLVED
    assert by_key["user.email"].action == git_identity.UNRESOLVED


def test_summary_fails_and_says_where_the_values_should_come_from(host_key):
    ok, detail = git_identity.summary()

    assert not ok
    assert "user.name" in detail and "user.email" in detail
    assert "fleet.yaml" in detail


# ---- the per-host half: host.yaml, minted once, never rewritten -----------------


def test_mint_records_the_key_git_already_names():
    """Discovery ADOPTS before it probes: a host that already told git what it signs with can
    only ever be agreed with, never contradicted."""
    _set_global("user.signingkey", "/somewhere/custom_key.pub")

    assert host.discover_signing_key() == "/somewhere/custom_key.pub"


def test_discovery_falls_back_to_the_key_the_host_already_has(tmp_path, monkeypatch):
    """The VM's ``~/.ssh/id_ed25519`` — created for ``gh``, unknown to git."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519.pub").write_text(PUBKEY + "\n")
    monkeypatch.setattr(host.Path, "home", staticmethod(lambda: tmp_path))

    assert host.discover_signing_key() == str(ssh / "id_ed25519.pub")


def test_no_key_anywhere_is_empty_not_invented(tmp_path, monkeypatch):
    monkeypatch.setattr(host.Path, "home", staticmethod(lambda: tmp_path))

    assert host.discover_signing_key() == ""


def test_a_recorded_signing_key_is_never_rewritten(host_key, monkeypatch):
    """``host.mint_if_needed``'s never-regenerate contract, extended to the second identity
    field: once a host declares which key it signs with, only an operator changes it."""
    host.mint_if_needed()
    assert host.signing_key() == str(host_key)

    monkeypatch.setattr(host, "discover_signing_key", lambda: "/a/different/key.pub")

    assert host.ensure_signing_key() == str(host_key)
    assert host.signing_key() == str(host_key)


def test_the_key_is_backfilled_into_a_host_yaml_that_predates_the_field(host_key):
    """A host minted before this feature has no ``signing_key`` line — back-fill it without
    disturbing ``host_id``, which is the value the whole fencing model keys off."""
    host.path().parent.mkdir(parents=True, exist_ok=True)
    host.path().write_text("host_id: 11111111-2222-3333-4444-555555555555\nlabel: oldhost\n")

    key = host.ensure_signing_key()

    assert key == str(host_key)
    assert host.host_id() == "11111111-2222-3333-4444-555555555555"
    assert host.label() == "oldhost"


# ---- allowed_signers: the source, and what it makes possible --------------------


def test_enrollment_publishes_only_public_material(host_key):
    _fleet_identity()
    config.hq_dir().mkdir(parents=True, exist_ok=True)

    git_identity.establish()

    listed = git_identity.allowed_signers_path().read_text()
    assert "op@example.com " + PUBKEY in listed
    assert "PRIVATE KEY" not in listed
    assert _global("gpg.ssh.allowedsignersfile") == str(git_identity.allowed_signers_path())


def test_enrollment_does_not_duplicate_an_already_listed_key(host_key):
    _fleet_identity()
    git_identity.establish()

    git_identity.establish()

    lines = [
        ln
        for ln in git_identity.allowed_signers_path().read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert len(lines) == 1


def test_enrollment_is_unresolved_without_hq(host_key):
    """No HQ store -> nowhere fleet-wide to publish a trusted key. Reported, not faked."""
    fill = git_identity.enroll_signer("op@example.com", str(host_key))

    assert fill.action == git_identity.UNRESOLVED
    assert "HQ" in fill.detail


def test_a_private_key_path_is_never_read_as_public_material(tmp_path):
    priv = tmp_path / "id_ed25519"
    priv.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnope\n")

    assert git_identity.public_key_material(str(priv)) == ""


def test_signing_summary_refuses_a_configured_but_unenrolled_key(host_key):
    """The distinction that makes verification REAL rather than presence-only: a key git will
    sign with, that nobody trusts, verifies as U — and U is a refusal."""
    _fleet_identity()
    git_identity.establish()
    git_identity.allowed_signers_path().write_text("# nobody enrolled\n")

    ok, detail = git_identity.signing_summary()

    assert not ok
    assert "not enrolled" in detail


def test_signing_summary_passes_once_the_key_is_enrolled(host_key):
    _fleet_identity()
    git_identity.establish()

    ok, detail = git_identity.signing_summary()

    assert ok, detail
    assert "verify as G" in detail


# ---- end to end: the acceptance criterion, as a commit --------------------------


def test_a_commit_on_a_bare_host_is_attributed_and_verifiably_signed(tmp_path, monkeypatch):
    """THE bead's headline acceptance criterion, exercised as an actual commit.

    Start from the measured VM: an empty global git config and one SSH key the host already
    has. Run the marrying step. Then make an ordinary commit — no bh worktree, no stamping,
    nothing but the global config the step wrote — and assert it carries the OPERATOR's name
    and email and verifies as ``G`` against the fleet's ``allowed_signers``.

    ``G`` (not merely "a signature exists") is the whole point: it is the only verdict the
    merge gate accepts, so this proves the identity half and the enforcement half actually
    meet."""
    key = tmp_path / "hostkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "vm", "-f", str(key), "-q"], check=True
    )
    monkeypatch.setattr(host, "discover_signing_key", lambda: str(key.with_suffix(".pub")))
    _fleet_identity()
    assert _global("user.name") == ""  # the VM's measured starting state

    git_identity.establish()

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["commit", "-q", "--allow-empty", "-m", "feat: made on a bare host"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    shown = subprocess.run(
        ["git", "log", "-1", "--format=%an|%ae|%G?"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert shown == "Fleet Op|op@example.com|G"
