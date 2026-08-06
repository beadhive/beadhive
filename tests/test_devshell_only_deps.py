"""local-install must leave bh's deps reachable OUTSIDE `nix develop` (bh-ytqc).

MEASURED ON THE REAL LINUX HOST (beadhive-factory, Debian 13 x86_64), 2026-08-05, on
`wt/bead/epic/bh-q160` @ 98bf536 immediately after a SUCCESSFUL `just local-install
from_source=1`:

    inside  nix develop:  bh setup check -> 4 of 4  (git-workspace, gh, bd, dolt all found)
    outside nix develop:  bh setup check -> 0 of 4  'missing: git-workspace, gh, bd, dolt'

A provisioned host is precisely the machine nobody is sitting at — cron, systemd units,
`ssh <host> bh sync` — and every one of those gets the bare PATH. `~/.nix-profile/bin` was
already on that PATH and `flake.nix` already exposed `packages.default` as a buildEnv of the
same toolchain; nothing put the two together.

Pure Python and text assertions: this needs no `nix` and so runs on macOS too, the same posture
`test_flake_toolchain.py` takes for the same reason. What CANNOT be proven from here is the
measurement itself — 4 of 4 from a bare login shell on a provisioned host is a Linux-host
verification, and it is named as such in the ADR amendment.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from beadhive import doctor

ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = ROOT / "justfile"
FLAKE = ROOT / "flake.nix"
ADR = ROOT / "docs" / "design" / "deployment-isolation-direction-adr.md"


def _local_install_recipe() -> str:
    text = JUSTFILE.read_text()
    match = re.search(r"\n_local-install:\n(.*?)\n\n", text, re.DOTALL)
    assert match, "could not locate the `_local-install` recipe in the justfile"
    return match.group(1)


# ---- the mechanism: the toolchain reaches the user profile, not just the devShell -----


def test_local_install_installs_the_toolchain_into_the_user_profile():
    """Step 1 used to be deliberately EMPTY — "the toolchain arrives with the flake devShell",
    which is true only while you are inside it. That emptiness was the bug."""
    recipe = _local_install_recipe()

    assert "nix profile install .#default" in recipe
    assert "already here, from the flake devShell" not in recipe


def test_the_profile_install_is_guarded_so_a_re_run_is_still_a_no_op():
    """`local-install` promises idempotence end to end, and `nix profile install` on an
    already-installed flake ref is not a documented no-op across nix versions."""
    recipe = _local_install_recipe()

    assert "nix profile list" in recipe
    assert "beadhive-local-install-toolchain" in recipe


def test_the_flake_still_exposes_the_package_the_step_installs():
    """`.#default` is not a new mechanism — it is the buildEnv that already existed and was
    never wired. If the flake renames it, this step silently installs nothing."""
    flake = FLAKE.read_text()

    assert 'name = "beadhive-local-install-toolchain"' in flake
    assert "default = pkgs.buildEnv" in flake


# ---- bh doctor NAMES the devShell-only state ------------------------------------------


def test_doctor_names_deps_visible_only_inside_a_devshell(monkeypatch, tmp_path):
    """`nix develop` stays a supported entry point, so the state has to be detectable — the
    symptom otherwise appears much later as an unattended job failing with tools "missing"
    that a human can plainly see."""
    monkeypatch.setenv("IN_NIX_SHELL", "impure")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda b: f"/nix/store/abc-{b}/bin/{b}")

    [warning] = doctor._devshell_only_warnings()

    assert "only inside this `nix develop` shell" in warning
    assert "nix profile install .#default" in warning
    assert "git-workspace" in warning


def test_doctor_is_silent_outside_a_devshell(monkeypatch):
    monkeypatch.delenv("IN_NIX_SHELL", raising=False)

    assert doctor._devshell_only_warnings() == []


def test_doctor_is_silent_once_the_toolchain_is_in_the_user_profile(monkeypatch, tmp_path):
    """The success state: reachable now AND after leaving the shell — no warning."""
    monkeypatch.setenv("IN_NIX_SHELL", "impure")
    profile_bin = tmp_path / ".nix-profile" / "bin"
    profile_bin.mkdir(parents=True)
    for binary in ("git-workspace", "gh", "bd", "dolt"):
        (profile_bin / binary).write_text("")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda b: f"/nix/store/abc-{b}/bin/{b}")

    assert doctor._devshell_only_warnings() == []


# ---- the ADR's measured table is corrected in place, not deleted ----------------------


def test_the_adr_table_says_which_shell_each_column_ran_in():
    """The original two-column table did not, and that is what made 4-of-4 vs 2-of-4 look like
    the same test. The finding is corrected, never removed."""
    adr = ADR.read_text()

    assert "inside `nix develop`" in adr
    assert "outside" in adr
    assert "**0 of 4**" in adr
    assert "**2 of 4**" in adr  # the original mise measurement survives


def test_the_adr_no_longer_claims_path_blockers_are_structurally_impossible():
    adr = ADR.read_text()

    stale = "| PATH-class blockers found | **5** in one session | structurally impossible |"
    assert stale not in adr
    assert "overstated as implemented" in adr
