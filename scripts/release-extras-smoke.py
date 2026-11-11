#!/usr/bin/env python3
"""Pre-publish smoke: the BUILT wheel installs and behaves in clean venvs, bare and per extra.

``beadhive[github]`` and ``beadhive[xtdb]`` vendor their plugin import packages in the root wheel
and add only third-party dependencies (githubkit; psycopg), so they must resolve from the package
index alone. This installs the wheel into three fresh venvs, run from an empty directory so no uv
workspace or ``tool.uv.sources`` can leak in, and checks in each (bh-lrs9v):

* bare: ``import beadhive`` imports neither githubkit nor psycopg; the vendored plugin packages
  import and ship their ``plugin.json``; enabling either plugin without its extra is a typed
  refusal naming the extra (never an ImportError); ``doctor`` reports the missing psycopg.
* ``[github]``: the real ``tracker.adapter`` factory builds the adapter; githubkit imports.
* ``[xtdb]``: the real ``beadlog.store`` factory builds the store; psycopg and ``beadlog_xtdb``
  import; ``doctor`` no longer reports a missing dependency.

Each venv also runs the installed ``beadlog extract --help`` (every strategy listed from the wheel's
own entry points) and ``beadlog read --help`` (bh-7br2h).

Each venv then runs the installed ``bh`` entry point: ``bh --version`` plus commands that import the
full ``beadhive.cli`` and build its option types (``bh --help``, ``bh hq authority --help``,
``bh hq placement --help``), each of which must exit 0. ``--version`` alone never imports the CLI,
so a dependency release that breaks it (typer 0.29.0, bh-jpaoz) slipped past the old smoke.

Exits non-zero on the first failure, so the release job (and the tag) fails before publication.
Usage: ``release-extras-smoke.py dist/beadhive-<version>-py3-none-any.whl``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

COMMON = r"""
import importlib.util, sys
import beadhive
for name in ("githubkit", "psycopg"):
    assert name not in sys.modules, f"import beadhive imported {name}"
from pathlib import Path
# Located by spec, never imported: importing beadlog_xtdb itself needs psycopg.
def shipped(pkg, resource):
    (root,) = importlib.util.find_spec(pkg).submodule_search_locations
    return (Path(root) / resource).is_file()
for pkg in ("beadhive_tracker_github", "beadhive_beadlog_xtdb"):
    assert shipped(pkg, "plugin.json"), f"{pkg}: no plugin.json"
    assert shipped(pkg, "py.typed"), f"{pkg}: no py.typed"
assert shipped("beadlog_xtdb", "py.typed"), "beadlog_xtdb: no py.typed"
import beadhive_tracker_github.factory, beadhive_beadlog_xtdb.factory
for name in ("githubkit", "psycopg"):
    assert name not in sys.modules, f"importing a plugin factory imported {name}"

from beadhive.bootstrap.tracker import TrackerAdapters, TrackerBindingError
from beadhive.bootstrap.beadlog_store import BeadlogStores, BeadlogStoreUnavailable
from beadhive.modules.tracker.contracts import Operation
from beadhive import beadlog_xtdb_plugin as xtdb

ENTRY = {
    "provider": "github", "org": "acme", "repo": "widget", "prefix": "w",
    "stores": {
        "internal": {"remote": "r"},
        "public": {"prefix": "wp", "path": "/s/p", "remote": "git-origin",
                   "source": "github-issues", "adapter": "tracker-github"},
    },
}
GITHUB = {"tracker-github": {"enabled": True, "read": {"issues": {"enabled": True}}}}
XTDB = {"beadlog-xtdb": {"enabled": True}}

def github_adapters():
    return TrackerAdapters(plugins=GITHUB, plugin_kernel=None, token_source=lambda: None)

def xtdb_stores():
    return BeadlogStores(plugins=XTDB, plugin_kernel=None)

def xtdb_doctor_package_row():
    doc = xtdb._document({"plugins": XTDB})
    return [r for r in xtdb.doctor_rows(doc) if r["check"] == "package"]
"""

BARE = r"""
assert importlib.util.find_spec("githubkit") is None, "githubkit leaked into the bare install"
assert importlib.util.find_spec("psycopg") is None, "psycopg leaked into the bare install"
try:
    github_adapters().for_store(ENTRY, requested=frozenset({Operation.ISSUES_READ}))
