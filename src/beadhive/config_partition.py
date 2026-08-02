"""Fleet vs host key partition (bh-e0y8.3) — the explicit split the future fleet+host merge
(bh-e0y8.5's ``config.py:load()``) needs so a host can never silently diverge on a value that
is supposed to be identical across the whole fleet.

Two kinds of truth are mixed together in one flat :class:`~beadhive.config_schema.BeadhiveConfig`:

  * **FLEET** — identical everywhere; lives in the HQ repo's ``fleet.yaml`` (bh-e0y8's design).
  * **HOST**  — deliberately divergent per machine; lives in a per-host manifest
    (``hosts/<host_id>.yaml``).

Rule of thumb: a key is FLEET when every host should see the same value (org policy,
naming/behavior conventions, cross-hive governance) and HOST when the value is inherently
local — a filesystem path, which local daemon/binary happens to be installed, or a
machine-specific tuning knob.

This module does NOT implement the merge — that is bh-e0y8.5's job, which consumes
:data:`FLEET_KEYS` / :data:`HOST_KEYS` (or :func:`partition_of`) as ready-made data. It only
*defines* the split, as plain frozensets, so it is testable and inspectable rather than
hardcoded in `if`/`else` branches somewhere in the merge path (the acceptance bar this bead
holds).

Granularity: most top-level sections sit wholly on one side, but a few split — only
``worktrees.path``/``hq.remote`` diverge from an otherwise-FLEET section; only the two
``work.dispatch`` budget fields diverge from an otherwise-FLEET ``work``. :data:`HOST_PREFIXES`
and :data:`FLEET_PREFIXES` are both checked by :func:`partition_of`, longest (most specific)
match wins, so a narrow carve-out inside a wider section overrides that section's default.

Only *leaf* keys are partitioned (the real settable/mergeable values — same granularity
``bh config get/set`` operates on). A container row like ``otel`` or ``work.dispatch`` is a
namespace, not an independent value to place on a side; its leaves (``otel.enabled``, …) carry
the classification instead. See ``tests/test_config_partition.py`` for the walk-the-schema
assertion that every current leaf lands on exactly one side — the enforcement mechanism that
keeps this partition honest as :mod:`beadhive.config_schema` grows.
"""

from __future__ import annotations

from .config_schema import iter_schema_fields

FLEET = "fleet"
HOST = "host"

# ---- host-local carve-outs ------------------------------------------------------
# A key (or everything nested under it) that is inherently about THIS machine: a filesystem
# path, a locally-installed binary/daemon, or a per-operator/per-host tuning knob — never
# meant to be identical across the fleet.
HOST_PREFIXES: frozenset[str] = frozenset(
    {
        "worktrees.path",  # persistent worktree root: a path on THIS disk
        "otel",  # points at THIS host's collector
        "work.identity",  # git identity + signing stamped per machine/seat
        "work.dispatch.max_beads_per_session",  # dispatch budget: THIS host's compute
        "work.dispatch.auto_budget",  # dispatch budget: THIS host's compute
        "hq.remote",  # derives from the identity resolved on THIS host
        "dolt",  # local Dolt container runtime (colima/docker/podman/none)
        "git_workspace",  # git-workspace integration: local paths + local tool presence
        "log",  # diagnostics verbosity/format: an operator/terminal preference
        "observaloop",  # routes to a LOCAL observaloop container (coupled to otel)
        "harness",  # which agent CLI is installed on THIS host
        "archive",  # local archive directory + retention
        "backup",  # local backup-root retention (keep-N/size-cap): THIS host's disk budget
        "metadata",  # local metadata-cache tuning (a derived, host-local cache)
        "orca",  # local orca state path + local tool presence
        "hitch",  # local agent-hitch checkout path + local tool presence
    }
)

