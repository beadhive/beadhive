"""The ONE sanctioned entry point for a PUBLIC bead snapshot — deliberately single-hive.

Boundary module for epic bh-7jm7v ("Publish boundary for bead data"), bead bh-7jm7v.3. See
`docs/design/publish-boundary-adr.md`; the guard that keeps this file honest is
`tests/test_publish_boundary.py`.

**This module is not wired into any CLI command and is not called from anywhere.** That is
deliberate: nothing in this repo publishes anything, and this epic does not build a publish
pipeline. What it establishes is the *shape* the eventual publish step must be written into,
with a test that fails loudly if that shape is widened. A future publish step calls
`export_public_snapshot()`; it does not hand-roll another `bd export` invocation.

WHY A BOUNDARY EXISTS AT ALL. This hive's bead data is already public — it rides
`refs/dolt/data` on the same public GitHub remote as the code — so exporting *this* hive's
issues exposes nothing new. The risk is scope creep past that fact: `bh` also maintains
machine-local AGGREGATE stores that span every registered hive, and some registered hives are
private. Concretely, in this package:

  - `hub.py`   — "one aggregated beads DB (under $BH_HOME) holding a cross-hive view of every
                 registered hive"; `bh hub <bd cmd>` queries it.
  - `hub_bulk.py`, `hq.py`, `hq_restore.py` — the same aggregate, fleet-wide.
  - `bd.passthrough()` / `route.fan_out()` / `route.targets()` — the `-a`/`--all` and
    `-r`/`--hive` machinery that runs one `bd` subcommand across MANY hives.

A publish path that can reach any of those is one refactor away from publishing a private
hive's beads. So the guarantee is structural, not conventional: the transitive import closure
of this module contains none of the aggregate modules, and this module makes no direct
reference to the cross-hive routing helpers. Nobody has to remember the rule; the test fails.

WHAT THIS MODULE MAY IMPORT. `bd.run()` — the shared, hive-scoped bd-invocation helper, which
emits `bd -C <hive_root> …`, i.e. exactly one hive named by a filesystem path. It reaches
`route`/`registry` transitively, as does *every* bd invocation in this package, so a
transitive ban on those two is not expressible without duplicating the bd seam; the guard bans
DIRECT reference to the fan-out entry points instead (`bd.passthrough`, `route.fan_out`,
`route.targets`) and bans the aggregate modules transitively. That split is the honest one —
see the ADR section for why it is drawn there and not somewhere flatteringly stricter.

NO HIVE-SELECTION PARAMETER EXISTS. `export_public_snapshot()` takes a `hive_root` PATH, not a
hive NAME: a path is "the checkout you are already standing in", while a name is a registry
lookup — the `-r <hive>` shape, the first step toward `--all`. There is deliberately no
`hive=`/`scope=`/`all=` parameter, not even one defaulting safely, because a parameter that
exists is a parameter a caller can pass. `hive_root` is additionally refused at runtime when it
points into bh's machine-local aggregate area ($BH_HOME and friends), which is the one way a
path-shaped argument could still address the cross-hive view.

THE INVOCATION ITSELF is bh-7jm7v.1's decision, verbatim: `bd export -o <dest>/issues.jsonl`,
with `--all`, `--include-memories` and `--include-infra` named as permanently forbidden (each
re-admits infra beads and/or memories; `--all` also reaches bd's ephemeral wisps table). The
argv is built by a pure function so a test can assert the constructed command line rather than
squinting at one run's output — which is what that ADR section asks a publish step's test suite
to do.
"""

from __future__ import annotations

from pathlib import Path

from . import bd, config

#: Filename bh-7jm7v.1 fixed for the public snapshot.
PUBLIC_SNAPSHOT_FILENAME = "issues.jsonl"

#: Flags that must never appear in a public-snapshot `bd export` (bh-7jm7v.1). Exported so a
#: caller's own tests can assert against the same list instead of restating it.
FORBIDDEN_EXPORT_FLAGS = ("--all", "--include-memories", "--include-infra")

#: Flags that would route the invocation past this one hive (bh/`bd` cross-hive routing).
FORBIDDEN_ROUTING_FLAGS = ("-a", "--all", "-r", "--hive", "--global")


class PublishScopeError(RuntimeError):
    """The requested publish target is not one hive's own checkout."""


def public_snapshot_argv(dest_dir: Path | str) -> list[str]:
    """The `bd` sub-argv for a public snapshot — bh-7jm7v.1's decided invocation, and nothing
    else. Pure (no I/O, no config, no subprocess) so the *command line* is directly assertable.

    Returns the args AFTER `bd -C <hive_root>`: `["export", "-o", "<dest_dir>/issues.jsonl"]`.
    No flag is added conditionally, so there is no branch that could grow an `--all`.
    """
    return ["export", "-o", str(Path(dest_dir) / PUBLIC_SNAPSHOT_FILENAME)]


def _aggregate_store_dirs() -> list[Path]:
    """Machine-local directories that hold CROSS-HIVE bead data. `config.home()` ($BH_HOME) is
    listed as well as the three stores under it because a future aggregate store gets covered
    for free; the three are listed individually because each is independently overridable by
    env ($BH_HUB / $BH_HQ / $BH_CACHE) and can therefore sit outside $BH_HOME."""
    return [config.home(), config.hub_dir(), config.hq_dir(), config.cache_dir()]


def _resolve_hive_root(hive_root: Path | str) -> Path:
    """Validate that `hive_root` is one hive's own checkout, or refuse.

    Two refusals, both about scope rather than tidiness:

    1. Inside bh's aggregate area — that is the cross-hive view (`hub`/`hq`) or another hive's
       cache clone, i.e. exactly the widening this module exists to prevent, arriving through
       the one parameter that takes a path.
    2. No `.beads/` — not a hive checkout at all, so whatever `bd -C` found there (an ancestor
       directory's database, most likely) is not the thing the caller named.
    """
    root = Path(hive_root).expanduser().resolve()
    for aggregate in _aggregate_store_dirs():
        agg = aggregate.expanduser().resolve()
        if root == agg or root.is_relative_to(agg):
            raise PublishScopeError(
                f"refusing to publish from {root}: it is inside bh's machine-local aggregate "
                f"area ({agg}), which holds a CROSS-HIVE view that can span private hives. "
                "A public snapshot is taken from one hive's own checkout."
            )
    if not (root / ".beads").is_dir():
        raise PublishScopeError(
            f"refusing to publish from {root}: no .beads/ directory — not a hive checkout."
        )
    return root


def export_public_snapshot(hive_root: Path | str, dest_dir: Path | str) -> Path:
    """Write `hive_root`'s public bead snapshot to `<dest_dir>/issues.jsonl` and return its path.

    `hive_root` is the checkout of the ONE hive being published (a path, never a hive name —
    see the module docstring). There is no parameter for selecting a different hive, several,
    or the aggregate; adding one is the regression `tests/test_publish_boundary.py` fails on.

    Raises `PublishScopeError` if `hive_root` is not a single hive's checkout, and
    `RuntimeError` if `bd` exits non-zero (a partial/absent snapshot must not read as success).
    """
    root = _resolve_hive_root(hive_root)
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    argv = public_snapshot_argv(dest)
    res = bd.run(argv, cwd=root)
    if res.returncode != 0:
        raise RuntimeError(f"bd {' '.join(argv)} failed in {root} (exit {res.returncode})")
    return dest / PUBLIC_SNAPSHOT_FILENAME
