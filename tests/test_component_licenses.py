"""Every component the image pins must carry a redistributable licence (bh-pc2a.21).

The durable risk was never today's image — it was that nothing stopped a copyleft or proprietary
tool being added to docker-bake.hcl. bh-pc2a.36 proved that is not hypothetical: a proprietary
harness was baked in and only found by reading the built image's package metadata.

So the pins are declared in docker-bake.hcl's licence-policy block, and this file makes the
declaration BINDING: a new pin with no declared licence fails, and a declared licence outside the
allowed set fails. Adding a component now requires a reviewed decision rather than arriving by
accident.

SCOPE, restated here because it is easy to over-read: this governs the components WE PIN. The
Debian base layer carries hundreds of GPL/LGPL packages, as any Debian-derived image does — those
are separate programs invoked as programs, governed by redistribution obligations rather than by
this allowlist, and docs/ASSURANCE.md scopes them explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAKE = (ROOT / "docker-bake.hcl").read_text()

# Permissive plus public-domain-equivalent. Deliberately NOT the justfile's `license_allow`, which
# governs Python dependencies linked into the wheel; these are standalone binaries redistributed
# alongside it. Same intent, different exposure.
ALLOWED = frozenset(
    {
        "MIT",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "PSF-2.0",
        "CC0-1.0",
        "Artistic-2.0",
    }
)

# Version variables that do NOT name a shipped component. Each exemption is a decision, so each
# gets a reason — an unexplained entry here would quietly reopen the hole this guard closes.
#
#   BEADHIVE_WHEEL     selects a local build of this project; not a third-party component.
#   CLAUDE_CODE        NOT SHIPPED (bh-pc2a.36). The pin survives only as the version
#                      `bh harness install` defaults to, so the image still names ONE validated
#                      version of something the USER installs. Nothing proprietary is
#                      redistributed, which is precisely why it is exempt from the allowlist —
#                      and why it must stay absent from the Dockerfile's npm install, asserted
#                      separately in tests/test_dependency_policy.py.
#   CODEX              NOT SHIPPED (bh-lnrn) — and the reason DIFFERS from claude's, which is why
#                      it gets its own entry instead of joining that one. Codex is Apache-2.0: it
#                      PASSES this allowlist, and passing is why it was shipped in the first
#                      place. It is excluded by DECISION, not by licence — the image ships the
#                      runtime and the means, never the harness. Reading this exemption as
#                      "another proprietary tool" would get the next harness wrong. The pin
#                      survives only as BH_CODEX_VERSION, the version `bh dep install`
#                      bootstraps to.
#   NIX                BUILDER STAGE ONLY (bh-8b8o.1). nixos/nix builds the toolchain closure and
#                      is discarded; none of its bits reach the shipped image, so its own licence
#                      (LGPL-2.1, copyleft, not in ALLOWED) governs nothing we redistribute. The
#                      retired `rust` row described the same category in PROSE while still being
#                      treated as shipped — it only passed because rust happens to be permissive.
#                      Generalising builder-only vs shipped belongs to bh-8b8o.2, which replaces
#                      this whole hand-maintained block; this is the narrow, honest entry until
#                      then. What DOES reach the image is the closure, governed there.
_NOT_A_COMPONENT = frozenset({"BEADHIVE_WHEEL", "CLAUDE_CODE", "CODEX", "NIX"})


def _pinned_components() -> set[str]:
    """Components pinned in docker-bake.hcl, normalized to the names used in the policy block.

    `*_VERSION` plus the `*_TAG` pins that name a base image (python), which are components
    even though they
    name a base image rather than a release asset.
    """
    names = set(re.findall(r'^variable "([A-Z0-9_]+)_(?:VERSION|TAG)"', BAKE, re.M))
    return {n.lower() for n in names - _NOT_A_COMPONENT}


def _declared_licenses() -> dict[str, str]:
    """The `#   <component>  <spdx>  <source>` rows of the licence-policy block."""
    table = BAKE.split("component       licence       source of the declaration", 1)
    assert len(table) == 2, "the licence-policy block in docker-bake.hcl has moved or changed"
    rows = {}
    for line in table[1].splitlines():
        m = re.match(r"^#\s{3}(\w+)\s{2,}([A-Za-z0-9.\-]+)\s{2,}\S", line)
        if m:
            rows[m[1]] = m[2]
        if line.startswith("# OVERRIDE"):
            break
    return rows


def test_every_pinned_component_declares_a_licence():
    """A new pin with no declared licence must FAIL — that is the whole guard.

    Without this, adding a tool to docker-bake.hcl is a one-line change with no licence decision
    attached, which is exactly how the proprietary harness got in (bh-pc2a.36).
    """
    undeclared = _pinned_components() - set(_declared_licenses())

    assert not undeclared, (
        f"pinned in docker-bake.hcl but absent from its licence-policy block: "
        f"{sorted(undeclared)}. Declare each component's licence there — redistributing a "
        "component is a decision, not a detail."
    )


def test_every_declared_licence_is_redistributable():
    """Copyleft, proprietary, or anything without an SPDX id must fail."""
    offenders = {c: lic for c, lic in _declared_licenses().items() if lic not in ALLOWED}

    assert not offenders, (
        f"component(s) declared with a licence outside the allowed set: {offenders}. "
        f"Allowed: {sorted(ALLOWED)}. Copyleft and proprietary components are USER-BROUGHT — see "
        "`bh harness install` for the pattern."
    )


def test_the_policy_block_does_not_drift_into_emptiness():
    """A parser that silently matches nothing would make both tests above pass vacuously.

    This is the failure mode that bit the licence gate itself (bh-vf8h.3): an allowlist that
    matches no packages permits everything while looking green.
    """
    declared = _declared_licenses()

    assert len(declared) >= 10, f"only parsed {len(declared)} declarations — the block format moved"
    # The canary must name a component the image actually SHIPS. It was `codex` until bh-lnrn
    # de-baked it; had it been deleted rather than repointed, this guard would have gone with it
    # and both tests above would pass vacuously the next time the row format moved — which is the
    # precise failure (bh-vf8h.3) this test exists to catch, reintroduced by its own cleanup.
    assert "dolt" in declared, "expected a known component to parse; the row format changed"


def test_no_proprietary_marker_survives_in_the_pins():
    """bh-pc2a.36 removed the one proprietary component. Nothing should reintroduce a non-SPDX
    marker like "SEE LICENSE IN" as a declared licence."""
    for component, lic in _declared_licenses().items():
        assert "SEE" not in lic.upper(), f"{component} declares a non-SPDX licence: {lic!r}"
