# herdr integration ADR — isolated panes, native worktrees, visible bead identity

**Status:** decided (GO), session selection amended by `bh-tvre4` and `bh-359ih` ·
**Date:** 2026-08-23 · **Decision owner:** `bh-ffwnu.1` ·
**Related:** [HERDR.md](../HERDR.md),
[work-runtime-tiers-adr.md](work-runtime-tiers-adr.md), and
[managed-harness-config-adr.md](managed-harness-config-adr.md).

## Context

`herdr` can start and observe real coding-agent terminal panes, while `bh` already owns the
bead lifecycle, managed worktrees, and the optional-plugin seam. The live experiment recorded in
`docs/HERDR.md` confirms that herdr's lifecycle signals are useful but that a first prompt can be
intercepted by harness onboarding, and that `HERDR_ENV` is a convention rather than a socket-level
access control.

This ADR fixes the ownership boundaries before implementing `bh plugin herdr`. It deliberately
does not make herdr authoritative for bead state, worktree state, or normal Task-tool fanout.

## Decision: GO

Proceed with the `bh plugin herdr` implementation beads. The integration is an opt-in,
best-effort interactive execution surface with explicit lifecycle boundaries below. Beads and git
remain the durable record; herdr is a live terminal/process controller.

### 1. Default to Herdr's normal session; allow explicit exact selection

The plugin resolves every session-scoped call with one precedence rule: an explicit `--session`
flag, then `BH_HERDR_SESSION`, then the normal `default` session. An operator may select an exact
named session through either override, or use the `current` / `active` sentinel from a
Herdr-managed pane. The sentinel resolves only from Herdr's injected caller environment; it never
follows another client's focused workspace. The resolved session is emitted in JSON and must be
carried through nested lifecycle and view calls.

The supervisor process may invoke herdr from outside a herdr pane; `HERDR_ENV` is not relied on
as an authorization boundary because the experiment established that it is unenforced. Isolation
comes from exact session selection and from every create operation being non-focusing by default.
A human attach command may print the exact session command, but `bh` must not take over the user's
TTY or workspace. The ordinary `default` session is operator-owned: a stopped default tombstone
is never automatically deleted or recreated. The legacy `bh-supervisor` spelling remains an
ordinary explicit compatibility override and the sole reserved recovery name whose stopped
tombstone may be recreated automatically. Other stopped sessions require an explicit human
recovery action, and a session failure never permits fallback to another session.

**Why:** matching Herdr's standard startup makes omission useful on an ordinary installation.
Exact selection, non-focusing create operations, and strict ownership proofs retain the safety
boundary; carrying the selected name through every command preserves intent without ambient
focus.

### 2. bh owns worktree lifecycle; herdr receives an existing worktree

`bh` remains the sole creator, attacher, initializer, and remover of managed git worktrees and
their `wt/...` branches. The herdr plugin does not register `wt_create` or `wt_remove` hooks and
does not call herdr's `worktree create`, `open`, or `remove` commands.

`spawn --hive --bead` resolves the bead's already-provisioned bh worktree and starts the pane with
that directory as its cwd. It must fail clearly when there is no valid bh worktree for the bead;
it must not silently create a second checkout. `reap` closes herdr resources only and never
removes a worktree. Normal `bh work` lifecycle commands remain responsible for worktree cleanup.

**Why:** `worktree.py` already owns branch naming, init/provisioning, attach behavior, and the
durable bead-branch contract. Dual ownership would permit double-booked directories and divergent
cleanup semantics.

### 3. Put bead identity in Herdr metadata and deterministic visible names, not a bh side table

Each spawned agent receives a deterministic, bh-reserved agent name and the corresponding pane is
renamed/labeled with that target. Herdr's 32-character target limit means dotted and long IDs
cannot always remain reversible in the visible name. New launches therefore report plugin-owned
workspace and pane metadata containing the exact canonical hive, bead, opaque target, marker, and
contract version. `bh plugin herdr ps --json` reads that metadata from one live snapshot; it does
not maintain a separate durable name-to-bead mapping or decode hashed targets.

