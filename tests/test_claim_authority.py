"""`beadhive.claim_authority` self-checks — the Tier 0 `LocalTrustAuthority` and the registry seam
(bh-ejlq). Real git in tmp_path (issue/read round-trips through worktree-scoped git config) plus
pure verify()/registry assertions. AAA structure.
"""

from __future__ import annotations

import pytest

from beadhive import claim_authority
from beadhive.claim_authority import ClaimRecord


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo standing in for a bead worktree."""
    from beadhive.run import run

    run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture
def clean_registry():
    """Snapshot the authority registry and restore it, so a test that registers a stub authority
    doesn't leak the name into other tests."""
    snapshot = dict(claim_authority._AUTHORITIES)
    yield claim_authority
    claim_authority._AUTHORITIES.clear()
    claim_authority._AUTHORITIES.update(snapshot)


def test_read_returns_none_before_any_issue(repo):
    # Arrange: a fresh worktree with no claim ever issued.
    authority = claim_authority.get_authority("local")

    # Act
    record = authority.read(repo)

    # Assert
    assert record is None


def test_issue_then_read_round_trips_the_seat(repo):
    # Arrange
    authority = claim_authority.get_authority("local")

    # Act
    issued = authority.issue("bh-ejlq", "dev/alice", repo)
    back = authority.read(repo)

    # Assert: read() sees exactly what issue() minted.
    assert issued.bead == "bh-ejlq"
    assert issued.seat == "dev/alice"
    assert issued.attestation == "none"
    assert back == issued


def test_issue_overwrites_a_prior_record_for_the_same_worktree(repo):
    # Arrange: an earlier claim on this worktree (e.g. a prior bead cycled through it).
    authority = claim_authority.get_authority("local")
    authority.issue("bh-old", "dev/alice", repo)

    # Act: a fresh claim/resume re-issues.
    authority.issue("bh-ejlq", "dev/bob", repo)
    back = authority.read(repo)

    # Assert: the new record wins outright — no stale bead/seat leaks through.
    assert back.bead == "bh-ejlq"
    assert back.seat == "dev/bob"


def test_verify_defaults_empty_seat_to_the_recorded_holder(repo):
    # Arrange: submit's no-`--as` path passes an empty seat, meaning "trust the record".
    authority = claim_authority.get_authority("local")
    record = authority.issue("bh-ejlq", "dev/alice", repo)

    # Act / Assert
    assert authority.verify(record, "submit", "") is True


def test_verify_matches_an_explicit_seat_against_the_record():
    # Arrange
    authority = claim_authority.get_authority("local")
    record = ClaimRecord(bead="bh-ejlq", seat="dev/alice", worktree="/tmp/x", issued_at="")

    # Act / Assert: matching explicit seat verifies, a mismatched one is refused.
    assert authority.verify(record, "submit", "dev/alice") is True
    assert authority.verify(record, "submit", "dev/mallory") is False


def test_verify_refuses_when_no_record_exists():
    # Arrange / Act / Assert: nothing to trust ⇒ never verified, explicit or not.
    authority = claim_authority.get_authority("local")
    assert authority.verify(None, "submit", "") is False
    assert authority.verify(None, "submit", "dev/alice") is False


def test_stub_authority_swapped_via_name_is_honored(clean_registry):
    # Arrange: a stub authority proving selection is by name through the registry, same shape as
    # conflict_estimator's plugin seam.
    class StubAuthority:
        def issue(self, bead, seat, worktree):
            return ClaimRecord(bead=bead, seat=seat, worktree=str(worktree), issued_at="")

        def read(self, worktree):
            return None

        def verify(self, record, action, seat):
            return True  # sentinel: always verifies

    clean_registry.register_authority("stub-signed", StubAuthority())

    # Act
    resolved = clean_registry.get_authority("stub-signed")

    # Assert: the stub came back, not the local floor, and is listed as available.
    assert resolved.verify(None, "submit", "dev/mallory") is True
    assert "stub-signed" in clean_registry.available_authorities()


def test_unknown_authority_raises_valueerror_listing_available():
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="unknown claim authority 'signed-token'"):
        claim_authority.get_authority("signed-token")
    with pytest.raises(ValueError, match="local"):
        claim_authority.get_authority("signed-token")


def test_default_authority_matches_config_default():
    # Arrange: the config default (work.identity.authority) must resolve through the registry.
    from beadhive import config

    # Act
    configured = config.claim_authority({}, {})

    # Assert
    assert configured == claim_authority.DEFAULT_AUTHORITY
    assert configured in claim_authority.available_authorities()
