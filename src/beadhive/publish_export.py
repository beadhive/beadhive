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

THE PUBLISHED PAYLOAD IS A SEPARATE ARTIFACT FROM `.1`'s JSONL (bh-7jm7v.4). `.1`'s
`issues.jsonl` is `bd export`'s own wire format — one JSON object per line, no enclosing
envelope, and not a shape `bh` owns or versions. The thing an external consumer (the epic's
own framing: a widget, or "a repo that is not beadhive-ui") actually loads is a single
wrapping JSON document that carries a `schema_version`, built by taking `.1`'s exported
records, reducing each to bh-7jm7v.2's field allow-list, and collecting the result into one
envelope object. `public_snapshot_envelope()` below is that shape, pinned the same way
`public_snapshot_argv()` pins .1's invocation — pure, so a consumer-side contract test can
assert against it directly instead of eyeballing one publish run's output. See
docs/design/publish-boundary-adr.md's bh-7jm7v.4 section for the full reasoning (why an
envelope rather than a per-line stamp, the version-bump rule, and the required unsupported-
version behaviour for a consumer). Like the rest of this module, this helper is
**boundary scaffold**: it is not wired into any CLI command, not called from anywhere, and
does not itself filter or export anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import bd, config

#: Filename bh-7jm7v.1 fixed for the public snapshot.
PUBLIC_SNAPSHOT_FILENAME = "issues.jsonl"

#: Flags that must never appear in a public-snapshot `bd export` (bh-7jm7v.1). Exported so a
#: caller's own tests can assert against the same list instead of restating it.
FORBIDDEN_EXPORT_FLAGS = ("--all", "--include-memories", "--include-infra")

#: Flags that would route the invocation past this one hive (bh/`bd` cross-hive routing).
FORBIDDEN_ROUTING_FLAGS = ("-a", "--all", "-r", "--hive", "--global")

#: Version of the PUBLISHED OVERLAY PAYLOAD envelope bh-7jm7v.4 decided — the wrapping
#: document a not-yet-built publish step emits, NOT `bd export`'s own JSONL wire format (that
#: has no version and is not this module's to version). Same bump rule as
#: `beadhive.jsonout`/`docs/design/config-schema-versioning.md`: a single monotonic integer,
#: bumped only when a field is removed, retyped, or re-meant (an added field does not bump
#: it). Bumping covers the envelope's own keys AND bh-7jm7v.2's per-issue field allow-list —
#: one contract, one counter, per docs/design/publish-boundary-adr.md's bh-7jm7v.4 section.
PUBLIC_SNAPSHOT_SCHEMA_VERSION = 1


class PublishScopeError(RuntimeError):
    """The requested publish target is not one hive's own checkout."""


def public_snapshot_argv(dest_dir: Path | str) -> list[str]:
    """The `bd` sub-argv for a public snapshot — bh-7jm7v.1's decided invocation, and nothing
    else. Pure (no I/O, no config, no subprocess) so the *command line* is directly assertable.

    Returns the args AFTER `bd -C <hive_root>`: `["export", "-o", "<dest_dir>/issues.jsonl"]`.
    No flag is added conditionally, so there is no branch that could grow an `--all`.
    """
    return ["export", "-o", str(Path(dest_dir) / PUBLIC_SNAPSHOT_FILENAME)]


def public_snapshot_envelope(generated_at: str, issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap already-filtered *issues* in bh-7jm7v.4's versioned publish envelope. Pure — no
    I/O, no `bd` invocation, no filtering of its own (`issues` must already be reduced to the
    bh-7jm7v.2 field allow-list; this function does not enforce that).

    Mirrors `beadhive.jsonout.envelope()`'s house convention — a flat top-level
    ``schema_version`` plus a subject key naming which contract it versions, merged in FRONT
    of the payload rather than nested — but is not built from `jsonout.envelope()` directly:
    that helper's ``command`` key names a CLI command, and this artifact is not produced by
    one (it is emitted by a not-yet-built OUT-OF-repo publish step). ``artifact`` is this
    envelope's equivalent subject key, naming the contract for the same reason `command` does
    there: the epic's own framing describes more than one thing eventually published
    ("the payload and the widget bundle"), so a bare version integer with no subject would be
    ambiguous the moment a second published artifact exists.

    Returns ``{"schema_version": ..., "artifact": "bead-snapshot", "generated_at": ...,
    "issues": [...]}``. ``issues`` is a materialized JSON array, not a JSONL stream — see
    docs/design/publish-boundary-adr.md's bh-7jm7v.4 section for why this envelope, not a
    per-line stamp on `.1`'s JSONL, is the right shape for this artifact's consumers.
    """
    return {
        "schema_version": PUBLIC_SNAPSHOT_SCHEMA_VERSION,
        "artifact": "bead-snapshot",
        "generated_at": generated_at,
        "issues": issues,
    }


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