Pre-metadata targets remain compatible only through a strict legacy proof: the deterministic
target, pane name, `bh:<hive>` workspace, unique target/pane records, and exact managed-worktree
cwd must all agree. A lookalike target without those facts is foreign rather than guessed.

The spawn operation must complete the agent/pane naming step before reporting success. If it
cannot tag a newly created resource, it reports failure and performs best-effort cleanup rather
than leaving an uncorrelated pane. `ps` renders an unrecognized, manually created agent as
unmanaged rather than guessing a bead.

**Why:** Herdr presentation tokens are live resource metadata and disappear with the resource. A
bh side table would duplicate live state, leak on crashes or manual pane cleanup, and violate the
plugin's read-through design. Visible names still make the association useful to an operator,
while metadata supplies lossless machine correlation.

### 4. herdr complements, never replaces, Task/Agent fanout

The existing in-process Task/Agent route remains the default for ordinary fire-and-forget
subagent work. Herdr is selected only by an explicit high-level `bh plugin herdr launch
<bead-id>` action, or by the low-level `spawn` / `dispatch` primitives when preparation is
deliberately external. `launch` may resolve and claim one exact bead through native `bh` lifecycle
ownership, but Herdr does not change `work.runtime`, intercept Task calls, or automatically route
ready beads into panes.

`dispatch` treats herdr's settled/`done` lifecycle signal as insufficient by itself for a newly
started agent. It applies the warm-up and pane-content verification described in `HERDR.md` before
claiming a prompt was delivered. Bead progression still occurs through `bh work` and git, not a
herdr status transition.

**Why:** the two mechanisms have different operational tradeoffs. Keeping both preserves the
documented Task-tool workflow and adds an opt-in live-operations surface without making a terminal
server a dependency of routine dispatch.

## Consequences and implementation constraints

- `herdr_plugin.py` is optional and import-safe: missing binary, unavailable server, or a failed
  subprocess degrades to a clear warning/falsy result where the command contract permits it.
- The plugin is mounted through the existing static plugin registry. It is config-gated and does
  no onboarding-time pane creation or per-agent integration installation. External package
  registration is a separate consent-only action enabled by explicit `--plugin herdr`; a running
  server alone never enables it.
- All plugin actions target the exact resolved session; no command may enumerate or fall back to
  another live session when selection fails. Selection is `--session` over `BH_HERDR_SESSION`
  over `default`.
- The initial worktree hooks remain `None`. This is an intentional boundary, not deferred
  plumbing.
- `launch` is a native-lifecycle composition, not a second lifecycle: hive lookup and claim /
  worktree provisioning stay in `bh`; session, workspace, pane, process, and live state stay in
  Herdr. A Herdr failure never rolls back a successful native claim or deletes its worktree.
- `spawn --hive --bead --kind` retains its required low-level surface unchanged. It remains useful
  for callers that already hold the claim/worktree; `launch <bead-id>` is the human- and
  agent-facing default.
- The implementation must test both the deterministic naming/parser contract and the invariant
  that spawn/reap do not invoke herdr worktree commands.

## Rejected alternatives

1. **Follow the focused session or another client's focus.** Rejected because `HERDR_ENV` does
   not technically prevent it, so an unattended supervisor could modify a human's active
   workspace. Omission selects the exact normal `default` session, never a focused workspace;
   `current` is accepted only from a verified Herdr-managed pane.
2. **Delegate worktree management to herdr.** Rejected because it duplicates bh's durable
   worktree/branch lifecycle and creates two cleanup authorities.
3. **Keep a bh-side agent-to-bead database.** Rejected because it is a second, stale-prone source
   of truth for state herdr already exposes.
4. **Replace Task/Agent fanout.** Rejected because it would make interactive terminal management a
   dependency of established dispatch and would erase the distinct operator-control use case.

## Follow-through

Unblock and implement the existing `bh-ffwnu` children in dependency order. In particular, the
scaffold establishes the session/config/plugin registry contract; `spawn` enforces native
worktree ownership and deterministic identity; `ps` consumes that identity; and the dispatcher
skill documentation explains the explicit choice between Task/Agent and herdr.
