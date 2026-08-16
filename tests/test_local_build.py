"""Guards on `scripts/local-build.sh` — the stamped local build (bh-7hacm).

THE DEFECT: `uv tool install --force .` from a tree carrying 19 merged beads produced a `bh`
reporting 0.11.5, byte-identical to the published 0.11.5 those beads postdate. `bh --version`
could not tell a release from someone's working tree, so every field report about which bh was
installed was ambiguous — and one inside bh-ku9n9 genuinely was.

The fix stamps a PEP 440 LOCAL VERSION IDENTIFIER (`0.11.5+local.g790ef0d[.dirty]`) onto builds
that come out of a checkout, from a throwaway `git worktree` so the real tree is never mutated.

What is asserted here, and why each one is a thing that has actually gone wrong somewhere:
  * the SHAPE, semantically (packaging.Version), not by regex — a local segment must sort AFTER
    the same public version, or a local build reads as older than the release it came from;
  * the RELEASE shape has NO local segment, and nothing on the release path routes through this
    script — a stamp that leaked into a published wheel would be rejected by PyPI, which is the
    safe direction, but a stamp that failed to appear locally is the whole defect returning;
  * BOTH HALVES OF CLEANUP, on success AND on failure. Removing the directory does not remove
    git's .git/worktrees bookkeeping; a cleanup that only `rm -rf`s accumulates metadata
    invisibly. The failure path is where a mutate-and-revert implementation breaks, so it is
    tested rather than assumed;
  * the SWEEP reaps an orphan left by a killed run — SIGKILL cannot be trapped, so the leak is
    bounded rather than prevented, and only a later run can do the bounding.

The build tests drive the real script against a minimal fixture checkout with `UV_OFFLINE=1`,
for the reason test_setup_guide_asset.py records at length: `uv build` resolves the build backend
before building, and left online it reaches PyPI whenever uv's cached index response has aged
out — making a packaging test into an intermittent network test. Any tree running this suite has
necessarily been `uv sync`ed, which is what populates hatchling in the cache.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "local-build.sh"
JUSTFILE = (ROOT / "justfile").read_text()
RELEASE_WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text()

#: A buildable project, so `build` mode reaches uv rather than failing early on the fixture.
_GOOD_PYPROJECT = """\
[project]
name = "fixturepkg"
version = "{version}"
requires-python = ">=3.11"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fixturepkg"]
"""

#: The same project pointed at a build backend that does not exist. `[project]` stays readable
#: (so the stamp is computed and the worktree IS created), and the build then blows up inside uv
#: — which is exactly the window the cleanup promise has to cover.
_BROKEN_PYPROJECT = _GOOD_PYPROJECT.replace(
    'build-backend = "hatchling.build"', 'build-backend = "no_such_backend.api"'
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _fixture_checkout(tmp_path: Path, version: str = "1.2.3", *, buildable: bool = True) -> Path:
    """A minimal checkout laid out the way the script expects: both scripts under `<root>/scripts`.

    Its own identity and no signing — this repo's config signs commits and forces annotated tags,
    which a throwaway fixture can neither satisfy nor should inherit (same reasoning, and the same
    settings, as test_local_install.py's fixture).
    """
    (tmp_path / "scripts").mkdir()
    for name in ("local-build.sh", "release-pin.sh"):
        shutil.copy2(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "src" / "fixturepkg").mkdir(parents=True)
    (tmp_path / "src" / "fixturepkg" / "__init__.py").write_text("")
    (tmp_path / "pyproject.toml").write_text(
        (_GOOD_PYPROJECT if buildable else _BROKEN_PYPROJECT).format(version=version)
    )

    _git(tmp_path, "init", "-q")
    for key, value in (
        ("user.name", "fixture"),
        ("user.email", "t@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(tmp_path, "config", key, value)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(root / "scripts" / "local-build.sh"), *args],
        capture_output=True,
        text=True,
        # UV_OFFLINE is uv's own env knob, so this needs no flag plumbed through the script.
        env={**os.environ, "UV_OFFLINE": "1"},
    )


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "--short=7", "HEAD").strip()


def _bookkeeping(root: Path) -> set[str]:
    """Names under .git/worktrees — the half of cleanup that `rm -rf` does not touch."""
    d = root / ".git" / "worktrees"
    return {p.name for p in d.iterdir()} if d.is_dir() else set()


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="a real build needs `uv`")


# ---- the stamp --------------------------------------------------------------


def test_a_clean_checkout_is_stamped_with_its_commit(tmp_path):
    root = _fixture_checkout(tmp_path)
    result = _run(root, "version")
    assert result.returncode == 0, result.stderr

    stamped = Version(result.stdout.strip())
    assert stamped.local == f"local.g{_head(root)}"
    assert stamped.base_version == "1.2.3"
    # The property that makes this correct rather than merely conventional: a local build must
    # never read as OLDER than the release it was built from.
    assert stamped > Version("1.2.3")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda root: (root / "untracked.py").write_text("x = 1\n"), id="untracked"),
        pytest.param(
            lambda root: (root / "src" / "fixturepkg" / "__init__.py").write_text("x = 1\n"),
            id="modified",
        ),
    ],
)
def test_an_unclean_checkout_says_so(tmp_path, change):
    """Untracked counts too: a build that picks up a module nobody committed is not the commit
    it names."""
    root = _fixture_checkout(tmp_path)
    change(root)
    stamped = Version(_run(root, "version").stdout.strip())
    assert stamped.local == f"local.g{_head(root)}.dirty"


def test_the_release_shape_carries_no_local_segment(tmp_path):
    """The pin the release path publishes stays a plain public version — and it sorts BELOW any
    local build of itself. PyPI rejects a local segment outright, so the stamped artifact is
    unpublishable by construction rather than by discipline."""
    root = _fixture_checkout(tmp_path)
    released = subprocess.run(
        [str(root / "scripts" / "release-pin.sh")], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert Version(released).local is None
    assert Version(_run(root, "version").stdout.strip()).local is not None


def test_an_unknown_mode_is_refused(tmp_path):
    root = _fixture_checkout(tmp_path)
    result = _run(root, "publish")
    assert result.returncode == 2
    assert result.stdout == ""


# ---- the recipes, and the release path they must not touch ------------------


def test_the_local_recipes_route_through_the_script():
    assert "\ninstall:\n    ./scripts/local-build.sh install\n" in JUSTFILE
    assert "\nbuild:\n    ./scripts/local-build.sh build\n" in JUSTFILE


def test_a_publishable_build_takes_a_deliberately_NAMED_recipe():
    """The default fails safe: run `just build` and you get an artifact PyPI rejects, never a
    silently mislabelled one.

    The opt-out is a NAME, not a flag value, and that is not a style choice. Measured on just
    1.57, `just build release=1` passes the literal string `release=1` as the parameter — only
    `just build 1` sets it — so a conditional keyed on that value reads as OFF whenever someone
    writes the flag the way it looks like it should be written. A name cannot be half-typed.
    """
    assert "\nbuild-release:\n    uv build\n" in JUSTFILE
    recipes = JUSTFILE.split("# ---- local builds are STAMPED")[1].split("# ---- local-install")[0]
    assert "if release" not in recipes, "the opt-out must not be a parameter value"


def test_the_actual_release_path_does_not_go_through_any_of_this():
    """release.yml builds the v* tag with `uv build` itself, so the published wheel is unstamped
    no matter what the justfile does. `release` / release-pin.sh only READ the version."""
    assert "run: uv build\n" in RELEASE_WORKFLOW
    assert "local-build" not in RELEASE_WORKFLOW
    assert "local-build" not in (ROOT / "scripts" / "release-pin.sh").read_text()
    assert "local-build" not in (ROOT / "scripts" / "push-main.sh").read_text()


# ---- the real build: artifact, cleanup, and the sweep -----------------------


@needs_uv
def test_a_build_stamps_the_wheel_filename_and_leaves_the_tree_untouched(tmp_path):
    """The filename is the point: anyone validating a build knows what is under test from the
    name alone, with nothing to look up."""
    root = _fixture_checkout(tmp_path)
    # UNCOMMITTED work must really be built. The throwaway worktree checks out HEAD, so building
    # it naively would silently drop every edit you are trying to install — a worse lie than the
    # one this script exists to fix, and it is what `.dirty` promises is present.
    (root / "src" / "fixturepkg" / "__init__.py").write_text('MARK = "uncommitted"\n')
    before_status = _git(root, "status", "--porcelain")
    before_books = _bookkeeping(root)

    result = _run(root, "build")
    assert result.returncode == 0, result.stderr

    (wheel,) = (root / "dist").glob("*.whl")
    assert f"+local.g{_head(root)}.dirty-" in wheel.name
    assert Version(wheel.name.split("-")[1]).local is not None
    with zipfile.ZipFile(wheel) as zf:
        assert b"uncommitted" in zf.read("fixturepkg/__init__.py")

    # Nothing mutated, and BOTH halves of cleanup done.
    assert _git(root, "status", "--porcelain") == before_status
    assert _bookkeeping(root) == before_books
    assert not list((root / ".git" / "bh-build").glob("build-*"))


@needs_uv
@pytest.mark.parametrize(
    ("delete", "survivor"),
    [
        # Used to ABORT the build: `ls-files --cached` still names the path, and tar exiting 2 on
        # it kills the pipeline under `set -euo pipefail`.
        pytest.param(
            lambda root: (root / "src" / "fixturepkg" / "gone.py").unlink(),
            None,
            id="unstaged-rm",
        ),
        # Used to SHIP SILENTLY: `ls-files --deleted` is empty once the deletion is staged.
        pytest.param(
            lambda root: _git(root, "rm", "-q", "src/fixturepkg/gone.py"),
            None,
            id="staged-git-rm",
        ),
        # Used to ship BOTH paths: git reports a rename as R, so a D filter without --no-renames
        # matches nothing. An ordinary refactor, not an exotic state.
        pytest.param(
            lambda root: _git(root, "mv", "src/fixturepkg/gone.py", "src/fixturepkg/kept.py"),
            "fixturepkg/kept.py",
            id="rename-git-mv",
        ),
    ],
)
def test_a_module_deleted_in_the_real_tree_does_not_survive_into_the_wheel(
    tmp_path, delete, survivor
):
    """The throwaway checks out HEAD, so REMOVAL has to be replayed onto it as well as addition.

    A `.dirty`-stamped wheel carrying a module you deleted is the exact lie this script exists to
    remove, one level down and now endorsed by the stamp. The earlier proof that the overlay
    worked exercised only the ADD direction; these three are the REMOVE direction, and all three
    were broken by the obvious `ls-files --deleted` spelling.
    """
    root = _fixture_checkout(tmp_path)
    (root / "src" / "fixturepkg" / "gone.py").write_text("GONE = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "add gone")

    delete(root)
    result = _run(root, "build")
    assert result.returncode == 0, result.stderr

    (wheel,) = (root / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    assert "fixturepkg/gone.py" not in names
    if survivor:
        assert survivor in names


@needs_uv
def test_a_FAILED_build_still_leaves_the_tree_and_the_bookkeeping_exactly_as_it_found_them(
    tmp_path,
):
    """The case that kills a mutate-and-revert implementation: it leaves pyproject and uv.lock
    modified when the build blows up. Stamping inside a throwaway worktree has no such window."""
    root = _fixture_checkout(tmp_path, buildable=False)
    before_status = _git(root, "status", "--porcelain")
    before_books = _bookkeeping(root)

    result = _run(root, "build")
    assert result.returncode != 0, "the fixture is supposed to be unbuildable"

    assert _git(root, "status", "--porcelain") == before_status
    assert _bookkeeping(root) == before_books
    assert not list((root / ".git" / "bh-build").glob("build-*"))


@needs_uv
def test_the_next_run_reaps_a_worktree_left_by_a_killed_one(tmp_path):
    """SIGKILL cannot be trapped, so a killed build leaks its worktree by construction and only a
    later run can reap it. Both halves again — the directory AND the bookkeeping — and the sweep
    says what it removed, because a leak nobody can see is a leak that comes back."""
    root = _fixture_checkout(tmp_path)

    # A pid that is definitively gone, obtained by spawning and reaping rather than by picking a
    # number and hoping. The orphan is registered with git, exactly as a killed run's would be.
    dead = subprocess.Popen(["true"])
    dead.wait()
    orphan = root / ".git" / "bh-build" / f"build-{dead.pid}-99999"
    _git(root, "worktree", "add", "--detach", "--quiet", str(orphan), "HEAD")
    assert orphan.is_dir() and orphan.name in _bookkeeping(root)

    result = _run(root, "build")
    assert result.returncode == 0, result.stderr

    assert not orphan.exists()
    assert orphan.name not in _bookkeeping(root)
    assert f"sweeping orphaned build worktree {orphan.name}" in result.stderr


@needs_uv
def test_the_sweep_spares_a_run_that_is_still_in_flight(tmp_path):
    """What it must NOT do. A concurrent build's worktree disappearing out from under it would
    look exactly like a mysterious mid-build failure — the liveness check is what prevents it."""
    root = _fixture_checkout(tmp_path)

    live = subprocess.Popen(["sleep", "60"])
    try:
        inflight = root / ".git" / "bh-build" / f"build-{live.pid}-99999"
        _git(root, "worktree", "add", "--detach", "--quiet", str(inflight), "HEAD")

        assert _run(root, "build").returncode == 0
        assert inflight.is_dir(), "the sweep reaped a live run's worktree"
    finally:
        live.kill()
        live.wait()
