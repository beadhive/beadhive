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
| `bh plugin herdr integrate <kind>` | `herdr integration install <kind>` | Explicit opt-in per agent kind — do not auto-install every kind on onboard |
| `bh plugin herdr spawn --hive <id> --bead <id> --kind claude` | `workspace create` (or reuse) → `pane split` → `agent start` → warm-up pass | The core primitive: stand up a pane for one bead in one hive, cwd'd to that hive's worktree |
| `bh plugin herdr dispatch <target> "<prompt>"` | `agent prompt --wait --timeout` + post-hoc content verification | Wraps the finding above — never trust `--wait` alone on a pane's first prompt |
| `bh plugin herdr watch <target>` | `agent wait --until blocked` | For a dispatcher polling loop: block until an agent needs input or finishes |
| `bh plugin herdr ps` | `agent list` / `api snapshot` | Fleet view: every live herdr-managed agent, its hive/bead if tagged, and its lifecycle state — the natural `bh hive status`-style dashboard row |
| `bh plugin herdr attach <target>` | prints the `herdr agent attach <target>` command | `bh` itself never takes over a TTY; it tells the human operator what to run |
| `bh plugin herdr reap <target>` | `pane close` / `workspace close` | Cleanup once a bead's dispatch completes — mirrors `wt_remove`'s hook shape |

### Lifecycle hooks (fits the existing `Plugin` dataclass)

- `enabled(cfg, entry)` — gate on `shutil.which("herdr")` and the server actually being up
  (`herdr status`), same idiom as `orca.is_available`.
- `on_onboard(ctx)` — **do nothing by default.** Unlike orca (a passive registry), starting a
  herdr pane is an active, visible action; onboarding a hive should not spawn terminal panes.
  Leave this hook a no-op (or config-gated) rather than auto-wiring every hive into herdr.
- `readiness(cfg, entry)` — report whether this hive's agent kind has its herdr integration
  installed, for `bh hive ready`.
- `wt_create` / `wt_remove` — **leave unclaimed.** herdr's `worktree create/open` and `bh`'s own
  worktree management would otherwise double-book the same directories. Native `git worktree`
  remains authoritative, so these hooks stay `None`, as they do for `hitch` and `observaloop`.

### Choosing Task/Agent or herdr

Use the in-process **Task/Agent** route for ordinary fire-and-forget subagent work that the
operator does not need to inspect while it runs. It remains the default dispatcher path.

Use **herdr** only through an explicit `bh plugin herdr spawn` followed by `dispatch` when the
operator wants a separately billed, persistent terminal agent they can inspect, attach to, or
steer live. Herdr complements Task/Agent; it does not reroute normal fanout, own worktree
lifecycle, or advance bead state. The complete ownership decisions are recorded in
[the herdr integration ADR](design/herdr-integration-adr.md).
