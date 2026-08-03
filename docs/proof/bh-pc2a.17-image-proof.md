# Proof gate — locally-baked image, bh-pc2a.17

The operator's hard gate: nothing deploys anywhere until a locally-baked image is proven to work
with all bundled components **together**. This file is that proof as an artifact rather than a
claim in a chat log — re-runnable, and honest about what is still unproven.

Scope is the **combination**. A component reporting `--version` proves nothing about whether `bh`
can drive it.

## Run under test

| | |
|---|---|
| Date | 2026-08-03 |
| Image | `beadhive/agent:dev`, target `agent` |
| Recipe | `just image-local agent` — native arch, `--load`ed, bh from the working tree |
| `build_sha` | `eb94e62aa09160dc658504de3883c86a54c18024` (bh-pc2a.29 merge) |
| bh provenance | `local-wheel:beadhive-0.7.1-py3-none-any.whl` |
| Host | colima, arm64, buildx 0.36.0 |
| Stack | `docker compose up -d` from this worktree |

Native arch deliberately, per this bead's design note: QEMU proves little here and is slow.

## Layer results

| # | Layer | Verdict |
|---|---|---|
| 1 | Component presence + version, cross-checked against the manifest | **PASS** |
| 2 | bh drives bd: config init, onboard, `bh hive ready` | **PARTIAL** — coupling proven, `hive ready` not green |
| 3 | Dolt store on the intended volume, survives `down && up` | **PASS** |
| 4 | git + gh + git-workspace: authenticated clone, workspace update | **PASS** (read paths) |
| 5 | Harness reachability: authenticated `claude` / `codex` | **NOT RUN** — needs harness credentials |
| 6 | Worktree created and pruned on the scratch volume | **PARTIAL** — create/remove pass, `prune` declines |

## Layer 1 — components against the manifest

`bh setup check` reads `/etc/beadhive/image-manifest.json` and skips probing entirely
(`Reading image manifest (/etc/beadhive/image-manifest.json) — skipping probes`). All 14 present,
each version matching its manifest entry:

| Component | Version | Source | Manifest | `bh setup check` |
|---|---|---|---|---|
| git | 2.39.5 | `apt:debian-bookworm` | ✓ | ✓ |
| python | 3.12.13 | `docker:python:3.12.13-slim-bookworm` | ✓ | ✓ |
| uv | 0.12.1 | `docker:ghcr.io/astral-sh/uv` | ✓ | ✓ |
| bh | 0.7.1 | `local-wheel:beadhive-0.7.1-py3-none-any.whl` | ✓ | ✓ |
| bd | 1.1.2 | `github:gastownhall/beads` | ✓ | ✓ |
| dolt | 2.2.3 | `github:dolthub/dolt` | ✓ | ✓ |
| gh | 2.97.0 | `github:cli/cli` | ✓ | ✓ |
| git-workspace | 1.10.1 | `crates.io:git-workspace` | ✓ | ✓ |
| jq | 1.8.2 | `github:jqlang/jq` | ✓ | ✓ |
| yq | 4.53.3 | `github:mikefarah/yq` | ✓ | ✓ |
| just | 1.57.0 | `github:casey/just` | ✓ | ✓ |
| node | 24.18.1 | `nodejs.org` | ✓ | ✓ |
| codex | 0.146.0 | `npm:@openai/codex` | ✓ | ✓ |

**`claude` is deliberately absent** as of bh-pc2a.36 — the run above predates it and listed 14.
Its package declares `SEE LICENSE IN README.md` rather than an SPDX identifier, so baking it made
anyone publishing this image a redistributor of proprietary software. The image now ships the
runtime and `bh harness install claude`. The manifest no longer lists it, which matters more than
it looks: in-image `bh setup check` trusts the manifest **instead of** probing, so a component
listed but not shipped is a lie the check structurally cannot catch.

`bh setup check` exits 0 unattended — no `BH_SKIP_SETUP_CHECK=1` bypass anywhere in this run.

**This layer only passes on a freshly baked image.** See "How this run first failed" below; it is
the single most likely way a re-run goes wrong.

## Layer 2 — bh drives bd

The historically-breaking combination. Proven against a throwaway repo with **no remote**, so no
step could write anywhere off the container:

| Check | Result |
|---|---|
| `bh config init` on an empty volume | ✓ writes config.yaml + templates |
| `bh setup check` | ✓ rc=0 off the manifest |
| `bh hive onboard` preflight | ✓ 10/10 checks pass |
| `bh hive onboard` steps | ✓ 10/10 run (resolve → bd-init → register → hub-sync → footprint) |
| bd live under the hive | ✓ `bh work list` responds; bead created and read back |
| dolt store created | ✓ `<hive>/.beads/embeddeddolt/throwaway/.dolt` |
| `bh hive ready` green | ✗ **not reached** |

`bh hive ready` cannot go green here, for a reason that is **not** a container defect:

```text
✗ furnish-needs-ownership: no confirmed push access to probe/throwaway —
  only the hive's owner may furnish it
```

Correct refusal — the test repo was deliberately remote-less, so ownership is unprovable. Closing
this needs a repo the operator owns, and furnishing **writes** (it commits files). Tracked on
bh-pc2a.10.

