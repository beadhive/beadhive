# Design

The current conceptual model and the reasoning behind it.

## The problem

One person (or their agents) works across dozens of repos spread over multiple git hosts.
Issues should live *with* each repo, but you also want one place to ask "what's ready to
work on anywhere?" — without a central service to run, and without issue data leaking into
code branches.

## Hives: one beads DB per repo

Each repo is a **hive** — its own beads database, embedded as Dolt under the repo's
gitignored `.beads/`. All authoritative writes happen in the hive. Issues are named by the
hive's **prefix** (`ag-infra-1`, `workspace-7`).

Why per-repo rather than one big DB: ownership and sync follow the repo, repos can move
between hosts/orgs independently, and most issue work is within a single repo anyway.

## Hosting: on the repo's own git remote

Beads stores issue history under **`refs/dolt/data`** on the *same git remote as the code* —
a separate ref namespace that never touches `refs/heads/*` (branches/PRs). So:

- There is **no database to provision**. `bd dolt push` publishes a hive's data; a fresh
  clone runs `bd bootstrap` to pull it.
- Backup rides on wherever the repo is mirrored (verify the mirror carries `refs/dolt/data`,
  else add an explicit backup Dolt remote).
- The optional local [Dolt server](DOLT.md) is *not* part of this path — it's opt-in infra
  for a shared/central backend, never required.

## Prefixes: a stable handle from the stable identity

The prefix is a permanent, short handle (think a namespace). It's derived from the **stable**
part of identity — `org` + `repo` — and deliberately **excludes `provider`**, the dimension
most likely to change (github→gitea). Provider lives in a label, so a host switch is a label
edit, not a prefix migration (prefix changes are expensive).

Derivation and the per-repo *kind* (org-native / personal / prototype / fork) are covered in
[HIVES](HIVES.md). The registry enforces global prefix uniqueness.

## Labels: identity you can filter on

`bd list` has no prefix filter, so labels are how you slice the aggregated view. Every issue
carries a `provider:`/`org:`/`repo:` **triplet** (the hive's *registered* identity, applied
automatically by `bh bd create`). Orthogonal **dimensions** (`component:`, `phase:`,
`tag:`, …) are open or closed sets. Labels are consistency-checked against the registry, not
treated as the issue's "home" (the hive is the home). See [LABELS](LABELS.md).

## Identity over time

The guiding principle: **the prefix is stable; labels carry current truth.** Most identity
changes are therefore label edits, not data surgery:

- **Mirror direction change** — not an identity change; `provider:` still names the primary.
- **Primary host change** (github→gitea) — edit the `provider:` label, repoint the remote.
- **Org transfer** — the repo and its `refs/dolt/data` move together (one DB relocates);
  edit the `org:` label, keep the prefix. Required-org consistency doesn't retro-apply
  (migrated-in repos are grandfathered).
- **Prototype graduation** — keep the bare prefix; drop `tag:prototype`.

Beads has no native "moved-to"/"supersedes" type, so lineage is modeled with a `related` dep
plus a close reason. Clean prefix cutovers are export → rewrite-JSONL → import (done early).

## The hub: a cross-hive view without a server

A dedicated beads DB at `~/.ws/hub` aggregates every registered hive via beads' multi-repo
hydration (`bd repo add` + `bd repo sync`). It's a **read cache** — authoritative data stays
in each hive. Cloned hives are added by local path; **uncloned** hives are fetched into a
minimal-clone cache (blobless, no working tree) so you can browse a hive's issues without
checking out its code. This means `bh` is useful on a machine with nothing cloned. See
[HUB](HUB.md).

## git-workspace: the optional substrate

[orf/git-workspace](https://github.com/orf/git-workspace) clones a fleet of repos into a
`<provider>/<org>/<repo>` layout. `bh` derives hive identity from that layout, and (opt-in)
reads providers/orgs from its config so they needn't be restated. It's the source for
fleet-scale operations (`-a`/`-r` routing, the remote-cache hub). It is **optional**:
single-hive use works without it; only fleet routing and provider auto-load require it. See
[INTEGRATIONS.md](INTEGRATIONS.md).

## Boundaries & trade-offs

- **`bh` orchestrates; it doesn't reimplement.** beads owns issues/Dolt; git-workspace owns
  cloning; `bh` owns the registry, conventions, validation, and routing.
- Cross-repo dependency links are **references** between hives, not one in-DB graph — the cost
  of per-repo ownership. Accepted because cross-repo links are occasional.
- The local Dolt server and any auto-sync daemon are deliberately **out of scope** for the
  core; on-disk hives + the hub + git-native distribution are sufficient at personal scale.

## Component map

| Concern | Doc | Modules |
|---|---|---|
| config & paths | [CONFIGURATION](CONFIGURATION.md) | `config.py` |
| command surface | [CLI](CLI.md) | `cli.py` |
| onboarding & identity | [HIVES](HIVES.md) | `hive.py`, `identity.py` |
| registry, labels, validation | [LABELS](LABELS.md) | `registry.py`, `validate.py` |
| passthrough & routing | [PASSTHROUGH](PASSTHROUGH.md) | `bd.py`, `git.py`, `route.py` |
| cross-hive hub | [HUB](HUB.md) | `hub.py` |
| managed worktrees | [WORKTREES](WORKTREES.md) | `worktree.py` |
| git-workspace integration | [INTEGRATIONS](INTEGRATIONS.md) | `gitworkspace.py` |
| diagnostics | [DIAGNOSTICS](DIAGNOSTICS.md) | `doctor.py` |
| optional Dolt server | [DOLT](DOLT.md) | `dolt.py` |
| subprocess helper | — | `run.py` |
