# Passthrough & hive routing

`bh bd` and `bh git` forward to `bd`/`git`, optionally across hives (modules: `bd.py`,
`git.py`, `route.py`).

## `bh bd`

Forwards to `bd` in the current directory, with two enhancements: `bh bd create` **and**
`bh bd import` auto-apply the `provider:/org:/repo:` triplet derived from the path (ports the
old `bdc`). Outside a managed path they degrade to plain `bd`. Both refuse if the hive has label
violations ([LABELS](LABELS.md#enforcement)).

### The host-lease gate

A **write** verb forwarded through `bh bd` is refused when this host does not hold the hive's
host lease — the same decision, predicate and refusal text `bh work assign|claim|submit|merge`
already get ([multi-host model](design/multi-host-model-adr.md)). That is the substantive
reason to prefer `bh bd` over a direct `bd`: bh can only respect a lock it is asked to.

- **Reads are never gated** — `list`, `ready`, `show`, `status`, `query`, `export`, `dep list`
  and friends forward untouched, and don't even pay for a config load.
- **Unknown verbs count as writes.** The allowlist is of reads (`guard.BD_READ_VERBS`), so a
  bd verb bh hasn't heard of is gated rather than waved through. Forgetting to add one costs a
  legible refusal; the other direction costs a silent hole.
- **Per hive under `-a`/`-r`** — a fleet-wide passthrough refuses only the hives this host
  isn't primary for, and still runs the rest.
- **Nothing is gated on a single-host factory.** An absent lease means "unconfigured", not
  "someone else's"; exclusive primary switches on when a second host adopts.

This is early, legible failure — not enforcement. It gates `bh bd`, not a genuinely raw `bd`,
which nothing in bh can. The backstop is the epoch fence beside the data at push time
(`host_fence.py`, [spike](spikes/bh-ukit.2-fence-under-a-dolt-server.md)).

### Destructive wisp-cleanup guard

Two `bd` cleanup operations are unsafe while any wisp molecule in the hive is still open:
`mol wisp gc --closed` can delete completed steps from an in-flight molecule, and `mol squash`
can delete its open steps and auto-close its root. Before forwarding either operation, `bh bd`
queries all wisp molecule roots hive-wide and refuses when any are non-closed, naming them in the
error. A failed or malformed safety query also refuses rather than guessing that cleanup is safe.

Once every wisp molecule is closed, both operations are forwarded unchanged. An operator who has
independently verified safety can use the standing convention-guard escape hatch,
`BH_DEBUG=1`. This is still early, legible failure rather than enforcement: a genuinely raw
`bd mol wisp gc` or `bd mol squash` invocation remains outside bh's control.

`bh bd import` is the bulk counterpart: plain `bd import` is a raw upsert that does *not* inject
the triplet, so a backfill JSONL would land registry-invalid. `bh bd import` merges the triplet
into every record's labels first (idempotent — existing tags aren't duplicated), then upserts by
`external_ref`. A zero-change re-import (bd's "nothing to commit") is treated as a successful
no-op, so re-running is safe.

```sh
bh bd ready
bh bd create "Fix login" -p 1      # → bd create … -l provider:…,org:…,repo:…
bh bd import backfill.jsonl        # → triplet merged into each record, then bd import (upsert)
```

### Filing bead prose — the transport rule

**Never pass bead prose as a double-quoted shell argument.** Bead prose is markdown, and
markdown marks identifiers with backticks; inside a double-quoted shell string a backtick is
**command substitution**. On 2026-08-16 a `bh bd create` call whose acceptance text named
`just push` / `just bump` / `just release` as code spans caused bash to **run them** before `bh`
was exec'd: main was pushed and a version bump + tag were created. `just release` — publish to
PyPI, recoverable only by yank — was a code span in the same argument and did not fire only by
luck, because bash pairs backticks **positionally** (1↔2, 3↔4), so which spans execute drifts
out of phase with what the author wrote. Auditing your prose for "dangerous" backticks is
therefore checking the wrong thing.

bh cannot defend against this. Substitution completes in the shell before the process starts;
bh receives the *result*. The only fix is a transport where prose is never a shell token.

In order of preference:

1. **The `bd_create` MCP tool** ([MCP](MCP.md)). Prose arrives as a JSON value over stdio and is
   never re-parsed. No shell exists anywhere in this path — the one transport where the bug is
   structurally impossible rather than merely avoidable.
2. **`bh bd create --json <path>` / `--json -`** — one whole-bead JSON document, the *same*
   schema the MCP tool takes (`bd_create` and `--json` share one core, so they cannot drift):

   ```json
   {"title": "…", "type": "bug", "priority": 0, "description": "…",
    "acceptance": "…", "design": "…", "parent": "…", "labels": [], "deps": []}
   ```

   A bare object, or a list of them. Values are placed straight into `bd`'s argv — a list, run
   without a shell — so no character in the prose is interpreted. `--json` refuses to be combined
   with per-field flags rather than resolving a silent precedence; triplet injection and the
   per-bead label gate are identical to the flag path.

   ```sh
   bh bd create --json bead.json
   printf '%s' "$payload" | bh bd create --json -
   ```

3. **A quoted heredoc plus a command-substituted read**, when neither is available:

   ```sh
   cat > /tmp/acceptance.md <<'EOF'      # QUOTED 'EOF' — unquoted EOF still substitutes
   Prose with `just release` in it.
   EOF
   bh bd create "title" --acceptance "$(cat /tmp/acceptance.md)"
   ```

   Command-substitution *output* is not re-scanned, so backticks inside the file are inert.

**Per-field `--file` flags do not close this class.** `bd create --body-file` / `--design-file`
are why those two fields survived the incident intact, and they are worth using — but the file
*write* is still shell-mediated (`cat > f <<'EOF'` is safe, `<<EOF` is not), so they **move** the
hazard into the heredoc rather than removing it: N writes with N chances to get the quoting
right, instead of one payload with one rule. A correct habit applied inconsistently still fails,
which is exactly how the incident happened — that same command used `--body-file` and
`--design-file` for description and design, and inlined acceptance and notes because they felt
short enough.

## `bh git`

Forwards to `git`, including `git workspace …` (git-workspace's own subcommands). One special
case: git hijacks `--help` for subcommands, so `bh git workspace --help` is rerouted to the
`git-workspace` binary (which has the real help).

```sh
bh git status
bh git workspace list
bh git workspace --help            # → git-workspace --help
```

## Hive routing (`-a` / `-r`)

Run the passthrough across hives instead of the current directory. Flags are **global** —
they go on `bh`, before the subcommand:

```sh
bh -a bd dolt push                 # every registered hive
bh -a git status
bh -r ag-infra git log --oneline   # one hive
bh -r ag-infra bd ready
```

- `-a/--all` → every entry in `managed_repos` (registered hives; the bh domain).
- `-r/--hive <id>` → one hive (resolution below).
- no flag → the current directory (today's plain passthrough; works without git-workspace).

For *all cloned repos* (broader than registered hives), use git-workspace's own runner:
`bh git workspace run -- <cmd>`.

### Mechanics (`route.py`)

- The root callback captures the flags; `route.targets(cfg, mode, target)` resolves them to
  `[(label, cwd)]`.
- `route.fan_out(targets, runner)` runs each, printing a `=== <hive>  <path> ===` header for
  multi-target runs, **continuing past failures**, and ending with an
  `N ok / M failed / K skipped` summary (exit non-zero if any failed). A single
  current-directory run propagates the child's exact exit code.
- `bh -r/-a bd create` applies each target hive's own triplet (cwd-aware).

### Gating & guards

- `-a`/`-r` no longer gate on a `git_workspace.enabled` flag — git-workspace is a required dep
  ([INTEGRATIONS.md](INTEGRATIONS.md)), always present. `-r <id>` still fails fast if `<id>`
  doesn't resolve to a registered hive in `managed_repos`.
- They're honored only by `bd`/`git`; using them elsewhere, with `bh git workspace …`, or
  after the subcommand is rejected (see [CLI](CLI.md#global-routing-flags)).

### Resolving `-r <id>` (`hive_match`)

Set under `git_workspace` in config; default `flexible`:

- **flexible** — try in order: prefix (`ag-infra`) → triplet (`github/agentguides/infra`) →
  `org/repo` (`agentguides/infra`) → bare repo (`infra`, only if unique).
- **prefix** — only the beads prefix.
- **triplet** — only the full `provider/org/repo`.

Resolution maps to `managed_repos` and the hive's checkout dir under `$GIT_WORKSPACE`
(`registry.resolve_hive` / `hive_dir`).
