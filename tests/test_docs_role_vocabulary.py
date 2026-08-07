"""Docs must not tell a reader to give a machine a role that cannot do the job (bh-6rmpy).

v0.8.0 shipped `docs/ONBOARDING.md` describing the `worker` role as the one that "takes primary
for particular repos and holds their leases, executing work. What you want for an added
machine." Exactly inverted: that role is the one role which is NEVER primary, so a reader
following the section provisioned a machine that refused the first `bh work claim` it was given.

The last acceptance bullet is the reason this file exists: "whatever guards this in future does
not rely on prose review alone, since prose review is what missed it." So these assert docs
against the CODE — `hosts.HOST_ROLES` and the `ttl_for_role` guard — not against a fixed string
a future rename would silently invalidate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from beadhive import host_lease, hosts

DOCS = Path(__file__).resolve().parents[1]
# The reader-facing docs. Dated design records (0.7.0-release-readiness.md) are deliberately
# excluded: they describe what a PAST release was, and rewriting history to match a later
# rename would make them lie about it.
GUIDES = [DOCS / "docs" / "ONBOARDING.md", DOCS / "docs" / "UPGRADING.md", DOCS / "INSTALL.md"]


def _refuses(role: str) -> bool:
    """Whether `ttl_for_role` refuses this role outright — i.e. it can never hold a lease, and
    so can never claim, submit or merge. Derived from the guard rather than hardcoded, so a
    later change to WHICH role is never-primary re-points these tests automatically."""
    try:
        host_lease.ttl_for_role(role)
    except host_lease.HostLeaseRejected:
        return True
    return False


def _never_primary() -> list[str]:
    return sorted(r for r in hosts.HOST_ROLES if _refuses(r))


def _provision_roles(text: str) -> list[str]:
    """Every role a doc tells the reader to pass to `host provision` / `host init`."""
    return re.findall(r"host (?:provision|init)[^\n`]*--role\s+([a-z-]+)", text)


@pytest.mark.parametrize("doc", GUIDES, ids=lambda p: p.name)
def test_no_guide_tells_a_reader_to_provision_a_never_primary_role(doc):
    if not doc.is_file():
        pytest.skip(f"{doc.name} not present")
    refused = _never_primary()
    offenders = [r for r in _provision_roles(doc.read_text()) if r in refused]
    assert not offenders, (
        f"{doc.name} instructs `--role {offenders[0]}`, but that role can never hold a lease "
        f"(host_lease.ttl_for_role refuses it) — the machine will refuse the first "
        f"`bh work claim` it is given. This is the bh-6rmpy inversion."
    )


@pytest.mark.parametrize("doc", GUIDES, ids=lambda p: p.name)
def test_every_role_a_guide_names_is_real(doc):
    """A doc naming a role that no longer exists is the other half of the same failure — the
    reader types it and `host init` refuses with a list that does not contain their word."""
    if not doc.is_file():
        pytest.skip(f"{doc.name} not present")
    known = set(hosts.HOST_ROLES) | set(hosts.DEPRECATED_ROLE_ALIASES)
    unknown = [r for r in _provision_roles(doc.read_text()) if r not in known]
    assert not unknown, f"{doc.name} names role(s) that do not exist: {unknown}"


def test_the_never_primary_role_is_not_described_as_executing_work():
    """The specific v0.8.0 sentence, generalised: the never-primary role must not be introduced
    with an executing verb. Checked on the line that DEFINES it, so ordinary prose mentioning
    the word elsewhere is not a false positive."""
    role = _never_primary()[0]
    for line in (DOCS / "docs" / "ONBOARDING.md").read_text().splitlines():
        if not line.strip().startswith(f"- **`{role}`**"):
            continue
        assert "never primary" in line.lower(), (
            f"the line defining `{role}` must say it is never primary, first: {line!r}"
        )
        return
    pytest.fail(f"ONBOARDING.md's role vocabulary list no longer defines `{role}`")
