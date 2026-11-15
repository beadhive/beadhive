"""Contract: bd's gate-refusal wording still matches the gate-free fallback's marker (bh-myjbi).

``replica_bootstrap.gate_refused`` is a substring match on bd's own refusal text, because bd
offers no structured signal: ``cmd/bd/bootstrap.go`` returns a plain ``fmt.Errorf("bd bootstrap
refuses to run over live bd activity on this workspace: %w", gateErr)``, which exits with the
generic status 1 like every other bd error (no dedicated exit code or typed error to switch to).
If a bd release rewords it, the busy-executor fallback would silently stop triggering (fails
safe, but onboarding on a busy executor regresses unnoticed).

The pin is tied to the supported bd range the repo already declares for the trigger canary
(``harness.trigger_canary.DEFAULT_RANGES["bd"]``, enforced against ``flake.nix`` by
``test_fence_trigger_canary_pin``). Hermetic: the string is read out of the installed bd
binary's bytes (Go string literals are embedded verbatim) — no bd command that touches a
workspace, no server, no sleeps. Skips when bd is absent or outside the supported range.
"""

from __future__ import annotations

import mmap
import shutil

import pytest
from packaging.specifiers import SpecifierSet

from beadhive import replica_bootstrap
from harness import trigger_canary as tc

#: The full bd-1.x refusal as bd formats it (the ``%w`` tail is the gate error, varies).
BD_REFUSAL_PREFIX = "bd bootstrap refuses to run over live bd activity on this workspace"


def test_marker_is_a_substring_of_the_pinned_bd_refusal_prefix():
    assert replica_bootstrap.GATE_BUSY_MARKER in BD_REFUSAL_PREFIX


def test_gate_refused_matches_the_pinned_wording_and_nothing_else():
    class R:
        returncode = 1
        stdout = ""
        stderr = f"Error: {BD_REFUSAL_PREFIX}: workspace gate busy\n"

    assert replica_bootstrap.gate_refused(R())
    R.stderr = "Error: some other bootstrap failure\n"
    assert not replica_bootstrap.gate_refused(R())


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed")
def test_supported_bd_binary_still_emits_the_refusal_wording():
    version = tc.installed_versions()["bd"]
    spec = SpecifierSet(tc.DEFAULT_RANGES["bd"])
    if not tc.version_in_range(version, spec):
        pytest.skip(f"bd {version!r} is outside the supported range {spec}")
    marker = replica_bootstrap.GATE_BUSY_MARKER
    with open(shutil.which("bd"), "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            found = mm.find(marker.encode())
    assert found >= 0, (
        f"bd {version} (supported {spec}) no longer contains {marker!r}: "
        "bd reworded its exclusive-gate refusal, so replica_bootstrap's gate-free fallback would "
        "silently stop triggering. Update GATE_BUSY_MARKER (and BD_REFUSAL_PREFIX) to the new "
        "wording, or switch gate_refused to a structured signal if bd now offers one (bh-myjbi)."
    )