One correction to the record: `bh hive onboard` does **not** require a real remote. bh-pc2a.10
previously asserted it did; onboarding a remote-less local repo works.

## Layer 3 — dolt store placement and survival

Both stores land on their intended volumes:

| Store | Path | Volume |
|---|---|---|
| HQ / hub | `~/.beadhive/hub/.beads/embeddeddolt/hub/.dolt` | `bh-hq` |
| Hive | `/workspace/gh/probe/throwaway/.beads/embeddeddolt/throwaway/.dolt` | `bh-workspace` |
| **dolt global config** | `~/.dolt` | **none** — bh-pc2a.32 |

Across a real `docker compose down && up` (**without** `-v`):

```text
config.yaml       : SURVIVED      hive dolt store   : SURVIVED
hq store          : SURVIVED      managed clone     : SURVIVED
worktree (scratch): SURVIVED
```

Survival is proven by reading application data back **out of dolt** after the recreate, not by
checking that files exist:

```text
○ throwaway-7no ● P2 survival canary
Total: 1 issues (1 open, 0 in progress)
```

Volumes size independently, which is the point of splitting them — `bh-hq` 1.347MB,
`bh-workspace` 1.361MB, `bh-worktrees` 74B, `bh-harness` 0B.

## Layer 4 — git, gh, git-workspace

Run with `GH_TOKEN` supplied from the host (bh-pc2a.29's headless path), **read-only operations
only**. Every positive has a matching negative control, so a pass cannot be a false positive:

| Check | Negative control | With token |
|---|---|---|
| `gh auth status` | — | ✓ active account from `GH_TOKEN` |
| `gh api rate_limit` | — | ✓ `limit=5000` (unauthenticated would be 60) |
| clone a **private** repo | ✓ refused: `could not read Username` | ✓ cloned |
| `git-workspace lock` | ✓ refused: `Missing GITHUB_TOKEN` | ✓ 8 repos locked |

A public-repo clone was run first and is **not** counted as evidence — it succeeds without any
credential. Only the private-repo clone proves the credential path.

`gh` needs no `gh auth login` at all: it reads `GH_TOKEN`, then `GITHUB_TOKEN`.

**Not proven, and it is a real gap:** HQ. `hq.py::_remote_urls` hardcodes SSH for both the git
remote and bd's `git+ssh://` dolt transport, and the image has no `known_hosts`, so a
token-authenticated container cannot sync HQ at all. Tracked on bh-pc2a.30.

## Layer 5 — harness reachability

**Not run.** Needs authenticated harnesses in-image, deferred with the rest of the write path.

**Restated by bh-pc2a.36**, which changed what this layer even means. It is no longer
authenticate-then-answer but **install → authenticate → answer**: `claude` is not shipped, so the
first step is `bh harness install claude`. That install path IS proven — installed as the non-root
agent user at the image's pinned version, resolving on `PATH` in a login shell, idempotent on
re-run, and surviving `docker compose down && up` on the `bh-harness` volume. What remains unproven
is the part that always was: that an authenticated harness returns a correct answer.

`codex 0.146.0` is present (layer 1) — presence is not reachability, and this file does not claim
it is.

## Layer 6 — worktree lifecycle on the scratch volume

| Check | Result |
|---|---|
| `bh worktree add --dry-run` | ✓ plans a `/worktrees`-rooted path |
| `bh worktree add --branch probe-wt` | ✓ created at `/worktrees/gh/probe/throwaway/probe-wt` |
| on the scratch volume | ✓ `/worktrees → /docker/volumes/bh-worktrees/_data` |
| survives `down && up` | ✓ |
| `bh worktree rm` | ✓ removed; `/worktrees` empty again |
| `bh worktree prune` | ✗ declines — `merged-orphan`, "not SAFE" |

The prune refusal is defensible: a bead-less worktree cannot be proven landed, and `rm` is the
documented escape hatch. It is recorded because `bh-worktrees` is documented as "scratch,
prunable, capped", and a class of worktree `prune` never reaps sits against that. Tracked on
bh-pc2a.34.

## How this run first failed — read this before re-running

The gate failed immediately, and **not for a gate reason**. `beadhive/agent:dev` was a stale bake:

```text
beadhive/agent:dev  ->  bh 0.6.0        (no manifest reader)
beadhive/core:dev   ->  bh 0.7.1
```

bh 0.6.0 falls back to probing, the probe demands a container runtime, and the result is:

```text
✗ missing: docker
```

which then gates off `bh hive` and `bh bd` entirely — an inert container whose only clue points at
installing docker or mounting the socket, i.e. exactly what bh-pc2a.6 refuses. `just image-local`
defaults to `target="core"`, so core moved forward and agent silently did not.

**Re-bake both targets before trusting any result here.** Tracked on bh-pc2a.33.

## Outstanding before this gate can close

| Blocker | Bead |
|---|---|
| `bh hive ready` green — needs an owned repo; furnishing writes | bh-pc2a.10 |
| Layer 5 harness reachability — needs harness credentials | bh-pc2a.10 |
| HQ unreachable from a token-authenticated container | bh-pc2a.30 |
| Image drift makes layer 1 unreliable to re-run | bh-pc2a.33 |

Layers 1, 3 and 4 are proven and need no further work. Layers 2 and 6 are proven except for the
rows called out above. Layer 5 is untested and is not claimed.
