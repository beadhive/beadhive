"""How THIS bh got installed, and therefore how it is upgraded (bh-jmw0).

Every plane installs bh with `uv tool install` — that is not the distinction. What differs is
what an upgrade CONSISTS OF, and a hint naming only the `uv` half is wrong in ways the operator
cannot see:

  PROVISIONED   two steps. `just local-install` runs `nix profile install .#default` (the
                TOOLCHAIN: bd, dolt, gh, git-workspace) and then
                `uv tool install "beadhive[otel]==$(scripts/release-pin.sh)"` (bh). Those pins
                move INDEPENDENTLY — flake.lock carries one, the release pin the other — so
                upgrading bh alone can leave a host whose bh expects a newer bd than its profile
                has. bh-65kh is exactly that (bh wanting a bd whose embedded dolt is >= 2.2.0).
  CONTAINER     rebuild. The image installs bh from a wheel at BUILD time, so a runtime
                `uv tool install --force` is discarded by the next `docker compose up` — the
                same disappearing-act as bh-h5if's runtime-installed harness.
  EDITABLE      nothing, or `just install`. A source checkout is always current by construction.
  PYPI          `uv tool upgrade beadhive` — the original install path, and now DISCOURAGED. It
                installs bh and nothing else: bd, dolt, gh and git-workspace are whatever the
                machine happens to carry, where the provisioned plane pins all four through
                flake.lock. So an upgrade here moves bh and leaves its dependencies wherever they
                were. The command is still given — an operator on this path needs to be able to
                upgrade — with one line naming what it does not cover and the verb that shows the
                gap (`bh setup check`). Discouraged, not refused: telling someone their existing
                install is wrong while withholding how to update it helps nobody.

WHY A VERSION IS NEVER SPELLED HERE. `scripts/release-pin.sh` derives the pin from the checkout
so that "the tag names the PyPI release" holds by construction rather than by discipline; its
header argues that a second place to type a version is a second place for it to be wrong. This
module is not going to become that second place — the caller passes the pin in, or gets a command
without one.

DETECTION PREFERS AN EXPLICIT SIGNAL, following the rule `compose.py` already settled for the
container marker: /.dockerenv and /proc/1/cgroup differ across docker, podman, containerd and
nerdctl, so a detector built on them goes quietly wrong on whichever runtime nobody tested. Where
no explicit signal exists, this returns UNKNOWN rather than guessing — a wrong guess the operator
cannot see is the whole defect being removed.
"""

from __future__ import annotations

import sys
from pathlib import Path

CONTAINER = "container"
PROVISIONED = "provisioned"
EDITABLE = "editable"
PYPI = "pypi"
UNKNOWN = "unknown"

#: Directory `just local-install` step 1 installs the toolchain into, and the one
#: `doctor._devshell_only_warnings` already treats as "the PATH that survives leaving the
#: devshell". Its presence is what distinguishes a provisioned host from an ad-hoc PyPI install.
_NIX_PROFILE_BIN = Path.home() / ".nix-profile" / "bin"


def detect(*, package_dir: Path | None = None, profile_bin: Path | None = None) -> str:
    """Which plane this bh is running under.

    Order IS the policy, so it is stated rather than left to if/elif accident. A host can satisfy
    several conditions at once — an editable checkout on a provisioned host, a devshell inside a
    container — and the earlier answer is the one that governs how an upgrade must be performed:

      1. CONTAINER first. It is the only fully explicit signal, and it overrides everything: no
         upgrade performed inside the image survives, whatever else is true of the filesystem.
      2. EDITABLE next. A source checkout is upgraded by editing it; how bh was ORIGINALLY put
         there is irrelevant once it runs from source.
      3. PROVISIONED vs PYPI last, and only these two need a filesystem tell — the nix toolchain
         profile that `just local-install` step 1 creates.

    `package_dir`/`profile_bin` are injected for tests; production passes neither.
    """
    from .compose import in_container  # lazy: compose pulls in typer/run

    if in_container():
        return CONTAINER
    if _is_editable(package_dir):
        return EDITABLE
    if (profile_bin or _NIX_PROFILE_BIN).is_dir():
        return PROVISIONED
    return PYPI


