# Integrations

`bh` layers on external tools two ways: **deps** (`deps.py`) are required for this version of
bh — always present, no on/off flag — and **plugins** are optional integrations gated by an
`enabled` flag, with a generic `enabled`/`readiness`/lifecycle-hook contract the onboard /
retire / hive-ready flows loop over (see `plugins.py`). **git-workspace is a dep**
(`deps.py`, `required=ALWAYS` — `bh setup check` requires the binary unconditionally); it still
carries its own `bh plugin git-workspace …` sub-app (`gitworkspace_plugin.py`), mounted and
probed directly by `cli.py` / `hive_ready.py` rather than through the plugin registry. **orca**
is a plugin (`orca.py`), and its own `enabled` flag gates it; routing lives in `route.py`.

## git-workspace

[orf/git-workspace](https://github.com/orf/git-workspace) clones a fleet of repos into
**repo groups** — each `[[provider]]` block in `$GIT_WORKSPACE/workspace*.toml` — and tracks
them in `workspace-lock.toml`. A repo group has three distinct parts, easy to conflate but
worth keeping separate (`gitworkspace.RepoGroup`):

- **`provider`** — HOW you auth + fetch: the transport/discovery mechanism (`github`/`gitlab`/
  `gitea`).
- **`name`** — WHICH account/org the group queries (`RepoGroup.account`).
- **`path`** — the group's on-disk folder segment (`RepoGroup.path`) — this, not the provider
  type, is the first segment of a hive's identity triplet: **`<group>/<account>/<repo>`**.

Multiple groups may share one `provider` type (five `github` groups with different
accounts/paths is normal), and a group's `path` may differ from its `provider` (e.g. a
`path="contrib"` group whose `provider="github"`) — `bh` always resolves the real provider
type via the group, never assumes `path == provider`. `bh` already derives hive identity from
the on-disk layout; it also always reads git-workspace's config directly — no separate flag to
turn that on, git-workspace is a required dep (`bh setup check` requires the binary
unconditionally).

### Configuring

```yaml
# ~/.beadhive/config.yaml
git_workspace:
  # path: ~/workspace/workspace.toml   # optional; default: glob $GIT_WORKSPACE/workspace*.toml
  # hive_match: flexible                 # how `bh -r <id>` resolves (see PASSTHROUGH.md)
```

### What it reads

From each `[[provider]]` block in `workspace*.toml` (parsed with stdlib `tomllib` into a
`gitworkspace.RepoGroup`; the `workspace-lock.toml` lock is **not** treated as a config
source):

- `path` (falling back to `provider` when unset) → a recognized `provider:` label — the
  repo-group path, not necessarily the provider type,