except TrackerBindingError as exc:
    assert "beadhive[github]" in exc.message, exc.message
else:
    raise AssertionError("tracker-github enabled without githubkit must refuse")
try:
    xtdb_stores().resolve("xtdb")
except BeadlogStoreUnavailable as exc:
    assert "beadhive[xtdb]" in str(exc), exc
else:
    raise AssertionError("beadlog-xtdb enabled without psycopg must refuse")
(row,) = xtdb_doctor_package_row()
assert row["severity"] == "error" and "psycopg" in row["detail"], row
assert "beadhive[xtdb]" in row["remedy"], row
assert not xtdb.package_installed()
"""

GITHUB = r"""
import githubkit
import beadhive_tracker_github.adapter
assert importlib.util.find_spec("psycopg") is None, "psycopg leaked into [github]"
bound = github_adapters().for_store(ENTRY, requested=frozenset({Operation.ISSUES_READ}))
assert bound.port is not None
"""

XTDB = r"""
import psycopg, beadlog_xtdb
assert importlib.util.find_spec("githubkit") is None, "githubkit leaked into [xtdb]"
bound = xtdb_stores().resolve("xtdb")
assert bound.store == "xtdb" and bound.port is not None
assert xtdb.package_installed()
assert xtdb_doctor_package_row() == []
"""

SCENARIOS = (("bare", "", BARE), ("github", "[github]", GITHUB), ("xtdb", "[xtdb]", XTDB))

# ``--version`` short-circuits before ``beadhive.cli`` is imported; the rest import the whole CLI
# and render the hq authority/placement options, whose duration type rides on typer internals.
CLI_COMMANDS = (
    ("--version",),
    ("--help",),
    ("hq", "authority", "--help"),
    ("hq", "placement", "--help"),
)


def run(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None, quiet: bool = False
) -> None:
    """Echo and run ``argv``, raising on a non-zero exit; ``quiet`` drops stdout (help text)."""
    print("+", " ".join(argv), flush=True)
    stdout = subprocess.DEVNULL if quiet else None
    subprocess.run(argv, cwd=cwd, check=True, env=env, stdout=stdout)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].endswith(".whl"):
        print(__doc__, file=sys.stderr)
        return 2
    wheel = Path(argv[1]).resolve()
    if not wheel.is_file():
        print(f"no such wheel: {wheel}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="bh-extras-smoke-") as tmp:
        work = Path(tmp)
        for name, extra, check in SCENARIOS:
            print(f"== {name}: install {wheel.name}{extra}", flush=True)
            venv = work / f"venv-{name}"
            python = venv / "bin" / "python"
            # cwd is the empty temp dir and --no-config ignores any uv.toml, so no workspace
            # resolution or [tool.uv.sources] can satisfy a requirement the index cannot.
            run(["uv", "--no-config", "venv", "--quiet", str(venv)], cwd=work)
            run(
                [
                    "uv",
                    "--no-config",
                    "pip",
                    "install",
                    "--quiet",
                    "--python",
                    str(python),
                    f"beadhive{extra} @ {wheel.as_uri()}",
                ],
                cwd=work,
            )
            env = {
                "BH_SKIP_SETUP_CHECK": "1",
                "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
                "HOME": str(work),
            }
            print(f"-- {name}: checks", flush=True)
            subprocess.run([str(python), "-I", "-c", COMMON + check], cwd=work, check=True, env=env)
            print(f"-- {name}: cli", flush=True)
            for args in CLI_COMMANDS:
                run([str(venv / "bin" / "bh"), *args], cwd=work, env=env, quiet="--help" in args)
            # The wheel vendors beadlog_sources and must register its own entry points and the
            # `beadlog` script (bh-7br2h): extract lists every strategy, no other dist installed.
            help_text = subprocess.run(
                [str(venv / "bin" / "beadlog"), "extract", "--help"],
                cwd=work,
                check=True,
                env=env,
                capture_output=True,
                text=True,
            ).stdout
            for strategy in ("bd", "bd-history", "docs", "git-commits", "planning"):
                assert f"extract with the {strategy} strategy" in help_text, strategy
            run([str(venv / "bin" / "beadlog"), "read", "--help"], cwd=work, env=env, quiet=True)
            print(f"== {name}: ok", flush=True)
    print("extras smoke: bare, [github] and [xtdb] all passed (checks + full CLI)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
