# Config schema versioning ADR — `bh` (Beadhive)

> Status: **decided.** The `bh` config (`~/.beadhive/config.yaml`) carries a **single monotonic
> integer** `schema_version`, starting at **1** (the first official version). Migrations — when an
> engine is later built — are keyed to a **linear revision chain**, not a two-axis semver.
> Companion work: the config-validation epic (schema model, `bh config validate`, `bh config
> schema`, lightest version detection, agentic-update offer) and a deferred **migration-engine**
> bead that this ADR's linear-chain design is written for.

## Context

A user's config can predate the `ws`→`bh` rename (`~/.ws/config.yaml`, `WS_*` env, `otel.rig`,
`git_workspace.rig_match`) and `bh` has no way to detect or communicate that. We are introducing a
`BeadhiveConfig` pydantic-settings model as the schema source of truth and a `schema_version`
stamp. Two questions had to be settled: **what shape is the version number**, and **how do future
migrations key off it**.

pydantic-settings itself offers **no** schema-versioning mechanism — no version field, no
migration hook, no major/minor convention (confirmed against its docs). The version field and its
semantics are entirely ours to define.

## Decision

**`schema_version` is a single monotonic integer.** The current value lives on the module as
`SCHEMA_VERSION` (= 1). Comparison is one-dimensional: `stored < SCHEMA_VERSION` ⇒ "needs
attention." No major/minor, no patch.

**Migrations are keyed to a linear revision chain.** A future migration engine registers ordered
steps `1→2, 2→3, …` and walks `stored..CURRENT`, applying each. This mirrors every real config/DB
migration system (Alembic, Django, Rails, Flyway) — all linear revision sequences, never a 2-axis
semver, because a migration step either transforms bytes or it does not; there is no such thing as
a "minor migration."

**The bump rule (this is what keeps the single int honest):**

- Bump `SCHEMA_VERSION` **only** for a change that needs a transform — a rename, a removal, a
  type/semantic change. That bump comes with a registered migration step.
- An **additive field with a sane default does NOT bump** the version. An old config that omits it
  still validates and behaves correctly because the default *is* the fill-in; there is nothing to
  migrate. Bumping a version for it would mint a delta the engine must be taught to treat as a
  no-op — negative work.

**Schema design targets rare bumps.** Because additive-with-defaults is free, the schema is
designed to *absorb growth without a bump*: new knobs arrive as optional fields with sane
defaults, and a section that grows complex is modeled as a **nested pydantic-settings sub-model**
(with `env_nested_delimiter="__"` for `BH_SECTION__KEY` overrides and, where useful,
`nested_model_default_partial_update=True` so a partial override merges with defaults rather than
replacing the whole sub-model). A version bump should be a genuinely rare event reserved for
breaking change, not the routine cost of adding a setting.

## Options considered and rejected

### Major-minor (semver) — NO-GO

`major` = needs migration, `minor` = additive/optional. Rejected: the `minor` axis carries no
behavior. The changes it would track (additive-with-defaults) are exactly the ones needing **no**
migration, so the engine would have to treat every minor delta as a no-op. The one real signal
minor could advertise — "new optional knobs exist since you last looked" — is a **discoverability**
concern already served by `bh config schema` and by diffing present keys against the model's full
key set. Encoding discoverability in the version integer duplicates that at the cost of a second
axis the engine must reason about.

### No version field at all — NO-GO

Leaves `bh` unable to distinguish a fresh config from a stale one, which is the originating
problem. A stamp is the minimum needed to detect staleness and (later) drive an engine.

## Consequences

- Today: no migration engine is built. The unversioned→1 jump is handled by the **lightest
  detection** (one warning on stale/missing version) plus the **agentic-update offer** from
  `bh config validate` — a coding agent rewrites the file to v1. This is deliberate while the user
  count is low.
- Future: the migration-engine bead builds the linear walker. It may retroactively define a
  `None→1` step if automating the unversioned jump ever becomes worth it; nothing about today's
  design blocks that.
- Ongoing: reviewers hold the bump rule — a PR that adds an optional field with a default must
  **not** bump `SCHEMA_VERSION`; only a breaking change does, and it lands with its migration step.