- `name` → an `org:` label,
- `skip_forks` / `include[]` / `exclude[]` → parsed and exposed for visibility (git-workspace
  itself enforces these filters; `bh` doesn't re-enforce them).

From `workspace-lock.toml` it reads each repo's clone **URL**, used by the hub to fetch
uncloned hives, and each repo's **`path`** — used both for identity and (via
`bh doctor`) to flag any lockfile entry nested deeper than the `<group>/<org>/<repo>` triplet
(see [Status / diagnostics](#status--diagnostics) below).

> **Gotcha:** `exclude.repos` entries in `config.yaml` are matched against the **group path**,
> not the provider type — `contrib/briancripe/orca` excludes that repo even though its
> `provider` is `github`, and `github/briancripe/orca` would be a *different*, unmatched key.

### What it unlocks

- **Provider auto-load** — `providers:` can be omitted from `config.yaml`; the effective set is
  the union of config + git-workspace's repo-group paths. Org **codes/policies** still come
  from `config.yaml` `orgs:` (absent orgs fall back to `sanitize(name)[:2]` + `personal`).
- **`bh plugin git-workspace groups`** — lists every repo group with its provider type,
  account, and filters (`gitworkspace_plugin.py`).
- **Hive routing** `-a`/`-r` for `bh bd` / `bh git` → see [PASSTHROUGH](PASSTHROUGH.md).
- **Remote-cache hub** for uncloned hives → see [HUB](HUB.md).
- **`bh git workspace …`** central passthrough, with the `--help` reroute → see
  [PASSTHROUGH](PASSTHROUGH.md).

### Scope & gating

- **Hives vs all repos.** `-a` targets **registered hives** (`managed_repos`). To act on *every*
  cloned repo (hive or not), use git-workspace's own runner: `bh git workspace run -- <cmd>`.
- **No gating flag any more.** git-workspace is a required dep (bh-hsus.4 deleted the old
  `git_workspace.enabled` toggle), so `-a`/`-r` and provider auto-load are never blocked on a
  config flag — `-a`/`-r` can still fail if `managed_repos` itself has nothing to resolve.

### Per-group auth

Each repo group may authenticate differently — a distinct SSH host alias / deploy key
(`url.<alias>.insteadOf`), a per-directory identity or signing key (`includeIf "gitdir:
<workspace>/<group>/"` blocks), or a distinct `gh` account. `bh` **reads** (never writes)
global git config to report, per group, which of these applies: `bh doctor` shows a
per-group auth table (effective `user.name`/`user.email`/`signingkey`, any `insteadOf` alias
covering its repos, and whether an `includeIf gitdir:` block scopes it), warning — never
erroring — when a group has no scoped identity or two groups silently share one
(`gitauth.py`). Writing that config stays out of scope: custodian/homelab provisioning owns it.

### Lifecycle roadmap (design intent, not yet built)

The hub + minimal-clone cache is the foundation for keeping most hives remote until needed:

1. **Import** git-workspace providers → register hives (first-time setup).
2. **Add remote-only** hives and browse their issue graphs via the hub (no code clone).
3. **Clone down to work** — configure git-workspace from a hive's info + `git workspace update`
   to materialize the checkout and wire beads for live work.
4. **Release** — when done, verify branches are clean and beads is pushed, then remove the
   repo from git-workspace to reclaim disk (the hive stays registered + viewable via cache).

Also deferred: `bh config import-orgs` (write stub org entries); high-level verbs coordinating
a git branch + its beads issues together.

## Orca

orca is a separate repo-registry tool that keeps a list of known repos in a JSON store. `bh` can
register its git-workspace clones with orca so orca's own tooling sees them. orca is the **first bh
plugin**: the generic `bh plugin` seam (`plugins.py`) drives it through the onboard / retire /
hive-ready lifecycle, so nothing about orca is hardcoded into those flows.

### Enabling

```yaml
# ~/.beadhive/config.yaml
orca:
  enabled: true
  # data_path: ~/.config/orca/orca-data.json   # default: platform-aware, see below
  # worktrees: true                            # opt in to worktree delegation (see below)
  # worktrees:
  #   enabled: true
  #   fallback: false                          # true = degrade to native git when orca fails
```

Per-hive overrides live on the `managed_repos` entry (`orca: {enabled: true, worktrees: true}`) and
the `enabled` flag is set with the generic feature-flag verbs: `bh hive enable orca <hive>` /
`bh hive disable orca <hive>`. A hive entry's `orca.worktrees` wins over the global `orca.worktrees`
(bare bool or `{enabled, fallback}` mapping); `orca.worktrees.fallback` itself is global-only.

### What it reads

orca's state file is **`orca-data.json`** — default `~/Library/Application Support/orca/
orca-data.json` on macOS, `~/.config/orca/orca-data.json` elsewhere (overridable via
`orca.data_path`). It holds three collections — `repos`, `projects`, and `projectHostSetups` —
and **`bh` only ever reads/writes the `repos` list and the `settings` object directly**:

- `repos` — a list of registered repos; each entry carries a `path`. `bh` lists them via
  `orca repo list --json` when the orca CLI is on `PATH`, else by reading `orca-data.json` directly.
- `settings.autoRenameBranchFromWork` — a **global**, UI-only setting (see
  [Worktree delegation](#worktree-delegation) below); `bh` parses it read-only except through the
  dedicated `fix-settings` verb.

`bh` never reads `projects` / `projectHostSetups` directly, and never touches any orchestration
database. The one deliberate exception is CLI-only: worktree-delegation wiring drives
`orca project setups` / `setup-update` (never the data file's `projects`/`projectHostSetups`
keys) to point a repo's project-setup at bh's shadow worktree dir — see below.

### What it unlocks

- **Repo registration on onboard** — `bh hive onboard … --plugin orca` (or with orca enabled in
  config) registers the freshly onboarded clone with orca via `orca repo add`.
- **`bh plugin orca sync`** — walks the real on-disk clones exactly three levels under
  `$GIT_WORKSPACE` (`provider/org/repo` dirs containing `.git`) and registers any not yet known to
  orca. Idempotent: a second run adds nothing. `--dry-run` previews without writing.
- **`bh hive ready`** — shows an `orca` readiness line (registered / not registered, or the
  worktree-delegation readiness states below) when enabled.

### Worktree delegation

With `orca.worktrees` on for a hive, `bh worktree` hands new-branch **create** and **remove**
(`bh worktree rm` / `prune`) to `orca worktree create` / `orca worktree rm` instead of plain
`git worktree`, so the tree shows up managed in Orca's desktop/mobile UI at bh's own
`wt/bead/<type>/<id>` path + branch convention.

- **Delegation policy — hard fail by default.** If a delegated create/remove fails (orca down, a
  bad result, a path/branch mismatch), `bh` raises rather than silently falling through to native
  git — a silently-broken delegation must never masquerade as success. Set
  `orca.worktrees.fallback: true` to relax this to warn-and-fall-back-to-native instead.
- **Attach and `verify-` trees are never delegated.** Only the *new-branch* create path can be
  taken over by orca; re-attaching an existing branch into a fresh dir always stays native (a
  warning is printed if a delegating plugin is enabled), and the ephemeral `verify-*`
  clean-checkout worktrees used by `bh work check`/`submit` bypass the delegation seam entirely —
  they're not a durable seat.
- **`keep_branch` semantics on remove.** orca's `worktree rm` deletes the tree's checked-out
  branch outright, even without `--force`. `bh worktree rm` (the durable-branch path) detaches
  HEAD first so the branch survives; `bh worktree prune` (already-merged, disposable branches)
  skips the detach so orca's delete matches native prune's own branch cleanup.
- **Readiness states** (`bh hive ready`, once `orca.worktrees` is on): `ok` when the orca runtime
  is reachable (`orca status --json`) and `settings.autoRenameBranchFromWork` is off; `warn`
  otherwise, naming every problem (runtime down — delegation will hard-fail or fall back per the
  `fallback` knob; or auto-rename is on).
- **Onboard/sync worktree-base-path wiring.** When `orca.worktrees` is on, `bh hive onboard` and
  `bh plugin orca sync` best-effort point the hive's orca project-setup `worktree-base-path` at
  `config.worktrees_root()/<provider>/<org>` (orca appends `<repo-displayName>/<leaf>` itself
  under its default `nestWorkspaces: true`, landing delegated trees exactly at bh's own worktree
  dir). This is onboarding bookkeeping, not the hard-failing hooks above — it warns and
  continues on any failure (missing CLI, no matching project-setup, a failing `setup-update`).
- **Auto-Rename Branch From Work.** `settings.autoRenameBranchFromWork` is a **global**, UI-only
  orca setting (default ON) that renames branches after agent startup — left on, it fights bh's
  `wt/bead/...` naming convention. There's no per-repo CLI override, so:
  - onboard/sync print an operator instruction to disable it by hand in Orca's Settings UI
    whenever it's on and worktree delegation is enabled;
  - **`bh plugin orca fix-settings`** flips it to `false` directly in `orca-data.json`, but
    *only* while `orca status` shows the runtime down — a safe write window where the live app
    isn't holding the file open. It refuses (exit 1, same Settings-UI instruction) when the
    runtime is up, and preserves every other key when it writes (atomic temp-file + rename).

### Scope & gating

- **repos + settings only** (plus the CLI-only project-setup exception above). `bh` confines
  itself to orca's `repos` list and the `settings` object — `projects` / `projectHostSetups`
  and any orchestration DB stay out of scope, by design.
- **Gating.** orca's own `enabled` flag is the only gate (bh-hsus.4 removed the old AND-gate on
  `git_workspace.enabled` — git-workspace is a required dep now, always present, so there was
  nothing left for it to test). Worktree delegation (`orca_worktrees_enabled`) is still
  AND-gated on `orca_enabled`.
- **Retire names the de-registration verb, WARN-only.** `orca project setup-delete --setup <id>`
  does de-register a repo — but retire only *prints* the command (with `orca project setups
  --json` for finding `<id>`) rather than running it, since auto-deleting a project-setup on
  retire risks dropping orca state the operator wanted to keep. `bh` never mutates
  `orca-data.json` to fake a removal.
- **Best-effort.** A missing orca CLI, an unreadable data file, or a failing `orca` subprocess
  degrades to a warning; it never aborts onboarding, retire, or hive-ready. The worktree
  delegation hooks (`create`/`remove`) are the deliberate exception — see above.

## hitch

[agent-hitch](https://github.com/briancripe/agent-hitch) resolves a **Hitch Pack** seat profile
into a harness-specific **Config Directory** and launches a harness against it. It is the bh-side
half of `docs/design/managed-harness-config-adr.md` (see Amendment 2 in particular): an OPTIONAL
plugin, off by default, exposed **only** through `bh plugin hitch up <target> <profile>` — never
an implicit step inside `bh work` or `bh role`, and never a change to bh's existing default
launch path. With hitch disabled, absent from PATH, or crashing on invoke, `bh role <seat>`
behaves exactly as it always has — `beadhive.role` contains zero references to this plugin.

### Enabling

```yaml
# ~/.beadhive/config.yaml
hitch:
  enabled: true
  repo: ~/workspace/github/briancripe/agent-hitch   # the agent-hitch checkout providing
                                                     # profiles/local.yaml + catalogs/local.yaml
                                                     # + packs/
  # command: hitch        # override the hitch CLI command/path
  # root: ~/.beadhive/hitch   # persistent Config Directory root (ephemeral: false only)
```

No AND-gate on another plugin (unlike orca, which requires git-workspace): hitch shares no
data or state with git-workspace / orca / observaloop.

### `bh plugin hitch up <target> <profile>`

```sh
bh plugin hitch up claude dispatcher
```

Translates bh's own harness vocabulary (`claude` | `opencode`, matching `bh role --harness`)
into hitch's own `up` target names — determined empirically, not assumed: hitch's CLI accepts
`claude-code`/`opencode`, not `claude` — then shells out to the real `hitch up <target>
<profile> --profiles-file <repo>/profiles/local.yaml --catalog <repo>/catalogs/local.yaml
--root <config-dir-root>` with **inherited stdio** (interactive hand-over, mirroring `bh role`),
propagating hitch's own exit code verbatim.

**Binding mechanism — determined empirically (settles ADR Amendment 1's open question).** The
Config Directory `hitch up` builds for `claude-code` is a full standalone `$CLAUDE_CONFIG_DIR`
tree (`skills/`, `commands/`, `agents/`, `hooks/`, a merged `settings.json`), and `hitch up` execs
`claude` with only `CLAUDE_CONFIG_DIR` pointed at it — confirmed by reading agent-hitch's own
`_up_claude_code`/`profile_build_claude_config_dir.py`, and by the tool's own generated
`README.md` inside the built directory ("no `claude plugin marketplace add` / `plugin install`
step is needed"). Neither the build nor the launch reads or writes the operator's personal
`~/.claude` — `bh` adds nothing on top, so that property is inherited, not re-implemented.

**Ephemeral by default.** The Config Directory root (`--root`, registry + build output only —
`--profiles-file`/`--catalog` are absolute paths into `hitch.repo`, unaffected) mirrors
`config.worktrees_root()` exactly: ephemeral (default, matching `worktrees.ephemeral`) ⇒
`<os-temp>/bh-hitch`; persistent ⇒ `hitch.root` (or `~/.beadhive/hitch`). Whether a given
(profile, target) pair is rebuilt within that root is hitch's own "build if absent, reuse if
present" call (Amendment 1), not reimplemented here.

**Fails loudly, never falls back.** A preflight failure inside `hitch up` (missing binary,
unsupported OS, …) exits nonzero; `bh plugin hitch up` propagates that exit code as-is — no
retry, no silent fallback to ambient `~/.claude` or to `bh role`.

### `wt_create` is deliberately NOT used for provisioning

Evaluated and rejected (recorded per bh-og0q.5's acceptance bar, which asks this to be decided
explicitly rather than defaulted): `wt_create`'s contract is delegating the **git worktree
create subprocess itself** (return the created path, or `None` to fall through to native `git
worktree add`) — hitch never creates a git worktree, so it would always return `None`, and the
generic `_consult_wt_create` fence treats any other exception as best-effort (warn + fall
through), which would silently mask exactly the preflight failures this integration must fail
loudly on. Build/launch happens only inside the explicit `up` verb, matching hitch's own
already-implemented "build if absent, launch" idiom — see `hitch_plugin.py`'s module docstring
for the full reasoning.

### Scope & gating

- **Disabled by default**, gated on `hitch.enabled` (per-hive override on `managed_repos`, same
  shape as `orca`/`observaloop`).
- **Readiness is silent when disabled.** `bh hive ready` reports `na` for hitch without ever
  probing it, when `hitch.enabled` is off — an optional integration that nags when unused is not
  optional (ADR Amendment 2).
- **No onboard/retire hook, no worktree delegation.** hitch only acts inside its own explicit
  `up` verb.

## Status / diagnostics

`bh doctor` reports how the integration and the registry line up — see
[DIAGNOSTICS](DIAGNOSTICS.md).
