---
name: setup-git-workspace
description: >-
  First-timer git-workspace walkthrough — explains GIT_WORKSPACE, the
  <provider-type>/<account>/<repo> layout, workspace.toml, and provider tokens (GITHUB_TOKEN /
  GITLAB_TOKEN), then routes to the right git-workspace:* skill based on your starting point.
  Invoked from the AGF setup skill (Phase 5). Covers three starting situations: workspace already
  configured (verify + move on), repos exist on disk but not managed (safe import with backups
  before any git workspace update), and greenfield (install + configure providers from scratch).
  Triggers on "set up git-workspace", "first time git-workspace", "what is GIT_WORKSPACE",
  "workspace.toml explained", "how do I use git-workspace", "I have repos I want to manage",
  "import my existing repos into git-workspace", "git-workspace for beginners",
  "git-workspace walkthrough", "configure git-workspace for ws".
---

# setup-git-workspace — first-timer walkthrough

You are walking a user through git-workspace setup for the first time. This sub-skill is called
from the AGF `setup` skill (Phase 5). Its job is to explain the concepts that make first-time
git-workspace setup confusing, then route the user to the right `git-workspace:*` skill for their
situation. Re-runs are safe — probe before acting.

---

## Step 0 — ensure the git-workspace skills are available

The `git-workspace:*` skills that this walkthrough delegates to ship in a separate plugin. Check
whether they are installed before proceeding:

```bash
claude plugin list | grep -q 'git-workspace'
```

If the plugin is **not** found (exit 1), install it now:

```
/plugin marketplace add briancripe/claude-plugins
/plugin install git-workspace@briancripe-plugins
```

Restart Claude Code if prompted. Once the plugin is present, continue.

---

## What git-workspace is, and why ws uses it

Before picking your starting branch, read this section. Most first-timer confusion comes from
skipping it.

### GIT_WORKSPACE — the workspace root

`GIT_WORKSPACE` is an environment variable that points to the single directory where all your
repos live. git-workspace reads this to know where to clone things; ws reads it to locate rigs.

```bash
echo "$GIT_WORKSPACE"    # should print the path, e.g. /Users/brian/workspace
```

If it's not set, `git-workspace` falls back to `~/workspace`. The AGF setup skill (Phase 4)
already set this. If it is still unset, add it to your shell profile now:

```bash
# in ~/.zshrc or ~/.bashrc
export GIT_WORKSPACE="$HOME/workspace"
```

Open a new shell (or `source ~/.zshrc`) before continuing.

### The layout — `<provider-type>/<account>/<repo>`

git-workspace does not clone repos flat. It clones into a three-level path:

```
$GIT_WORKSPACE/
  github/
    briancripe/
      workspace/          ← one repo
      dotfiles/           ← another repo
    anthropics/
      claude-code/
  gitlab/
    my-company/
      backend-api/
```

This is `<provider-type>/<account>/<repo>` — the same string ws calls a **triplet**
(e.g. `github/briancripe/workspace`). That triplet is how ws identifies rigs in commands like
`ws rig onboard github/briancripe/workspace`. The layout is not cosmetic — ws depends on it.

If your existing repos are laid out differently (e.g. flat `account/repo` with no provider
prefix), the import path (Branch B below) can optionally migrate them.

### `workspace.toml` — declarative provider config

`workspace.toml` lives in `$GIT_WORKSPACE` and declares which GitHub orgs, GitHub users, GitLab
groups, or Gitea instances to clone. Example:

```toml
[[provider]]
provider = "github"
name = "briancripe"
path = "github"        # repos land in $GIT_WORKSPACE/github/briancripe/<repo>

[[provider]]
provider = "github"
name = "anthropics"
path = "github"
include = ["^claude-.*"]
```

When you run `git workspace update`, git-workspace reads this file, queries each provider's API
for matching repos, and clones anything new. Repos removed from a provider get moved to
`$GIT_WORKSPACE/.archived/`. The file is managed with `git workspace add github/gitlab/gitea`
rather than hand-editing (hand-editing works but `add` keeps formatting consistent).

### Provider tokens — what they're for

git-workspace queries provider GraphQL APIs to discover which repos exist. It can't do that
without a personal access token:

| Provider | Env var | Needed for |
|---|---|---|
| GitHub | `GITHUB_TOKEN` | Any `github` provider in `workspace.toml` |
| GitLab | `GITLAB_TOKEN` | Any `gitlab` provider in `workspace.toml` |

These are **read at shell startup** — they must be exported before any `git workspace` command.
A GitHub Classic token with only the `repo` scope is sufficient. A Fine-grained token needs
`Contents` and `Metadata` (read-only). Never commit them — keep them in a sourced file outside
your dotfiles repo, or in your system keychain.

The `git-workspace:install` skill covers token creation and safe storage in detail.

---

## Which branch are you on?

Run these probes to decide your starting point, then jump to the matching section.

```bash
# Probe 1 — is the binary installed?
command -v git-workspace && echo "installed" || echo "missing"

# Probe 2 — does workspace.toml exist?
[ -f "${GIT_WORKSPACE:-$HOME/workspace}/workspace.toml" ] && echo "found" || echo "missing"

# Probe 3 — do repos already live in $GIT_WORKSPACE?
ls "${GIT_WORKSPACE:-$HOME/workspace}/" | head -5
```

