# Setup Guide ADR — where it lives, what it refuses to do, and the order it teaches

> Status: **decided**, 2026-08-07, against `bh` 0.8.4 · Epic: **bh-0olv9**
> (agent-guided install) · **Supersedes:** `bh-infra-e0j` — specifically its
> "brew-first method order" child `bh-infra-e0j.4`, reversed by [Decision 2](#decision-2--the-method-order-is-installmds-derived-not-restated).
> `bh-infra-e0j.2` (marketplace consumption) survives in the infra hive by operator decision.
> **Companions:** [`../../INSTALL.md`](../../INSTALL.md) (the route axis — the source of the
> method ordering and the nix reasoning), [`../ADOPTION.md`](../ADOPTION.md) (the depth axis —
> the four-rung vocabulary the Guide's end states are scored against), and the exemplar this
> Guide is modelled on, `beadhive/infra:guides/github-app-tier-provision/`.

Records four decisions taken before any Guide content was authored, so that the next
contributor does not quietly reverse one that is only implied by the file layout.

## Context

A user should not have to *read* their way through installation. Today agent guidance for
installing `bh` stops at `INSTALL.md`'s frontmatter: `methods[]`, `verify`, `upgrade: ask`
and a three-entry `configure[]`. That is enough for a conforming installer to put the binary
on `PATH` and run three commands. There is no probing, no branch on what the machine already
has, no explanation of the fork between routes, and nothing at all past `bh config init`.

The richer shape already exists in this org and is proven in production:
`beadhive/infra:guides/github-app-tier-provision/` is an [agentguides.io](https://agentguides.io)
0.1 Guide — `GUIDE.md` with `goal_state`, per-item `performer`/`check` prerequisites,
`external_resources` and scored `end_states`; a `SKILL.md` carrying `metadata.type: guide`;
seven `steps/` each with `action`/`verify`/`interactions`/`on_failure`/`effect`; and
`scripts/check-*.sh`. Nothing equivalent exists for installing or onboarding `bh`.

Each of the four decisions below has been argued at least once already — in `bh-infra-e0j`,
in `INSTALL.md`'s own comment blocks, or in this epic's design. They are recorded here because
each is the kind of call that reads as an oversight rather than a choice, and so gets reversed
by the next contributor who notices it.

## Decision 1 — the Guide ships from the `beadhive` repo, via the existing assets mechanism

The Guide is authored at `src/beadhive/assets/guides/setup/` and ships in the wheel through
the packaging path that already exists. **Nothing is added to the build.**

`[tool.hatch.build.targets.wheel] packages = ["src/beadhive"]` already carries every
non-Python file under that tree, which is how `AGF-hint.md`, `claude-settings.json`,
`opencode.json` and `opencode-plugins/` reach an installed user today. `config.py` already
resolves them with `files("beadhive.assets") / name`, so the Guide is reachable by the same
call with no new resolution code. The one thing this decision demands in return is that
**wheel inclusion is verified by unpacking a built wheel**, not by reading `pyproject.toml`:
an asset that does not ship is invisible until a user installs, which is the failure mode the
skeleton bead exists to prevent.

**Rejected — keep the Guide in the `beadhive/infra` hive, next to the existing exemplar.**
This is what `bh-infra-e0j` actually did, and it is the first of the two reasons that epic is
being re-filed rather than resumed. It loses because *every* deliverable is in this repo — the
asset tree, the `bh setup guide` verb that exports it, the `INSTALL.md` `configure[]` delta,
the `docs/ONBOARDING.md` sweep — and because a Guide that teaches you to install `bh` but is
not carried *by* `bh` has to be fetched before it can be run, which is the problem it exists
to solve.

**Rejected — a force-include or a second packaging path (`MANIFEST.in`-style, or a
`[tool.hatch.build.force-include]` block).** It loses on cost against a mechanism that is
already load-bearing for four shipped assets: a second path is a second thing to keep correct,
and its first failure mode is silent.

## Decision 2 — the method order is `INSTALL.md`'s, derived not restated

The Guide teaches **managed path first, PyPI as a labelled fallback, Homebrew last**, and it
teaches that order *because `INSTALL.md` declares it* — `methods[]` is ordered by preference
and its first entry is the recommendation. The Guide's route step reads that ordering as its
input; it does not carry a second copy of the reasoning.

The reasoning already lives in `INSTALL.md`'s comment block above `methods:` — why the managed
path leads (it is the only route that also installs and *pins* the four tools `bh` drives),
why it is `kind: script` rather than excluded like Docker, why the version tag is pinned rather
than a branch ref, and why Homebrew is last (it compiles `pydantic-core`, `cryptography` and
`rpds-py` from source unless a bottle is already published). **That block is the citation, not
a summary to be copied.** A duplicate in the Guide would be correct on the day it was written
and wrong on the day `INSTALL.md` next changes, and the Guide is the copy a user runs.

**Rejected — brew-first, per `bh-infra-e0j.4` ("brew-first method order").** This decision
**explicitly reverses it.** That bead was filed in July against a design whose flagship line
was `brew install beadhive/tap/beadhive`; v0.8.0 demoted Homebrew to *last* for the
source-compilation reason above and promoted the managed nix path to method 1. The ordering
`bh-infra-e0j.4` would install is inverted relative to what the project now ships, so it does
not survive into this epic.

**Rejected — restating the ordering and its rationale inside the Guide so the Guide is
self-contained when exported to `~/.beadhive/guides/setup/`.** Self-containment is real value
and this is the closest call of the four. It loses because the two copies drift *silently*:
`INSTALL.md` is edited when a route changes, the exported Guide is a stale copy on a user's
disk, and the divergence surfaces as an agent confidently recommending a demoted route. The
Guide instead names the route it is taking and links `INSTALL.md` for the argument.

## Decision 3 — the Guide does not install nix; it offers the command and waits

**This is a decision, not an omission.** Installing nix needs root — it installs a system
daemon, and on macOS creates an encrypted APFS volume for `/nix`. That is a human step, not
something an install agent performs unattended on a machine it was invited onto to install a
CLI. The Guide therefore *presents* the installer command, explains what it will do to the
machine, and **stops until a human runs it or declines**.

`INSTALL.md` already made exactly this call and for exactly this reason: its `install:`
frontmatter deliberately omits nix while the prose below covers it. The comment there also
records the consequence, which is the part that makes the refusal safe rather than a dead end
— if nix is absent, the managed command fails immediately with `nix: command not found`, and a
conforming agent **falls through to the PyPI methods**. "Cannot install nix" (or "will not
right now") is precisely who the PyPI route is for. The Guide's route step inherits that
fall-through rather than reimplementing it.

Note the scope this leaves: declining nix costs the user rung 3 (the pinned toolchain), not
rung 1. Per [`ADOPTION.md`](../ADOPTION.md), rung 3 is **orthogonal** — takeable later, or
never — so an agent that stops at "nix declined → PyPI" has not blocked the user's path to a
working loop, and the Guide says so.

**Rejected — run the nix installer under `sudo` on the user's behalf when they consent once.**
It loses on the shape of the consent, not on the permission: a single yes/no cannot be
informed consent for a system daemon plus an APFS volume plus uninstall receipts, and an agent
that takes root once has taught the user that agents take root. `INSTALL.md`'s prose already
carries the command and the `/nix/nix-installer uninstall` escape hatch; a human running it
from there reads both.

**Rejected — drop the managed route from the Guide entirely, so it only ever teaches PyPI.**
It loses because the managed path is the *recommended* route (Decision 2) and the only one
that pins `bd`, `dolt`, `gh` and `git-workspace` together. A Guide that never mentions it
would quietly re-rank the routes — the same drift Decision 2 is guarding against, arrived at
by silence instead of by copying.

## Decision 4 — modelled on the infra exemplar, in the agentguides.io 0.1 schema family

The Guide uses the same artifact type as `beadhive/infra:guides/github-app-tier-provision/`:
`GUIDE.md` against `agentguides.io/schemas/0.1/guide.schema.json`, `SKILL.md` against
`skill-guide-extension.schema.json` with `metadata.type: guide` and
`metadata.guide.entry: GUIDE.md`, and numbered `steps/` against `step.schema.json`. The two
should be recognisably the same kind of thing on sight.

The payoff is **graceful degradation, declared in the artifact rather than hoped for**: a
Guide-aware harness loads `GUIDE.md` and walks the steps with their `verify`/`on_failure`
semantics; a harness that only supports plain Skills reads `SKILL.md`, and the Guide tells it
in one sentence to treat `GUIDE.md` and `steps/` as a linear runbook. Both harnesses get a
usable install; only the second loses the branching.

**Rejected — a plain Claude Code Skill (`SKILL.md` plus prose), no Guide schema.** It loses
because the two things this epic is *for* — probing before acting, and branching on what the
machine already has — are exactly what a prose Skill cannot express in a checkable way. There
is no `verify`, no `on_failure`, no `effect`, and no scored `end_states`, so "did this run
succeed?" becomes an opinion.

**Rejected — a bespoke step format (a YAML manifest of our own) tuned to `bh`'s install.**
It loses on the one property that matters most here: a format only `bh` understands is a
format only `bh` can walk, and the target reader is a *third-party* harness setting up a
machine that does not have `bh` on it yet. The 0.1 schemas are already what `INSTALL.md`'s
own frontmatter is validated against in this repo, so this is the family we are in.

## Where this Guide deliberately differs from the exemplar

Recorded so the differences read as choices rather than as an incomplete copy:

- **`rollback_strategy: none`**, where the infra Guide needs `best-effort`. Every step of the
  setup Guide is re-runnable and non-destructive; there is no half-registered GitHub App to
  clean up. Re-running the Guide *is* the recovery.
- **A successful end state that never reaches the goal.** The infra Guide's mid-score exit is
  a partial provision; ours (`installed-unwired`, 0.5) is a user who installed `bh`, verified
  it, and stopped. That is a legitimate finish, not a failure, and scoring it as one would
  make the Guide push past a "no thanks".
- **`external_resources` are URLs, not in-Guide mirrors.** The infra Guide mirrors its tier
  table into `references/`. Ours points at `INSTALL.md` / `ADOPTION.md` / `ONBOARDING.md` on
  the canonical remote — a direct consequence of Decision 2: a mirror is a copy, and a copy
  drifts.

## Limitations

- **Decision 1 is verified, not asserted, only if the wheel check stays wired.** The guard is
  a test that builds a wheel and unpacks it; if that test is ever weakened to inspect
  `pyproject.toml` instead, the decision loses its evidence.
- **Decision 2 buys freshness at the cost of self-containment.** An exported Guide on a
  machine with no network cannot follow its own citation. Accepted: the route step still names
  the route it is taking and the one-line reason, so the link is depth, not the whole argument.
- **Decision 3 means the Guide cannot deliver rung 3 unattended, ever.** That is the intent,
  and it is why rung 3 is orthogonal in `ADOPTION.md` rather than a step on the way to rung 1.
- **Decision 4 binds us to a 0.1 schema family.** These schemas are pre-1.0 and may move; the
  mitigation is that all three are declared per-file by `$schema` comment, so a version bump
  is a mechanical edit and a re-validation rather than a rewrite.
- **The guide-level `performer` enum is `agent` | `human` only.** `either` is valid on a
  *step*, not on a `prerequisite` — checked against the 0.1 `guide.schema.json`, whose
  `Prerequisite.performer` omits it. Prerequisites that genuinely admit either performer are
  written as `agent` with the human fallback stated in the description.

## Consequences for filed work

- `bh-infra-e0j.4` (brew-first ordering) is **dead**, superseded by Decision 2 rather than
  merely deprioritised. Do not resurrect it as "restore brew as the flagship".
- `bh-infra-e0j.2` (verify `bh@beadhive` marketplace consumption) is untouched by this ADR and
  stays in the infra hive — the marketplace is genuinely that hive's surface.
- Every step bead under `bh-0olv9` inherits Decision 3's posture: a step that would take root
  on the user's behalf is out of scope by decision, not by omission.
- Reopen Decision 1 only if `src/beadhive/assets/` stops shipping in the wheel; reopen
  Decision 4 only if the agentguides schemas reach 1.0 with a breaking change.
