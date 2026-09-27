# Integrations

Authors of new optional integrations should start with the
[PluginManifest v1 authoring and isolation guide](PLUGIN-AUTHORING.md). This page documents the
current built-in operator surfaces; the authoring guide owns the kernel contract, conformance
workflow, and compatibility-facade removal ledger.

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
```

Per-hive overrides live on the `managed_repos` entry (`orca: {enabled: true}`) and the `enabled`
flag is set with the generic feature-flag verbs: `bh hive enable orca <hive>` /
`bh hive disable orca <hive>`. `orca.worktrees` is **retired** — see
[Orca worktree delegation retired](#orca-worktree-delegation-retired).

### What it reads

orca's state file is **`orca-data.json`** — default `~/Library/Application Support/orca/
orca-data.json` on macOS, `~/.config/orca/orca-data.json` elsewhere (overridable via
`orca.data_path`). It holds three collections — `repos`, `projects`, and `projectHostSetups` —
and **`bh` only ever reads/writes the `repos` list and the `settings` object directly**:

- `repos` — a list of registered repos; each entry carries a `path`. `bh` lists them via
  `orca repo list --json` when the orca CLI is on `PATH`, else by reading `orca-data.json` directly.
- `settings.autoRenameBranchFromWork` — a **global**, UI-only setting; `bh` only ever writes it
  through the dedicated `fix-settings` verb (below).

`bh` never reads or writes `projects` / `projectHostSetups`, and never touches any orchestration
database.

### What it unlocks

- **Repo registration on onboard** — `bh hive onboard … --plugin orca` (or with orca enabled in
  config) registers the freshly onboarded clone with orca via `orca repo add`.
- **`bh plugin orca sync`** — walks the real on-disk clones exactly three levels under
  `$GIT_WORKSPACE` (`provider/org/repo` dirs containing `.git`) and registers any not yet known to
  orca. Idempotent: a second run adds nothing. `--dry-run` previews without writing.
- **`bh hive ready`** — shows an `orca` readiness line (registered / not registered) when
  enabled; a hive that still sets the retired `orca.worktrees` reads `warn`, naming the fix.
- **`bh plugin orca fix-settings`** — flips `settings.autoRenameBranchFromWork` to `false` in
  `orca-data.json`, but *only* while `orca status` shows the runtime down — a safe write window
  where the live app isn't holding the file open. It refuses (exit 1, with an instruction to use
  Orca's Settings UI) when the runtime is up, and preserves every other key when it writes
  (atomic temp-file + rename).

### Orca worktree delegation retired

Orca used to take over worktree **create** and **remove** (`orca worktree create` /
`orca worktree rm`) when `orca.worktrees` was set, through the generic `wt_create` / `wt_remove`
plugin hooks. That made Orca a second owner of worktree mechanics, which the worktree-manager ADR
([bh-mr9tk.2](design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md)) forbids: **exactly one
configured `worktrees.manager` creates, attaches, and removes worktrees** — native git, the default
and only legal value (see [WORKTREES.md](WORKTREES.md#the-worktree-manager)). bh-055ot.1 retired the
delegation, the `wt_create` / `wt_remove` hooks themselves, the `orca.worktrees.fallback` knob, and
the onboard/sync `worktree-base-path` wiring.

Setting `orca.worktrees` (globally or on a `managed_repos` entry) **no longer changes anything
about worktrees**. It is not silently ignored either: every `bh` invocation logs an
`orca_worktrees_retired` config warning naming each scope that still sets it, and `bh hive ready`
shows the orca line as `warn`. Remove `orca.worktrees` from your config to clear both.

### Scope & gating

- **repos + settings only.** `bh` confines itself to orca's `repos` list and the `settings`
  object — `projects` / `projectHostSetups` and any orchestration DB stay out of scope, by design.
- **Gating.** orca's own `enabled` flag is the only gate (bh-hsus.4 removed the old AND-gate on
  `git_workspace.enabled` — git-workspace is a required dep now, always present, so there was
  nothing left for it to test). The retired-`orca.worktrees` warning is likewise AND-gated on
  `orca_enabled`.
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
explicitly rather than defaulted): `wt_create`'s contract was delegating the **git worktree
create subprocess itself** (return the created path, or `None` to fall through to native `git
worktree add`) — hitch never creates a git worktree, so it would always return `None`, and the
generic `_consult_wt_create` fence treated any other exception as best-effort (warn + fall
through), which would silently mask exactly the preflight failures this integration must fail
loudly on. (bh-055ot.1 has since retired `wt_create` altogether — a plugin declaring it is now
refused — so the decision stands for a stronger reason.) Build/launch happens only inside the
explicit `up` verb, matching hitch's own
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
