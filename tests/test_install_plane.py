"""`install_plane` — which plane this bh runs under, and what upgrading it means there (bh-jmw0).

The defect: doctor emitted `uv tool install --force 'beadhive[otel]'` unconditionally, from three
call sites. The TOOL was right — bh comes from uv on every plane — but the advice was not:

  * it dropped the version pin `just local-install` derives from the tag (scripts/release-pin.sh),
    so following it moved a provisioned host off the pin install.sh established;
  * it named one step of that plane's TWO, since flake.lock pins the toolchain separately;
  * inside the image it described a repair the next `docker compose up` discards.
"""

from __future__ import annotations

import pytest

from beadhive import install_plane


@pytest.fixture
def outside_container(monkeypatch):
    """Neutralise the container marker so filesystem-driven cases are what is under test."""
    monkeypatch.delenv("BH_IN_CONTAINER", raising=False)


# ---- detection ------------------------------------------------------------------------------


def test_the_container_marker_wins_over_every_filesystem_tell(monkeypatch, tmp_path):
    """Order is policy: nothing done inside the image survives, whatever the filesystem says, so
    the container answer must not be reachable-around by an editable checkout or a nix profile."""
    monkeypatch.setenv("BH_IN_CONTAINER", "1")
    profile = tmp_path / ".nix-profile" / "bin"
    profile.mkdir(parents=True)

    assert install_plane.detect(package_dir=tmp_path / "src" / "beadhive", profile_bin=profile) == (
        install_plane.CONTAINER
    )


def test_a_source_checkout_is_editable_even_on_a_provisioned_host(outside_container, tmp_path):
    """A host can be both. Once bh runs from source, HOW it originally got there stops mattering:
    the upgrade is to edit the checkout."""
    profile = tmp_path / ".nix-profile" / "bin"
    profile.mkdir(parents=True)

    plane = install_plane.detect(package_dir=tmp_path / "src" / "beadhive", profile_bin=profile)

    assert plane == install_plane.EDITABLE


def test_a_nix_toolchain_profile_means_provisioned(outside_container, tmp_path, monkeypatch):
    """The tell that separates a provisioned host from an ad-hoc PyPI install: `just
    local-install` step 1 creates the toolchain profile, an ad-hoc `uv tool install` never does."""
    monkeypatch.setattr(install_plane, "_is_editable", lambda package_dir=None: False)
    profile = tmp_path / ".nix-profile" / "bin"
    profile.mkdir(parents=True)

    assert install_plane.detect(profile_bin=profile) == install_plane.PROVISIONED


def test_no_toolchain_profile_means_an_ad_hoc_pypi_install(
    outside_container, tmp_path, monkeypatch
):
    monkeypatch.setattr(install_plane, "_is_editable", lambda package_dir=None: False)

    assert install_plane.detect(profile_bin=tmp_path / "absent") == install_plane.PYPI


# ---- what upgrading MEANS, per plane ---------------------------------------------------------


def test_a_provisioned_upgrade_names_both_halves_and_keeps_the_pin():
    """The defect in one assertion: the old hint was one unpinned command. flake.lock and the
    release pin move independently, so the toolchain half cannot go unsaid, and an unpinned
    reinstall silently moves the host off the version its tag established."""
    steps = install_plane.upgrade_steps(install_plane.PROVISIONED, pin="0.8.0")

    assert steps == ["nix profile upgrade", "uv tool install --force 'beadhive[otel]==0.8.0'"]


def test_an_unknown_pin_yields_an_honest_unpinned_command():
    """bh cannot preserve a pin it was not given. Emitting the command unpinned is honest;
    inventing a version here would make this module the second place a version is written down,
    which is exactly what scripts/release-pin.sh exists to prevent."""
    steps = install_plane.upgrade_steps(install_plane.PROVISIONED)

    assert steps[-1] == "uv tool install --force 'beadhive[otel]'"


def test_a_container_is_never_told_to_reinstall():
    assert install_plane.upgrade_steps(install_plane.CONTAINER) == []

    described = "\n".join(install_plane.describe(install_plane.CONTAINER))
    assert "uv tool install" not in described
    assert "rebuild the image" in described


def test_the_pypi_path_still_gets_a_command_and_a_caveat():
    """DISCOURAGED, not refused: it installs bh and none of its dependencies. Telling an operator
    their install is wrong while withholding how to update it helps nobody, so the command is
    given and the gap named once — pointing at the verb that shows it rather than restating a
    tool list that would drift."""
    described = "\n".join(install_plane.describe(install_plane.PYPI))

    assert "uv tool upgrade beadhive" in described
    assert "bh setup check" in described
    assert "ONLY" in described


def test_an_undetermined_plane_lists_candidates_instead_of_guessing():
    """A wrong guess the operator cannot see is the defect being removed. They know which host
    they are on; bh does not have to."""
    described = "\n".join(install_plane.describe(install_plane.UNKNOWN, pin="0.8.0"))

    assert "could not determine" in described
    assert "nix profile upgrade" in described  # the provisioned candidate
    assert "uv tool upgrade beadhive" in described  # the pypi candidate
    assert "just install" in described  # the editable candidate


def test_an_editable_checkout_is_upgraded_by_its_own_checkout():
    assert install_plane.upgrade_steps(install_plane.EDITABLE) == ["just install"]
