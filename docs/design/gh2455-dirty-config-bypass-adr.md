# GH#2455 dirty-config bypass ADR (bh-areg.2)

> Status: **decided, shipped, deliberately temporary.** `bh` clears a real bd storage-layer
> bug by directly executing SQL against bd's own Dolt database — a bypass `bd sql --help`
> itself warns against. This document is the conscious decision that constraint required,
> not a quiet helper function.

## Context

A freshly-minted **server-mode** `bd` store (owned, shared, or external — never embedded)
can leave its internal `config` table reported as `modified` by `dolt_status` immediately
after `bd init`, with no bd-native command able to clear it: `bd dolt commit` prints
"Committed." while the dirty row survives the "commit" unchanged. The next `bd dolt pull`
then refuses outright with a dirty-internal-config guard.

The bug is bd's own internal numbering — its error text says "GH#2455", which resolves to
no public issue anywhere. The real, open, **unpatched at time of writing** upstream reports
are **gastownhall/beads#4934** and **#5111**. Nothing in this decision, the code, or any
`bh` output should ever point an operator at "GH#2455" as if it were a public issue to go
read; where it's mentioned at all, it's labeled bd-internal and paired with the real
upstream numbers.

Today this is a desk workaround: `bd sql "CALL DOLT_ADD('-A')"` then
`bd sql "CALL DOLT_COMMIT('-m', ...)"`, applied once, by someone who read a spike doc. `bh`
does not yet default any onboarding path to server mode itself (that is a separate,
later-landing sibling bead's job — see `bh-areg`'s "default" child) — but the moment it
does, or the moment an operator's own ambient bd config/env (`BEADS_DOLT_SHARED_SERVER=1`,
`dolt.shared-server: true`, an explicit `--server`) puts `_act_bd_init` on a server-mode
path today, this stops being a footnote and becomes the first thing that hive's operator
hits. This bead is deliberately defensive, landing ahead of that default.

## The empirical split (bh-u562.1, re-verified for this bead)

The upstream bug is real, but it is **specific to a fresh, non-clone server-mode init** —
not to server mode in general. `bh-u562.1` Finding 7 established:

- Immediately after a **cold-clone** `bd init --server --remote git+ssh://...` against a
  real, populated remote, `dolt_status` returned `[]` (clean) for owned, shared, **and**
  external modes — zero bypasses needed via the clone-based recipe.
- A **plain, non-clone** `bd init --server` (no `--remote`) reproduced the bug exactly:
  `dolt_status` immediately showed the dirty `config` row, and `bd dolt commit` printed
  "Committed." while the row stayed dirty — confirming no bd-native command clears it. The
  documented SQL bypass cleared it in exactly one application.

This bead re-verified the split directly against `onboard._act_bd_init`'s three concrete
paths (real `bd`, isolated scratch dirs, a real git-backed origin — never touching a real
hive or `~/.beadhive`, matching the `bh-u562.1`/`bh-00cq` discipline):

| `_act_bd_init` path | mechanism | `dolt_status` immediately after (server mode) |
|---|---|---|
| **furnished** `bd init` (no `--remote`) | bare init, mints a fresh store | **dirty** — reproduced (`config` row, `status: modified`) |
| **zero-footprint** `bd init --setup-exclude` (no `--remote`) | bare init, mints a fresh store | **dirty** — reproduced identically (flags differ only in gitignore/exclude bookkeeping, not the init mechanism) |
| **`bd bootstrap`** from origin's `refs/dolt/data` (second-host case) | clone-equivalent: pulls an already-committed dolt history | **clean** — `[]`, confirmed against a real git-backed origin in shared-server mode |

The third row is not a guess: `bd bootstrap`'s sync-from-origin action
(`cmd/bd/bootstrap.go`'s `executeSyncAction`) calls the exact same `cloneFromRemote` /
`cloneFromRemoteWithMode` primitive that `bd init --remote` uses
(`cmd/bd/init.go:1084`) — source-confirmed, not inferred from behavior alone. Both routes
pull an already-committed dolt commit graph; the dirty `config` row is an artifact of the
uncommitted **working set** at fresh-init time, which a clone never transfers because a
clone only ever materializes committed history.

## Decision

1. **Bypass only the two paths that mint a fresh store.** `_act_bd_init`'s furnished and
   zero-footprint branches (both bare `bd init`, no `--remote`) call the bypass unit after
   `bd init` returns. The `bd bootstrap` branch (second-host case) calls nothing — the
   empirical + source-level evidence above says it never needs it, and adding a no-op check
   there would cost an unnecessary subprocess spawn with zero payoff.
2. **One named unit, not scattered inline SQL.** `onboard._bypass_gh2455_dirty_config` (plus
   its `_dolt_status_has_dirty_config` helper) is the sole place bh ever issues
   `CALL DOLT_ADD` / `CALL DOLT_COMMIT` directly against bd's database. Its docstring states
   the removal condition verbatim: delete the function and both call sites the moment bh's
   required bd floor version ships a fix for gastownhall/beads#4934 or #5111.
3. **Detection never trusts a parsed "mode" string.** `bh-u562.1` Findings 8/9 found
   `bd dolt status --json`'s `"mode"` key inconsistent across bd's four engine modes
   (absent for owned and local-external). Detection instead runs
   `bd sql "SELECT * FROM dolt_status"` directly and uses bd's own exit code as the
   discriminator — a non-zero exit (embedded: `bd sql` is unsupported there) means "nothing
   to do," never a guess based on a fragile string.
4. **Visible, never silent, exactly when it does something.** No output at all when there is
   nothing to report (embedded mode, or an already-clean store) — that is what keeps
   embedded-mode onboarding byte-for-byte unaffected (see Consequences). When it finds and
   clears a dirty `config` row, it says so on stdout; if the bypass itself fails to clear it,
   that's a stderr warning pointing at this document.
5. **Never claimed as sanctioned bd behavior.** The detection/clear message and this
   document both state plainly that `bd sql --help` itself warns direct SQL access
   "bypasses the storage layer" — this is bh's own temporary workaround, not something bd
   endorses.

## Consequences

- **Embedded-mode onboarding is unaffected**, verified by a regression test
  (`tests/test_onboard_gh2455.py`) asserting byte-identical `hive.run` call sequences and
  zero extra `typer.echo` output when `bd sql`'s probe fails the way it does under embedded
  mode. The one added cost is a single extra fast-failing subprocess spawn per bare `bd
  init` call (furnished/zero-footprint paths only) — bd's own backend check fails
  immediately, before opening any real database work.
- **`bd bootstrap`'s second-host path stays untouched.** If this reasoning is ever proven
  wrong by a future bd version, the fix is to add a third call site next to the other two,
  not to change this decision's shape.
- **This is borrowed code with an expiry date.** Once bh requires a bd floor version past
  gastownhall/beads#4934 or #5111, `_bypass_gh2455_dirty_config` and its two call sites in
  `_act_bd_init` should be deleted outright — nothing else in this codebase depends on it.

## Out of scope

- **Making any `_act_bd_init` path default to server mode.** That's `bh-areg`'s "default"
  child bead's decision, gated on `bh-ukit.4`/lifecycle/`bh-wnly`, not this one's.
- **A generic "bd bug workaround" framework.** One function for one bug, named after that
  bug, removed when that bug is fixed — not a reusable mechanism for future bd bugs, which
  would be speculative generality this bead has no evidence it needs.