# ---- fleet-wide shared truth -----------------------------------------------------
# Identical everywhere: org policy, naming/behavior conventions, cross-hive governance.
FLEET_PREFIXES: frozenset[str] = frozenset(
    {
        "schema_version",
        "delimiter",
        "providers",
        "orgs",
        "exclude",
        "dimensions",
        "passthrough",
        "managed_repos",
        "worktrees",  # templates/init rules/toolchain — only .path (above) is host
        "work",  # "work defaults" — only .identity + the two dispatch budgets are host
        "release",
        "claude",
        # host-lease TTL/renewal (bh-ytbb.6): FLEET, and load-bearingly so. Two hosts that
        # disagreed about when a lease expires would disagree about who may write — one would
        # see a free hive while the other still considered itself primary. Per-host variation
        # goes through the manifest `role` (which SCALES this baseline), never through a
        # per-host override of the baseline itself.
        "host",
    }
)

assert HOST_PREFIXES.isdisjoint(FLEET_PREFIXES), (
    "a prefix cannot be both fleet and host — fix HOST_PREFIXES/FLEET_PREFIXES"
)

# ---- override allowlist -----------------------------------------------------------
# The narrow subset of FLEET keys a host manifest MAY still override; everything else is
# fleet-only (a host manifest setting an unlisted fleet key is a config error, not a quiet
# local override). Empty today — no fleet key needs a per-host escape hatch yet; extend
# deliberately, one documented key at a time, when a concrete need shows up.
FLEET_HOST_OVERRIDE_ALLOWLIST: frozenset[str] = frozenset()


def _prefix_match_len(path: str, prefixes: frozenset[str]) -> int:
    """Length of the longest prefix in `prefixes` matching `path` (exact, or a dotted
    ancestor — `path == prefix` or `path.startswith(prefix + ".")`); -1 when none match."""
    lengths = [len(p) for p in prefixes if path == p or path.startswith(p + ".")]
    return max(lengths, default=-1)


def partition_of(path: str) -> str | None:
    """FLEET or HOST for a dotted schema `path`; the longest (most specific) matching
    prefix between :data:`HOST_PREFIXES` and :data:`FLEET_PREFIXES` wins, so e.g.
    `work.identity.name` resolves HOST despite `work` itself defaulting FLEET.

    Returns None when NEITHER side claims the key — an unclassified schema field. Callers
    (and the walk-the-schema test) treat that as a failure rather than a silent default, so
    a newly added `config_schema` field forces a deliberate fleet/host call instead of
    drifting in unnoticed."""
    host_len = _prefix_match_len(path, HOST_PREFIXES)
    fleet_len = _prefix_match_len(path, FLEET_PREFIXES)
    if host_len < 0 and fleet_len < 0:
        return None
    return HOST if host_len >= fleet_len else FLEET


def is_host_overridable(path: str) -> bool:
    """Whether a host manifest may override FLEET key `path` (the AC's explicit
    allowlist — 'everything else is fleet-only'). Meaningless (always False) for a key
    that is already HOST; callers should only consult this for a FLEET key."""
    return _prefix_match_len(path, FLEET_HOST_OVERRIDE_ALLOWLIST) >= 0


def schema_leaf_paths() -> list[str]:
    """Every dotted key :func:`beadhive.config_schema.iter_schema_fields` declares that does
    NOT itself recurse further — the real settable/mergeable values. A container row (e.g.
    `otel`, `work.dispatch`) is excluded: it is a namespace, not an independent value: its
    leaves (`otel.enabled`, …) carry the fleet/host classification instead.

    Collection-member rows (`managed_repos[].furnish`) are excluded for the same reason: they
    describe the SHAPE of a dynamically-keyed member, not a value anyone sets or merges. The
    collection itself (`managed_repos`) stays the leaf that carries the classification.

    Walks the LIVE schema (no hand-maintained list to drift from it) — same source
    `config_schema.known_keys()` uses."""
    paths = [f.path for f in iter_schema_fields() if "[]" not in f.path]
    branch_paths = {p for p in paths if any(o != p and o.startswith(p + ".") for o in paths)}
    return [p for p in paths if p not in branch_paths]


# Materialized partition of the CURRENT schema's leaves — the ready-made data bh-e0y8.5's
# merge consumes directly (`if key in HOST_KEYS: host value wins`, else fleet's).
FLEET_KEYS: frozenset[str] = frozenset(p for p in schema_leaf_paths() if partition_of(p) == FLEET)
HOST_KEYS: frozenset[str] = frozenset(p for p in schema_leaf_paths() if partition_of(p) == HOST)
