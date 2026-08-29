# herdr — terminal/agent pane control (`bh plugin herdr`)

`bh plugin herdr` is an optional integration with [herdr](https://herdr.dev), a terminal
workspace manager purpose-built for driving coding-agent panes. It follows the same optional
plugin shape as `orca`, `observaloop`, and `hitch` (`plugins.py`), while `bh` remains the
authority for beads, branches, and managed worktrees.

## What herdr is

A client/server terminal multiplexer (v0.8.0 at time of writing) that runs a persistent
background server (`~/.config/herdr/herdr.sock`) and exposes a JSON socket API through the
`herdr` CLI. It organizes terminals into **workspaces → tabs → panes**, recognizes coding
agents running inside panes via small per-tool hook integrations, and tracks each agent's
lifecycle (`idle` / `working` / `blocked` / `done` / `unknown`).

- Binary: `/home/linuxbrew/.linuxbrew/bin/herdr` (Homebrew)
- Config: `~/.config/herdr/config.toml`; logs alongside it
- `herdr --skill` prints herdr's own agent-facing operating instructions — the CLI's authority
  on syntax is the installed binary itself (`herdr <group>` with no subcommand reprints that
  group's help), so `bh`'s wrapper should defer to it rather than hardcoding assumptions that
  will drift across herdr versions.

## Command surface

| Group | Verbs | Controls |
|---|---|---|
| `workspace` | list, create, get, focus, rename, close, report-metadata | Top-level spaces (`--cwd`, `--label`, `--env KEY=VALUE`, `--focus`/`--no-focus`) |
| `tab` | list, create, get, focus, rename, close | Tabs within a workspace |
| `pane` | list, split, run, send-text, send-keys, wait-output, read, resize, zoom, move, swap, close, report-agent(-session), release-agent | Raw terminal control — run arbitrary commands, split layout, read output |
| `worktree` | list, create, open, remove | Git-worktree-backed workspaces (`--branch`, `--base`, `--path`) |
| `agent` | list, get, read, send-keys, prompt, rename, focus, wait, attach, start, explain | Agent-aware layer: `start --kind <claude\|codex\|gemini\|cursor\|...> --pane <id>`; `prompt <target> <text> --wait --until <state>` |
| `session` | list, attach, stop, delete | Named persistent multi-workspace sessions; `--remote <ssh-target>` for a remote server |
| `integration` | install, uninstall, status | Wires per-agent hooks so herdr can detect lifecycle state |
| `api` | snapshot, schema | `api snapshot` dumps full live state as JSON; `api schema --json` dumps the typed request/response schema |

Supported `agent start --kind`: `pi, claude, codex, gemini, cursor, devin, agy, cline, omp,
mastracode, opencode, copilot, kimi, kiro, droid, amp, grok, hermes, kilo, qodercli, maki`.

## Local experiment (this session)

Installed both integrations and drove real Claude and Codex panes through the socket API from
outside herdr (a plain shell, `HERDR_ENV` unset) against the already-running default server,
in an isolated `--no-focus` workspace (`w2`) that was closed afterward — the user's own
workspace (`w1`) was never touched.

```bash
herdr integration install claude   # -> ~/.claude/hooks/herdr-agent-state.sh, adds a SessionStart
                                    #    hook entry to settings.json alongside orca's existing
                                    #    UserPromptSubmit/Stop/Subagent* hooks — additive, not
                                    #    a takeover of those event slots.
herdr integration install codex    # -> ~/.codex/herdr-agent-state.sh, hooks.json, config.toml

herdr workspace create --cwd <dir> --label herdr-experiment --no-focus   # -> w2
herdr pane split --pane w2:p1 --direction right --cwd <dir> --no-focus  # -> w2:p2
herdr agent start expclaude --kind claude --pane w2:p1   # idle in ~1s
herdr agent start expcodex  --kind codex  --pane w2:p2   # idle in ~1s
herdr agent prompt expclaude "..." --wait --timeout 60000   # done in ~3s
herdr agent prompt expcodex  "..." --wait --timeout 60000   # done in ~2s
herdr workspace close w2
```

### Findings

1. **Lifecycle detection works and is fast** once the per-tool integration is installed —
   `idle`/`done` transitions were observed within 1–3 seconds, and `agent get` returns a stable
   `agent_session` id plus the pane's live terminal title (handy as a free status line).
2. **A freshly started pane's first-run UI intercepts the first prompt.** Both Claude ("Teach
   auto mode about your environment?") and Codex (a hook-trust review screen) showed an
   interactive onboarding dialog on first launch. `agent prompt --wait` still reported
   `agent_status: done` — herdr's detector watches terminal-state signatures, not semantic task
   completion, so `done` does **not** guarantee the submitted text reached a real conversation
   turn.
3. **Recovery from that first-run interception differs by agent kind and isn't automatic.**
   Dismissing Claude's dialog (`send-keys down`, `send-keys enter`) let the buffered prompt
   through to a real turn. Codex needed the prompt **resent** after dismissing its screen with
   `esc` — nothing conversational happened until the second `agent prompt` call.
4. **Read-source choice matters and can silently under-report.** `agent read --source
   recent-unwrapped --lines 15` missed a turn that `--source visible` showed clearly, on the
   same pane, moments apart — a plugin should not treat a short "empty-looking" read as ground
   truth without also trying `visible` or increasing `--lines`.
5. **`HERDR_ENV` is a self-imposed gate, not a technical one.** herdr's own `--skill` doc tells
   an agent to refuse control commands unless `HERDR_ENV=1` (i.e. running inside a
   herdr-managed pane), but every command used above ran fine from a plain shell outside herdr
   — it just talks to the local socket. A `bh` plugin driving herdr from an unrelated process
   (e.g. a supervisor seat) is technically unconstrained but is deliberately bypassing herdr's
   own safety convention; see open question below.

**Design implication:** a herdr-launched agent pane should not be considered ready-to-drive the
moment `agent start` returns `interactive_ready: true`. The plugin needs a **warm-up step**
(send a harmless no-op / dismiss-known-dialogs pass, or a `read` + pattern check against known
first-run screens before the first real `prompt`) and should verify completion by reading pane
content back and checking it actually contains the expected turn — not just trusting `--wait`'s
settled state — at least for the first prompt against a newly started pane.

## `bh plugin herdr` design

### Scope

Mirrors the existing plugins: a thin, best-effort wrapper (`herdr_plugin.py`) around the
`herdr` CLI, never a reimplementation of it. `bh` should not duplicate herdr's own state — read
through `herdr api snapshot` / `agent get`, write through the `herdr` CLI, same pattern as
`orca.py`'s `run.out(["orca", ...])` calls. `import beadhive.herdr_plugin` must always be safe
(missing binary, stopped server, or any subprocess failure degrades to a warning + falsy
return, never raises), matching every other plugin's contract.

### Why this hive, not a new one

The overlap is direct: herdr's `agent` layer is a second engine capable of running a `bh:
developer` (or any seat) inside a real interactive pane alongside the in-process Task/Agent
tool, and herdr's `worktree` group duplicates `bh`'s own worktree lifecycle. The integration ADR
sets the ownership line so this remains an optional interactive surface, not a parallel runtime.

### Command surface (`bh plugin herdr ...`)

| Command | Wraps | Notes |
|---|---|---|
| `bh plugin herdr status` | `herdr status`, `integration status` | One-shot health: server running? which agent kinds have hooks installed? |
| `bh plugin herdr add --local PATH` | `herdr plugin link PATH --enabled` | Explicitly link the validated `beadhive.herdr` package during private development; `--managed-ref REF --yes` installs the known GitHub source after release |
| `bh plugin herdr integrate <kind>` | `herdr integration install <kind>` | Explicit opt-in per agent kind — do not auto-install every kind on onboard |
| `bh plugin herdr launch <bead-id>` | exact hive lookup → native `bh work claim` → live reuse or warm agent creation | High-level get-or-create path: the bead ID is the only required input; returns the session, target, and retained native worktree |
| `bh plugin herdr spawn --hive <id> --bead <id> --kind claude` | existing worktree → `workspace create` (or reuse) → `pane split` → `agent start` → warm-up pass | Low-level escape hatch when the caller intentionally prepared the claim and worktree itself; accepts the same explicit session selection as `launch` |
| `bh plugin herdr dispatch <target> "<prompt>"` | metadata-backed ownership proof → local socket or legacy `agent prompt` → bounded readback | Safe stdin/file input uses Herdr's structured socket acknowledgement; the legacy positional form additionally requires a new exact prompt occurrence in visible pane content |
| `bh plugin herdr watch <target>` | `agent wait --until blocked` | For a dispatcher polling loop: block until an agent needs input or finishes |
| `bh plugin herdr ps` | `agent list` / `api snapshot` | Fleet view: every live herdr-managed agent, its hive/bead if tagged, and its lifecycle state — the natural `bh hive status`-style dashboard row |
| `bh plugin herdr attach <target>` | prints the `herdr agent attach <target>` command | `bh` itself never takes over a TTY; it tells the human operator what to run |
| `bh plugin herdr reap <target>` | exact `pane close` | Cleanup once a bead's dispatch completes; `--pane` accepts the exact locator from a spawn receipt for terminal/idempotent cleanup |

### Lifecycle hooks (fits the existing `Plugin` dataclass)

- `enabled(cfg, entry)` — gate on `shutil.which("herdr")` and the server actually being up
  (`herdr status`), same idiom as `orca.is_available`.
- `on_onboard(ctx)` — only an explicit `--plugin herdr` links the validated local
  `beadhive/herdr-plugin` checkout. Merely detecting a running server never installs code, and
  onboarding never spawns terminal panes or installs per-agent lifecycle hooks.
- `readiness(cfg, entry)` — report whether this hive's agent kind has its herdr integration
  installed, for `bh hive ready`.
- `wt_create` / `wt_remove` — **leave unclaimed.** herdr's `worktree create/open` and `bh`'s own
  worktree management would otherwise double-book the same directories. Native `git worktree`
  remains authoritative, so these hooks stay `None`, as they do for `hitch` and `observaloop`.

### Launch one bead

The normal high-level invocation is:

```bash
bh plugin herdr launch nvhack-lvxi
```

`launch` discovers the exact registered hive, resolves the configured agent kind, verifies that
the kind's Herdr integration is installed, and uses or creates the selected session without
attaching to or focusing the operator's terminal. Only after those preflights
does it enforce the host lease and call the native structured `bh work claim` lifecycle. An open
bead is claimed and provisioned; the current actor's existing claim/worktree is reattached. A
foreign claim, closed or missing bead, unsupported kind, missing integration, unavailable
session, or ambiguous hive is refused with a staged remedy.

The optional overrides are `--hive`, `--kind`, `--session`, `--as`, `--adopt-expired`, `--direction
right|down`, `--focus/--no-focus`, and `--json`. Direction defaults to `right`; no-focus is the
safe default. `--adopt-expired` uses the normal non-forced host-adoption core only for a released
or expired lease. It never seizes an active foreign lease; forced takeover remains the separate,
dangerous `bh host lease adopt <hive> --force` operation.

The launch is idempotent. A proven live target is reused only when its agent name, visible pane
name, `bh:<hive>` workspace, live state, pane, and exact worktree cwd all match. Otherwise a
conflict is refused. Herdr's unique agent-name boundary fences concurrent launches: a loser
closes only the pane it created and returns the proven winner. Later startup or warm-up failure
also closes only that new pane; the successful native claim and worktree are retained and the
error prints status, attach, and retry guidance. No path invokes `herdr worktree create`, `open`,
or `remove`.

For an agent, consume the returned target rather than predicting its encoded name:

```bash
launch_json="$(bh plugin herdr launch nvhack-lvxi --json)"
target="$(printf '%s' "$launch_json" | jq -r '.target')"
bh plugin herdr dispatch "$target" "Implement the claimed bead and submit it for review."
bh plugin herdr watch "$target"
bh plugin herdr attach "$target"  # prints; never attaches itself
bh plugin herdr reap "$target"    # closes only the proven owned pane
```

JSON stdout is one version-1 document: `schema_version`, `command`, `status`, `disposition`,
`session`, `hive`, `bead`, `kind`, `worktree`, `workspace`, `pane`, and `target`. Read both
`.session` and `.target`; do not
derive it from the bead ID because dotted or long IDs use a deterministic collision-resistant
Herdr-safe encoding.

### Session selection and lifecycle propagation

Every session-aware lifecycle and view command uses `--session` over `BH_HERDR_SESSION` over
`default`:

- Omitting `--session` and leaving `BH_HERDR_SESSION` unset selects exactly `default`, matching a
  normal Herdr startup. It never selects another client's focused session.
- `BH_HERDR_SESSION=NAME` selects that exact session when the flag is omitted. An explicit
  `--session NAME` wins over the environment. For example:

  ```bash
  BH_HERDR_SESSION=team bh plugin herdr ps --json
  bh plugin herdr ps --session bh-supervisor --json  # explicit legacy compatibility name
  ```

- `--session current` and `--session active` are aliases for the calling pane's session. They
  are accepted only with Herdr's injected `HERDR_ENV=1` and `HERDR_PANE_ID`; a named pane uses
  `HERDR_SESSION`, while the original session resolves to `default`. The wrapper never infers a
  session from another client's focus.
- `--session NAME` selects that exact Herdr session. Names use Herdr's ASCII letters, digits,
  dot, underscore, and dash grammar. The command never falls back to `default` or enumerates a
  different live session as a substitute.

`launch` and `spawn` use Herdr's exact-session snapshot as the get-or-create boundary. A stopped
`default` tombstone is operator-owned and is never automatically deleted or recreated. The
legacy explicit `bh-supervisor` name remains the sole reserved recovery name: if that session is
selected and stopped, Beadhive may delete and recreate its tombstone; a concurrent launcher that
wins the recreation race is safely reused. No other stopped named session is deleted
automatically. Instead the failure prints an explicit `herdr session delete NAME` recovery
command so a human can confirm that teardown. Invalid or incompatible session inventory is a
refusal, not permission to guess. These session rules are independent of the host-lease gate: an
active foreign host lease is still never adopted.

The launch result, lifecycle receipts, roster, and pane presentation locators all carry the
resolved session name. The normal path may omit the flag throughout because every command selects
`default`. For a non-default session, keep the same explicit flag or `BH_HERDR_SESSION` value for
`dispatch`, `watch`, `attach`, `ps`, views, and `reap`. Exact propagation prevents a target name
in one session from authorizing an operation against a same-named target in another session.

The linked read-only smoke test proves the installed normal path without creating, deleting,
focusing, or otherwise mutating a pane or registry entry:

```bash
BH_BIN=bh bash scripts/smoke-herdr-default.sh github/beadhive/beadhive
```

It unsets `BH_HERDR_SESSION` for each probe and requires status to be available, presentation to
have an exact `default` workspace correlation, and Crew scope and workspace locators to name
`default`.

### Lifecycle receipts and prompt input

`add`, `status`, `ps`, `spawn`, `dispatch`, `watch`, `attach`, and `reap` accept `--json` and
return the shared [lifecycle receipt v1 schema](schemas/herdr-lifecycle-receipt-v1.schema.json). Successful
and refused operations use the same additive envelope: `operation_id`, operation, outcome,
disposition, observation time, exact hive/bead identity where known, Herdr session and locator,
capabilities, warnings, retained resources, and a structured error on failure. Callers may pass a
safe `--operation-id`; otherwise `bh` mints one. Exit 0 means a successful observation, mutation,
or defined no-op. Exit 1 means a runtime failure, timeout, stale target, or authority refusal.
Exit 2 means invalid input. Error codes, not messages, are the machine decision surface.

Read operations (`status`, `ps`, `attach`, and `watch`) are idempotent. After warm-up, `spawn`
rereads the authoritative roster and returns `created` or `reused` only when the exact target and
pane are currently idle, bh-owned, and dispatch-advertised. A blocked, terminal, missing, moved,
or ambiguous startup is a failure. The new pane is closed exactly; if that close fails, its pane
and target are listed in `retained_resources`. `reap` without `--pane` keeps the strict live proof.
A caller holding a spawn receipt may pass its exact `.pane` with `--pane`; this permits cleanup of
that same explicitly plugin-marked blocked or terminal pane and makes an already-absent pane a
successful `already_reaped` no-op. Partial, stale, foreign, and ambiguous matches remain refusals
that preserve every pane and worktree. `dispatch` is intentionally non-idempotent. A verified
instruction returns `dispatched`; an unverified delivery returns
`dispatch_unverified` with `retryable: false`, because blindly retrying could create a duplicate
turn. Both mutating commands rebuild the current live roster and require the same metadata,
managed-worktree, session, workspace, pane, and lifecycle proof used by their advertised actions
before changing anything. An operation ID is correlation, not permission to replay a dispatch.

Use stdin or a file for prompt-bearing automation:

```bash
printf '%s' "$prompt" | bh plugin herdr dispatch "$target" --stdin --json
bh plugin herdr dispatch "$target" --prompt-file /private/path/instruction.txt --json
```

These modes read at most 1 MiB plus one sentinel byte, reject invalid UTF-8, and send valid text
through Herdr's local NDJSON socket, so the prompt body appears in neither the `bh` argv nor a
child `herdr` argv. Receipts, errors, and default logs never include prompt or transcript content.
Delivery proof requires a response with the matching request ID and an `agent_prompt` result in
an expected terminal state; server error detail is replaced with a stable redacted failure.
Visible pane readback is bounded and cannot contain every valid 1 MiB prompt. The positional
`PROMPT` form remains for human compatibility but necessarily appears in process arguments, so
automation must not use it for sensitive content. Because that
legacy transport has no structured acknowledgement, it still requires a new exact prompt
occurrence in the before/after visible-pane read. Exactly one of positional `PROMPT`, `--stdin`,
or `--prompt-file` is required.

### Live roster and correlation

`bh plugin herdr ps --json --session NAME` returns the version-1 live roster documented by
[`herdr-agent-roster-v1.schema.json`](schemas/herdr-agent-roster-v1.schema.json). The complete
snapshot is embedded in the shared lifecycle receipt and scoped explicitly to the selected
authoritative session. Each agent carries its target, canonical hive and bead, lifecycle
timestamps, managed worktree and branch, and Herdr workspace/tab/pane locator. The document
validates as both a `ps` lifecycle receipt and the roster extension contract. Each agent has a
deterministic revision over those correlation facts, including both the observed pane cwd and the
expected managed-worktree path. The roster has a deterministic aggregate revision over its ordered
agents. Lifecycle actions use that atomic roster revision as their precondition, while the
per-agent revision remains the exact row identity. Consumers use those revisions to invalidate
stale actions, topology projections, pagination cursors, and view streams whenever the underlying
lifecycle or ownership proof changes.

New launches write `bh.plugin.herdr/v1` ownership metadata to the workspace and pane through
Herdr's metadata API. Pane tokens carry the exact hive, bead, and opaque target; this is what
makes dotted or long bead IDs recoverable when the visible target is hashed. The roster also
recognizes pre-metadata legacy `bh-<bead>` targets, but only when the visible pane name,
`bh:<hive>` workspace, exact managed-worktree cwd, target spelling, and unique live identities
all agree. A reserved-looking name alone never proves ownership.

Ownership is reported as `owned`, `stale`, `unknown`, or `foreign`. Missing worktrees and
conflicting locators retain any explicit association for diagnosis but disable every advertised
agent operation. Unrelated Herdr panes remain visible as foreign with no inferred hive or bead.
Consumers should use the capability records rather than interpreting lifecycle strings or
reconstructing identity from a target.

Each roster agent also embeds a presentation-neutral `facts` record conforming to
[`beadhive-agent-facts-v1.schema.json`](schemas/beadhive-agent-facts-v1.schema.json). It carries
the exact harness, Beadhive role, work operation/phase, explicit terminal-phase flag, exact
parent relation, and direct/total active-descendant counts. Missing parents, ambiguous joins,
cycles, and unavailable supervisor data degrade coverage instead of becoming invented roots or
zeroes. Its retirement receipt is advisory and revision-scoped, with stable refusal reasons for
live or retained work, pending review/operations, and retained children. `reap` remains the
mutation authority and re-reads live ownership immediately before closing a pane. The generic
record intentionally contains no Herdr workspace, pane, metadata-token, or layout vocabulary.

### Herdr view projections

The Deck plugin consumes eight additive version-1 JSON projections. They are deliberately
presentation adapters over the generic hive summaries, work queues, exact bead detail,
advertised actions, and live roster above; they do not decide readiness, ownership, or mutation
authority themselves.

```bash
bh plugin herdr view picker --limit 50 --json
bh plugin herdr view deck --hive github/beadhive/beadhive --width 140 --json
bh plugin herdr view bead --hive github/beadhive/beadhive --bead bh-123 --json
bh plugin herdr view agent --target bh-bh-123 --json
bh plugin herdr view crew --hive github/beadhive/beadhive --json
bh plugin herdr view layout --hive github/beadhive/beadhive \
  --context-json '{"width":100,"height":40}' --json
bh plugin herdr view presentation --hive github/beadhive/beadhive --json
bh plugin herdr view stream --hive github/beadhive/beadhive --limit 50
```

`picker`, `deck`, `bead`, `agent`, `crew`, and `layout` emit one document conforming to
[`herdr-view-v1.schema.json`](schemas/herdr-view-v1.schema.json). Rows contain bounded,
single-line, control-free render tokens and stable entity/action IDs. Advertised invocations are
argv arrays rooted only in `bh plugin herdr`; they are never shell strings. Prompt-bearing
`agent.dispatch` actions declare stdin transport and use lifecycle `dispatch --stdin`, so no
prompt value belongs in the projection or process arguments. Forbidden, unavailable, or unsafe
actions have a null invocation. Lifecycle commands recheck every precondition at invocation.
For generically ready work, the Deck and exact-bead projections further constrain
`work-item.launch` with Herdr-local preflight evidence: CLI and kind availability, installed
integration, the authoritative resolved session, and host-lease ownership. Unknown proof is
`unavailable`, an active foreign lease is `forbidden`, and adopting an expired foreign lease is
`confirmation-required`; these projections never broaden a generic denial.

The picker and Deck are bounded and use opaque, revision-scoped cursors. A cursor from another
scope is refused, and a cursor whose source revision changed requires a fresh snapshot. Missing
factory or Herdr observations are explicit in `coverage`, `freshness`, and `warnings`; the views
never turn unknown counts into authoritative zeroes.

`crew` is an atomic, bounded forest over the exact authoritative resolved-session roster. It maps
generic parent and topology facts once into stable `root-dispatcher`, `direct-agent`,
`direct-child`, `direct-child-dispatcher`, and `orphan` relations; preserves true direct and total
counts; and points only at existing exact workspace/tab/pane/target locators. Its desired tabs
identify real dispatcher and direct-agent terminals without creating, copying, or reparenting
them. The sole synthetic surfaces are one Crew TUI and a right-side Child Stage selector for a
dispatcher with direct children. Duplicate targets, cycles, missing parents, foreign sessions,
locator disagreement, depth overflow, or node overflow make the whole forest partial and remove
navigation and removal authority. The atomic forest therefore refuses continuation cursors.

A direct child receives `safe_removal` only when the complete fresh forest, exact ownership,
terminal Beadhive work phase, and the roster-wide revision of one confirmation-required reap
action all agree. This remains advisory: `reap` re-reads the live roster, closes only the proven
pane without focus, and returns that pre-close roster revision in its correlated receipt. A
consumer must then obtain a newer fresh complete Crew forest proving absence. Only the external
plugin's separately recorded Child Stage ownership may authorize closing an empty Stage; the
projection never asks to close dispatcher, direct-agent, or user panes.

Layout intent is deterministic by terminal width: wide uses three columns, medium uses tabs, and
narrow uses one attention-first list with an overlay inspector. The exact resolved session is
carried through every locator; Board does not own agent panes, Agents does. The picker and agent
actions are session-modal popups. For a resolved hive, supplying `workspace_id` or `pane_id` in
the layout context requests an ordinary companion Deck split targeting the `agents` role and
exact invoking pane: down when narrow, right when medium or wide. Closing the companion closes
its ordinary pane; reopening recreates the split. The version-1 compatibility behavior for
callers that supply only viewport dimensions is the original dedicated `board` Deck tab, with
explicit tab lifecycle and close/reopen intent. An unresolved hive always retains the picker
popup even if Herdr supplied workspace context. The activity tray is an ordinary right split
whose hide operation closes it and whose show operation recreates it; it is not modeled as a
native collapsible.

`stream` emits bounded NDJSON. Every connection starts with a complete Deck snapshot, followed
by zero or more observations. Its cursor is opaque. Missing, malformed, wrong-scope, or stale
`--since` cursors do not suppress the snapshot: the first frame sets `resync_required` and names
the reason so a client can discard old state safely.

`presentation` composes the generic hive identity, managed-worktree inventory, read-only bounded
Dolt comparison, work queues, and agent-facts contracts with the exact live resolved-session
workspace/tab/pane locators. Its
[`herdr-presentation-v1.schema.json`](schemas/herdr-presentation-v1.schema.json) output contains
ready-to-submit argument objects for Herdr's `workspace.report-metadata` and
`pane.report-metadata` APIs: a Herdr-safe stable source ID, unsigned sequence, TTL, and at most
sixteen bounded, single-line, control-free tokens plus an explicit `clearTokens` set. Workspace
tokens include the precomputed `[prefix] org/repo` Space title, affiliation, exact managed-
worktree count, and Dolt ahead/behind counts. A measured zero remains `0`; unavailable or stale
counts remain `-`. Pane tokens include
the precomputed `[harness] bh-role` Agent title, name the exact bead, Beadhive role and phase,
parent/child facts, dispatcher-owned direct-agent count, correlation, and worktree coverage.
Direct developers clear the dispatcher-only managed-agent count. The projection never supplies
idle/working/attention labels or Git tokens: Herdr's built-in status icon and Git branch/
ahead-behind rows remain authoritative, including their host-owned color and theme choices.

New reports use the Herdr-valid source ID `bh.plugin.herdr.presentation.v1`. The policy-level
protocol identifier remains `bh.plugin.herdr.presentation/v1`; the v1 schema continues to accept
that legacy slash-form report source when reading payloads recorded before this correction.

Only a uniquely correlated workspace or bh-owned pane receives a non-null report object. Missing,
ambiguous, and stale correlations remain in the output with exact locators where known and a null
report plus a stable `reason_code`, so a consumer never decorates a guessed resource. The
projection is read-only: it does not
invoke either metadata API, report a semantic agent identity, change a Herdr lifecycle state, or
advance Beadhive work. Consumers should submit a report before `expires_at`, keep sequences
monotonic per source and resource, and let Herdr remove that source's display metadata after the
TTL when refresh stops.

### Choosing Task/Agent or herdr

Use the in-process **Task/Agent** route for ordinary fire-and-forget subagent work that the
operator does not need to inspect while it runs. It remains the default dispatcher path.

Use **herdr** only through an explicit `bh plugin herdr launch <bead-id>` when the operator wants
a separately billed, persistent terminal agent they can inspect, attach to, or steer live. Use
the lower-level `spawn --hive --bead --kind` only when claim/worktree preparation is deliberately
external. Herdr complements Task/Agent; it does not reroute normal fanout, own Beads or Git
worktree lifecycle, or advance bead state. The complete ownership decisions are recorded in
[the herdr integration ADR](design/herdr-integration-adr.md).