| Your situation | Branch |
|---|---|
| Binary present, `workspace.toml` present, repos already cloned | **Branch A — already good** |
| Repos (any layout) already on disk in `$GIT_WORKSPACE`, but no `workspace.toml` OR no binary | **Branch B — has repos, safe import** |
| No binary, no repos, greenfield machine | **Branch C — nothing yet** |

If you're unsure, choose Branch B — it is the most conservative path and won't lose work.

---

## Branch A — already good

Your workspace is configured and repos are cloned. Verify quickly before moving on:

```bash
# List tracked repos
git workspace list

# Confirm the layout looks like <provider-type>/<account>/<repo>
ls "${GIT_WORKSPACE:-$HOME/workspace}"
```

If `git workspace list` returns repos and the layout has the provider-type prefix
(`github/`, `gitlab/`, etc.), you are in good shape. Nothing further to configure here.

Return to the `setup` skill — rig onboarding is the next step.

**If `git workspace list` fails** (e.g. "no workspace.toml" error), your binary is installed but
the config is incomplete. Treat this as Branch B.

---

## Branch B — has repos, safe import

You have repos on disk in `$GIT_WORKSPACE` but no `workspace.toml`, or git-workspace is not yet
managing them. The risk here is that `git workspace update` could archive or overwrite repos it
does not recognise — so the import skill backs everything up before touching anything.

Load `git-workspace:import`, which guides you through five steps:

1. **Scan** — classify every repo as READY / PUSH_NEEDED / WIP_DIRTY / NO_ORIGIN / etc.
2. **Triage** — decide what to do with each non-READY category.
3. **Back up** — push commits, snapshot dirty state to dated WIP branches, publish no-origin repos.
4. **Verify** — run `verify-safe.sh` to gate before any `git workspace update`.
5. **Migrate** (optional) — move repos to the `<provider-type>/<account>/<repo>` layout if they
   are not already in it.

```
/git-workspace:import
```

or ask the user to load it:

> Load the `git-workspace:import` skill to continue.

**Important:** if git-workspace itself is not installed yet (Probe 1 said "missing"), run
`brew install git-workspace` first (or load `git-workspace:install` for the full install
guide including tokens), then return here.

After the import skill completes and `verify-safe.sh` is green, you are ready for
`git workspace update`. Return to the `setup` skill for rig onboarding.

---

## Branch C — nothing yet

Start from scratch: install the binary, set the env var, add provider tokens, declare your
providers in `workspace.toml`, then clone.

### C1 — install the binary and configure the environment

Load `git-workspace:install`:

```
/git-workspace:install
```

or ask the user to load it:

> Load the `git-workspace:install` skill to continue.

That skill installs the binary (Homebrew is the preferred method on macOS), sets
`GIT_WORKSPACE`, and walks through `GITHUB_TOKEN` / `GITLAB_TOKEN` creation and safe storage.

Come back here after `git-workspace --version` prints cleanly and your token is exported.

### C2 — declare your providers

Create `$GIT_WORKSPACE/workspace.toml` by adding one or more providers. Load
`git-workspace:providers` for the full schema and filter options:

```
/git-workspace:providers
```

or ask the user to load it:

> Load the `git-workspace:providers` skill to continue.

The quick version: `git workspace add` appends a provider block. Run one per GitHub user/org or
GitLab group you want to track:

```bash
# Your own GitHub account
git workspace add github <your-github-username>

# An org you contribute to (with a filter)
git workspace add github <some-org> --include="^your-prefix-.*"

# A GitLab group
git workspace add gitlab <group-name>
```

Each call writes a `[[provider]]` block to `$GIT_WORKSPACE/workspace.toml`.

### C3 — clone the repos

Once `workspace.toml` has at least one provider and your token is exported:

```bash
git workspace update
```

This queries each provider's API, clones new repos into `$GIT_WORKSPACE/<path>/<account>/<repo>/`,
and moves any previously tracked repos that no longer exist on the provider to `.archived/`. On a
fresh workspace there is nothing to archive — only cloning happens.

The first run can take a few minutes if you have many repos; it runs clones in parallel.

Verify the result:

```bash
git workspace list
```

You should see one line per tracked repo. The directory tree under `$GIT_WORKSPACE` should now
match the `<provider-type>/<account>/<repo>` layout.

Return to the `setup` skill — rig onboarding is the next step.

---

## Don't

- Don't run `git workspace update` on a directory full of existing repos without completing
  Branch B's import steps first — `update` can archive repos it does not recognise.
- Don't commit `GITHUB_TOKEN` or `GITLAB_TOKEN` to any repo or dotfiles tracked by git. Keep
  them in a sourced-but-gitignored file or your system keychain.
- Don't point `GIT_WORKSPACE` at an existing repo directory — it should be the **parent** that
  holds clones, not a repo itself.
- Don't expect `git workspace --help <subcommand>` to work; git hijacks `--help` on subcommands.
  Use `git-workspace <subcommand> --help` (the hyphenated form) instead.
- Don't use `cargo install git-workspace` unless no other install method is available — the
  upstream README explicitly warns it is slow. Prefer `brew install git-workspace`.