def _is_editable(package_dir: Path | None = None) -> bool:
    """True when bh runs from a source checkout rather than an installed snapshot.

    PEP 610 first: pip and uv both record `direct_url.json` with `dir_info.editable` on an
    editable install, which is a STANDARD rather than a path heuristic. Falls back to asking
    whether the running package sits under a `src/` layout, which is what a checkout looks like
    and an installed wheel never does — site-packages has no `src` parent.
    """
    import importlib.metadata as md

    try:
        raw = md.distribution("beadhive").read_text("direct_url.json")
    except Exception:  # noqa: BLE001 - absent metadata is a normal answer, not an error
        raw = None
    if raw:
        import json

        try:
            if json.loads(raw).get("dir_info", {}).get("editable"):
                return True
        except (ValueError, AttributeError):
            pass  # malformed metadata: fall through to the layout tell rather than crash

    pkg = package_dir or Path(__file__).resolve().parent
    return pkg.parent.name == "src"


def upgrade_steps(plane: str, *, pin: str = "") -> list[str]:
    """The commands that upgrade bh on `plane`, in order — empty when there is nothing to run.

    `pin` is the version to preserve (``scripts/release-pin.sh``'s output). Passed in, never
    derived here: see the module docstring on why this file does not become a second place a
    version is written down. Without it the provisioned command is emitted UNPINNED, which is
    honest — bh cannot preserve a pin it was not told.
    """
    spec = f"'beadhive[otel]=={pin}'" if pin else "'beadhive[otel]'"
    if plane == CONTAINER:
        return []  # rebuilt, not reinstalled — see `describe`
    if plane == EDITABLE:
        return ["just install"]
    if plane == PROVISIONED:
        # BOTH halves. flake.lock and the release pin move independently, so naming only the uv
        # step leaves the toolchain behind — silently, until a verb wants a bd the profile lacks.
        return ["nix profile upgrade", f"uv tool install --force {spec}"]
    if plane == PYPI:
        return ["uv tool upgrade beadhive"]
    return []


def describe(plane: str, *, pin: str = "") -> list[str]:
    """Operator-facing lines for `plane`: what to run, or why there is nothing to run.

    UNKNOWN lists the candidates WITH their conditions instead of picking one. An operator knows
    which host they are on; bh guessing wrong on their behalf is what this exists to stop.
    """
    if plane == CONTAINER:
        return [
            "upgrade: rebuild the image (`just image core`) — bh is installed from a wheel at",
            "         BUILD time, so reinstalling inside a running container is discarded by the",
            "         next `docker compose up`.",
        ]
    if plane == UNKNOWN:
        return [
            "upgrade: could not determine how this bh was installed. By plane:",
            *(
                f"           {p:<12} {' && '.join(upgrade_steps(p, pin=pin))}"
                for p in (PROVISIONED, PYPI, EDITABLE)
            ),
        ]
    steps = upgrade_steps(plane, pin=pin)
    if not steps:
        return []
    if plane == PYPI:
        # The original install path, now discouraged: it carries bh and none of its dependencies.
        # Still answered — someone on it needs to upgrade — with the gap named once, and pointing
        # at the verb that shows it rather than restating a list that would drift.
        return [
            f"upgrade: {steps[0]}",
            "         note: this path installs bh ONLY — bd, dolt, gh and git-workspace are not",
            "         pinned with it (the provisioned plane pins all four via flake.lock).",
            "         `bh setup check` reports which are missing.",
        ]
    if len(steps) == 1:
        return [f"upgrade: {steps[0]}"]
    return ["upgrade (both steps — the toolchain and bh are pinned separately):"] + [
        f"           {s}" for s in steps
    ]


def running_from() -> str:
    """Where the running bh lives — the string doctor already renders, kept here so the plane and
    its evidence are read from one place."""
    return str(Path(sys.modules["beadhive"].__file__ or "").resolve().parent)
