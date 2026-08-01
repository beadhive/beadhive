# Managed harness config ADR — the pack is the source, emit is a shim, launch is ephemeral

**Status:** proposed · **Date:** 2026-07-31 · **Supersedes:** nothing ·
**Amends:** no other ADR — but materially rescopes the dotfile workstream and the
cross-platform portability spike (see [Consequences for filed work](#consequences-for-filed-work)).
**Related:** [multi-host-model-adr.md](multi-host-model-adr.md) (the host model this sits on;
note it is silent on platform, which is the gap this ADR closes),
[toolchain-declaration.md](toolchain-declaration.md), and the multi-host plan of record
(whose dotfile-scaffolding and manager-seam parts this revises)

Establishes where a harness's configuration comes from, who owns it, and what actually travels
between hosts.

## Context

bh drives agent harnesses (`harness:` is a closed set: `claude`, `codex`). Today those harnesses
read **user-scope defaults** — `~/.claude/`, `~/.claude.json` — which are hand-maintained per
machine with no declared source. Standing up a second host means recreating the first from
memory, and the failure is silent: the harness starts fine and behaves differently.

Three pieces of field evidence, gathered read-only from a reference Linux host (Debian 13; the
harness runs under a service account whose `HOME` is outside `/home`):

1. **Transport carries absolute paths that do not exist on the target.** A harness-launched
   process on that Linux host had a `PATH` of 68 entries of which **59 do not exist there** —
   including `/opt/homebrew/bin`, `/System/Cryptexes/App/usr/bin`, `/Users/<user>/Library/pnpm`,
   and ~25 `/Users/<user>/.claude/plugins/cache/…` entries. macOS configuration had reached a
   Debian box intact and dead.

2. **Version skew is already live.** The two hosts resolve different cached versions of the same
   plugin. One mechanism, two hosts, divergent state.

3. **Launch context is not platform.** The harness there is started by a **systemd service unit**
   (headless, under a virtual framebuffer) and inherits a **15-variable service environment** —
   nothing from `.bashrc`/`.profile`, no `GIT_WORKSPACE`, no `BH_*`, no `XDG_*`. A login shell on
   the same box has 24. The same gap would occur on macOS under `launchd`. Measuring config over
   SSH captures the shell environment and reports health while the harness runs on something
   else entirely.

The prior plan answered this with a dotfile manager (chezmoi) **transporting** `~/.claude`
between hosts. Evidence (1) is the direct refutation: transporting a file whose content names
host-specific absolute paths moves the paths too.

Separately, **agent-hitch** already defines a neutral **Hitch Pack** format that describes agent
resources once and projects them onto target harnesses, with a versioned `resolved-profile/v1`
conformance contract and an existing native consumer in `baml-harness`.

## Decision 1 — the pack is the source of truth; harnesses launch from bh-managed config

bh stops depending on user-scope harness defaults. Harness configuration derives from a Hitch
Pack, and harnesses launch against a **bh-managed config directory**, not `~/.claude`.

The mechanism exists and is first-class: Claude Code respects `CLAUDE_CONFIG_DIR` (changelog:
"Respect CLAUDE_CONFIG_DIR everywhere"), plus `--settings`, `--mcp-config`, `--agents`, and
`--plugin-dir` for finer control. This is a supported entry point, not a hack.

A user's personal `~/.claude` is thereby **out of scope and left alone**. bh manages bh's
harnesses; it does not adopt or rewrite the operator's own configuration. This mirrors the
managed-workspace decision: bh owns its tree even when another exists on the host.

## Decision 2 — native consumption where possible, emit as a permanent shim where not

Two consumption paths off one source:

- **Native** — a consumer reads `hitch profile resolve --json` directly, binding to the
  `resolved-profile/v1` contract. No translation, therefore no translation loss. `baml-harness`
  does this today.
- **Emit shim** — a target-specific projection for harnesses that cannot read the pack.

**Emit is permanent for `claude` and `codex`, not transitional.** Both are third-party binaries
that read their own formats (`settings.json`, `.mcp.json`, `CLAUDE.md`). No degree of pack
adoption changes that, so the shim is a standing component and must be treated as one — not as
scaffolding awaiting removal.

The consequence that matters: **emitted output is lossy by construction.** Harnesses do not
share a capability surface (Claude Code has hooks, MCP, skills, and plugins; other targets
expose different subsets), so a projection necessarily drops or approximates. That loss must be
**declared per target and verified**, not discovered in the field. This is a conformance
question, and `conformance/resolved-profile/v1` is the established pattern for answering it —
producer and consumer bind to the same fixtures so disagreement surfaces as a diff rather than
at runtime.

## Decision 3 — sync the pack, not its projections

The dotfile manager syncs the **pack** (and a pinned emitter version). It does not sync emitted
artifacts.

Three reasons, in ascending order of force:

1. **Capability-conditional fields cannot be templated.** Differences sort into three tiers:
   - *Pure content* (skills, instructions, personas, `CLAUDE.md`) — byte-portable.
   - *OS-conditional* (`~/Library` vs XDG, brew prefixes) — resolvable from static facts
     (`.chezmoi.os`/`.chezmoi.arch`), so templating suffices.
   - *Capability-conditional* — resolvable only by probing the host. Whether docker is present;
     whether there is a display (the reference Linux host runs headless under a virtual
     framebuffer); which binary provides `jq`; whether `bh` is on *this* process's `PATH`. A
     template engine knows OS, arch, and hostname — static identity. It cannot answer any of
     these. A probe is emitter logic, not template syntax.

2. **Syncing projections freezes the target set.** Emitted output is a lossy projection onto
   whichever harnesses were selected at emit time. Ship only the projection and a host cannot
   add a harness without a round-trip to the emitting host. A single agent host in practice
   carries config surfaces for ~10 harness-ish tools; one host's emit selection is not
   another's need.

3. **The neutral form is the reusable artifact.** Collapsing it into one host's answer discards
   precisely what makes the pack worth having.

**Counterargument, recorded because it is real:** one could emit every supported target on a
primary host and sync all of it. That works. It trades a runtime dependency for permanent
surface bloat — every host carrying config for harnesses it does not run — and still requires
a round-trip to add a target. Rejected on those grounds, not because it cannot work.

## Decision 4 — emit at launch, into an ephemeral config directory

For shim-side harnesses, bh resolves the profile and emits **at launch time** into an ephemeral
directory, points `CLAUDE_CONFIG_DIR` (or the harness equivalent) at it, and runs.

Nothing persistent is emitted, therefore nothing emitted can drift. The harness receives config
generated moments before it starts, on the host it starts on, with that host's capabilities
already resolved. Local emission would otherwise reintroduce the very version-skew drift
evidenced above — this is what makes it safe.

This fits bh's existing shape: bh already owns the launch. The dispatcher spawns developers and
`bh work start` provisions worktrees; emitting a config directory is the same provisioning
motion, and ephemeral-by-default matches `worktrees.ephemeral` (default `true`).

**Cost, stated plainly:** this puts agent-hitch on the critical path of every harness start. It
must be fast, and it must fail loudly — a broken emit now blocks work rather than quietly
producing bad config. That is the correct failure direction but it is a real operational
dependency, and `bh doctor` must check emitter presence and version match.

## Decision 5 — pin the emitter version in the pack

The pack declares the agent-hitch version it is emitted by. A host whose installed emitter does
not match fails a `bh doctor` check.

Without this, local emission is a drift vector rather than a drift fix: two hosts running
different emitter versions against the same pack produce different config, which is
mechanically identical to the plugin-version skew already observed across hosts.

## Verification

Emit on two hosts from the same pinned pack and diff. **Every difference must map to a declared
OS or capability dimension; anything unmapped is a defect in the pack or the emitter.** This is
sharper than the transport-damage measurement it replaces, because the expected answer is known
in advance rather than discovered.

Launch context must be probed as the harness sees it — the live process environment
(`/proc/<pid>/environ` on Linux), not a shell. Deltas caused by *how the harness was started*
are their own class and must never be counted as platform differences; doing so inflates the
figure a portability verdict turns on and misattributes a service-unit problem
(`Environment=`/`EnvironmentFile=`) to cross-platform support.

## Limitations

- **Codex coverage is thin.** agent-hitch carries substantially more claude-code implementation
  than codex. The closed `harness:` set is `claude|codex`, so the second target is a real gap,
  not a formality.
- **Secrets are out of scope here.** The macOS Keychain has no Linux equivalent; that
  substitution is a different mechanism with different trust properties and is settled by the
  existing who-decrypts decision, not by this ADR.
- **This ADR does not cover the operator's personal dotfiles.** Only bh-managed harness config.
- **Plugin marketplace state is host-local** and is not claimed as pack-managed by this ADR.

## Consequences for filed work

- **Dotfile-source and harness-declaration scaffolding** (`kickoff=pending`, nothing built) —
  rescoped, not cancelled. chezmoi remains the transport tier; what it transports changes from a
  rendered `~/.claude` tree to the pack. Its hostname-keyed template selector enumerates hosts
  rather than generalizing, and should be revisited against the three-tier split above.
- **The cross-platform portability spike** — narrows. "Can a host's functional config be
  reproduced on a different-platform host?" largely dissolves when config is generated rather
  than transported. What survives is cross-harness conformance: what each emit target loses
  relative to the pack. The existing probe remains the instrument; it now verifies generated
  output instead of measuring transport damage.
- **The dotfile-manager plugin-seam spike** — its premise shifts. The seam question was posed
  when a dotfile manager owned the harness layer; under this ADR agent-hitch owns generation and
  the manager only syncs a pack.
- **The bh-managed git workspace molecule** — unaffected and consistent: same principle of bh
  owning its own tree, applied to clones rather than config.

## Amendment 1 — verified by manual spike; the mechanism is more specific than Decision 1 assumed

**Date:** 2026-07-31. A manual end-to-end run on the reference Linux host validated most of this
ADR and corrected one detail. No beads were filed for the spike itself; its lessons are filed as
follow-on work.

**What was verified.** agent-hitch was installed from source on the Linux host (git bundle →
`uv tool install`; 9 dependencies, sub-second install — the "runtime dependency on every host"
cost in Decision 4 is small). A seat profile emitted successfully for the `claude-code` target,
producing the complete config surface for that seat.

**Correction to Decision 1.** The mechanism is more specific than "point `CLAUDE_CONFIG_DIR` at a
directory". The layering is:

1. `hitch profile build <profile> --target claude-code` produces the base output — for
   `claude-code` that is a **Claude Code plugin marketplace** (`.claude-plugin/marketplace.json`
   plus a `plugins/` tree carrying `.mcp.json`, `agents/`, `commands/`, `skills/`).
2. `hitch config-dir create <profile> <name>` layers overrides onto that build output to produce
   a named **Config Directory**.
3. `hitch up <target> <profile>` launches the harness against that Config Directory, building it
   if absent.

So Decision 4's "emit at launch into an ephemeral config directory" is already implemented
upstream as `hitch up`. Whether `CLAUDE_CONFIG_DIR` is the binding mechanism or the marketplace
is installed via `claude plugin marketplace add` **was not determined** and remains open.

**Decision 2's per-target loss is already machine-reported.** The emitter prints, per pack, which
declared families a target cannot accept — observed: `target 'claude-code' does not support
family 'instructions'`, and likewise for `personas`. The conformance measurement this ADR asks
for partly exists rather than needing to be built.

**Decision 3's capability tier is already enforced, as preflight.** The emitter refuses to build
when a required binary is absent or the host OS is unsupported. This is the capability-conditional
probe Decision 3 argues templating cannot do — it exists, and it fails closed.

**Decision 5 is declared but NOT enforced.** Observed: `[warn] binary 'bh' version constraint
'>=0.3.0' not verified`. The pack expresses the constraint; nothing checks it. Version pinning is
therefore currently advisory, which is exactly the gap that makes local emission a drift vector.

**Two blockers found, both narrow.** The `beadhive` pack declared `requirements.os: [darwin]`,
which fails preflight on Linux outright; the pack's content carries no genuine macOS dependency
beyond install-instruction prose, so this is over-restrictive metadata. Separately, four of six
preflight failures were purely a `PATH` that omitted the account's `.local/bin` — the service
environment problem recorded in Context (3), reproducing here as a direct build failure.

## Amendment 2 — hitch is optional support behind the plugin seam

**Date:** 2026-07-31. Operator decision, taken after Amendment 1's spike findings. This narrows
how bh exposes the integration and, in doing so, retracts the largest cost this ADR recorded.

**agent-hitch is an OPTIONAL integration, exposed only through the existing `bh plugin` seam** —
the same shape as `orca`, `git-workspace`, and `observaloop`. It is not a change to bh's core
launch path, and it is not an implicit step inside `bh work`.

Launch mechanics are a plugin verb:

```sh
bh plugin hitch up claude <profile>
```

`src/beadhive/plugins.py` already defines the contract (`name` / `cli` / `enabled` /
`on_onboard` / `on_retire` / `readiness` / `wt_create` / `wt_remove`), and `registry()`'s own
docstring states that new integrations join the list the same way. `gitworkspace_plugin.py` is
the closest analogue, since it also wraps an external binary. Notably `wt_create` fires exactly
when a seat is provisioned, and a seat's config directory is the same shape of per-seat resource
as its worktree — so the provisioning seam this ADR needs likely already exists.

### This retracts Decision 4's stated cost

Decision 4 records: *"this puts agent-hitch on the critical path of every harness start."*
**Behind an optional plugin, that is false.** The critical path is unchanged for anyone who has
not opted in, and the fail-fast operational dependency applies only to hosts that enable it. The
cost is real but scoped, not global — a strictly better position than Decision 4 assumed.

Decision 4's substance is otherwise unchanged: when the plugin *is* enabled, config is still
emitted at launch into an ephemeral directory, and nothing persistent is emitted, so nothing
emitted can drift.

### Degradation is the acceptance bar

Disabled by default. With hitch absent, disabled, or failing to load, bh must behave **exactly**
as it does today — not "mostly working". A plugin that alters the default launch path while
disabled has failed regardless of how well it performs when enabled.

The same rule governs the diagnostic surface: seat-runnability reporting rides `Plugin.readiness`
and is **silent** when hitch is disabled — no warning, no nagging, no suggestion to enable it. An
optional integration that complains when unused is not optional.

### Unchanged by this amendment

Decisions 2, 3, and 5 concern pack/emitter correctness and are independent of how bh exposes the
integration. The two upstream agent-hitch defects recorded in Amendment 1 — the over-restrictive
`os: [darwin]` gate and version constraints that warn rather than enforce — are likewise
unaffected.

## Amendment 3 — bh renders its own config from the HQ host manifest; no dotfile manager required

**Date:** 2026-07-31. Operator decision. Extends the same principle to bh's *own* configuration
that Decision 2 applied to harness configuration: **generate, do not template or transport.**

**bh renders `~/.beadhive/config.yaml` itself, from the per-host manifest already in HQ**
(`hosts/<host_id>.yaml`). No template engine, and therefore no dependency on any particular
dotfile manager.

### What this replaces

The multi-host plan's dotfile-scaffolding work proposed rendering bh's config from a chezmoi
source: `dot_beadhive/config.yaml.tmpl` driven by a host table keyed on `.chezmoi.hostname`.
Every artifact in that design is a chezmoi convention — `dot_beadhive/` (source-state naming),
`.tmpl`, `.chezmoi.hostname`, `.chezmoidata` — so it did not merely prefer chezmoi, it was
unimplementable without it. That made chezmoi a silent hard dependency of bh's multi-host story,
and dotfile managers differ sharply in features; templating in particular is not universal.

### Why the infrastructure already exists

HQ is a git remote bh already clones, and the fleet/host config split is already implemented:
`load_fleet()` reads the fleet-wide base, `load_host()` reads host-owned content, and `load()`
deep-merges host over fleet. Per-host identity and the `hosts/<host_id>.yaml` manifest already
exist. So the distribution mechanism for bh's own config is **built and shipped** — a dotfile
manager was never needed for it.

### Consequences

- The hostname-keyed selector problem **disappears** rather than being solved. bh reads its own
  host's manifest entry directly; nothing selects a row from a table.
- bh needs the **least** possible from a dotfile manager: plain file sync. Templating capability
  stops being a selection criterion, so "not all managers are equal" stops mattering.
- A dotfile manager becomes **one more optional plugin** under Amendment 2's pattern, not a hard
  dependency — see the follow-up work filed for chezmoi specifically.
- What remains for a manager to sync is the **Hitch Pack**, and only on hosts that enable the
  (also optional) hitch plugin. A host running neither plugin needs no dotfile manager at all.

### Correcting an earlier closure

Spike bead `bh-y3xd.3` asked exactly this — *"is `none` (bh renders from the manifest)
sufficient for `~/.beadhive`?"* — and was closed on 2026-07-31 as converging with this ADR. That
closure was premature: this ADR had settled the **harness** layer, not bh's own config. The
question was live, and this amendment is its answer: **yes, `none` is sufficient.**

## Amendment 5 — config directories persist; Decision 4's "ephemeral" is retracted

**Date:** 2026-07-31. Resolves the auth tension surfaced when `bh plugin hitch up` landed.

**Decision 4 said config is emitted "at launch time into an ephemeral directory". That is
retracted.** Config directories **persist**, and are rebuilt when their inputs change.

### Why ephemeral was wrong

Three reasons, and the first alone is disqualifying:

1. **It forces re-authentication at every launch.** Claude Code keeps OAuth session state in
   `.claude.json` *inside the active config directory*. A directory recreated per launch has no
   session, so every seat start would demand a login. That is not a rough edge; it makes
   unattended operation impossible, which is the entire point of the factory.

2. **It fights the tool.** `hitch up` builds the Config Directory **only if absent**
   (`--profiles-file` is documented as "used to build the default Config Directory if missing").
   Its design presumes a directory that persists and is reused. Ephemerality would mean
   rebuilding on every launch specifically to defeat that.

3. **It was never the actual requirement.** Decision 4's stated goal was "nothing persistent is
   emitted, therefore nothing emitted can drift". But the property that prevents drift is that
   config is **derived from the current pack on this host** — not that it is destroyed
   afterwards. A persistent directory rebuilt when its inputs change has exactly the same
   freshness guarantee, without the cost.

### Why rebuilding is safe for auth state

Verified in the emitter (`profile_build_claude_config_dir.py`): the build is **additive**. It
`mkdir(parents=True, exist_ok=True)`, `shutil.copytree(..., dirs_exist_ok=True)`, and writes
`settings.json` and `README.md`. There is no `rmtree` of the output directory, and it never
writes `.claude.json`. So a rebuild refreshes emitted content and leaves Claude Code's own
runtime state — including the OAuth session — untouched.

**Auth therefore becomes a one-time bootstrap per config directory**, analogous to `gh auth
login` on a new machine, rather than a per-launch obstacle.

### The residual hazard, stated because additive is not free

Additive rebuild **does not prune**. If a pack removes a skill, a command, or an agent, the old
file survives in an existing config directory — the emitted content is a superset of what the
pack now declares, and the seat silently keeps a capability that was deliberately withdrawn.
That is a real staleness vector and it is the honest cost of persistence.

Whatever implements rebuild must handle removals explicitly: either build into a fresh directory
and carry `.claude.json` (and any other runtime state) across, or track emitted paths and prune
what the current build no longer produces. **Do not rely on additive copy alone.** Note that the
first option reintroduces the auth problem unless the carry-across is deliberate and complete.

### What is unchanged

Decision 4's substance otherwise stands: bh resolves the profile and builds on the host, so the
host's own capabilities are resolved locally and nothing emitted is ever synced between hosts
(Decision 3). Amendment 2 also stands — this remains an optional plugin, and none of it applies
to a host that has not enabled it.
