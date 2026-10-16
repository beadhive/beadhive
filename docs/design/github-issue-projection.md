# GitHub Issues projection into the public store (bh-lniwf.6)

**Status:** implemented · **Date:** 2026-10-16 · **Bead:** `bh-lniwf.6` · **Epic:** `bh-lniwf`
**Implements:** [two-store-scope-adr.md](two-store-scope-adr.md) section 3 (public store:
GitHub-authoritative, pull-only, stable `external_ref` identity).
**Absorbs:** the mapper-limit scope of `bh-7vu4` and `bh-t9pe` (both closed as superseded by
this bead; see [Superseded beads](#superseded-beads)).

## What it does

```sh
bh hive project-issues <hive> [--dry-run] [--json]
```

pulls the hive's GitHub Issues into the hive's configured **public** bead store. It runs
Beads' own GitHub integration and nothing else:

```text
bd -C <public store root> github sync --pull-only --prefer-github [--dry-run]
   env: BEADS_DIR=<public store>/.beads  GITHUB_OWNER=<org>  GITHUB_REPO=<repo>
        GITHUB_TOKEN=<ambient credential, if bh resolved one>
```

Implementation: `src/beadhive/github_projection.py`; the argv is the named route
`beadhive_bd_cli.github_pull_only_argv`; the guard is `guard.public_store_github_refusal`.

## Off by default

Nothing runs unless **both** hold: the hive declares `stores.public` with
`source: github-issues` and `sync: pull-only` (see
[CONFIGURATION.md](../CONFIGURATION.md#stores)), and an operator invokes the verb. There is no
schedule, hook or daemon trigger.

## Store selection — public only

- The store is resolved with `store_target.resolve_store(entry, "public")`. A legacy
  single-store hive, or a partitioned hive without a public store, raises
  `StoreNotConfigured`: the projection never falls back to the internal or legacy store.
- The public store must exist (`StoreTarget.require_present`), its `source` must be
  `github-issues` and its `sync` must be `pull-only`; `sync: none` disables the projection.
- bd is scoped with `-C <public store root>`. Measured on bd 1.3.0: `bd -C <checkout>` ignores
  `BEADS_DIR` and opens the checkout's own (internal) store, so `StoreTransport` now always
  scopes a detached store by its own root (`tests/test_store_target_scoping_int.py`).

## Owner, repository and credentials

- Owner and repository come from the hive's identity triplet (`provider: github`, `org`,
  `repo`), never from the internal store. They are handed to bd as `GITHUB_OWNER` /
  `GITHUB_REPO` in the child environment only; nothing is written to any config.
- bd prefers `github.owner` / `github.repo` / `github.repository` from the store's own config
  over the environment, so a public store whose bd config names a **different** repository is
  refused before any pull.
- The credential stays ambient: `GITHUB_TOKEN`, then `GH_TOKEN`, then `gh auth token`. With
  none, bd may still use a host-local `github.token` in the public store's `config.yaml`
  (bd keeps that key out of the Dolt database). bh never stores the token, and scrubs it from
  every line it echoes, including bd's error output.

## Pull-only

GitHub is authoritative, so the one admitted shape is
`bd github sync --pull-only [--prefer-github] [--dry-run] [--json]`. Bare `github sync`
(bidirectional), `--push-only`, `github push`, `github pull`, selective `--issues` / `--parent`
and `--prefer-local` / `--prefer-newer` are refused by `guard.public_store_github_refusal` for
every seat. `--prefer-github` makes GitHub win any conflict. No GitHub write API is ever called.

## Identity and idempotence

bd's GitHub mapper keys each issue on `external_ref` = the issue's `html_url`, e.g.
`https://github.com/<owner>/<repo>/issues/<number>` (and `source_system` =
`github:<html_url>:<number>`). A second pull matches on that reference and **updates the bead
in place** — the bead id is kept; title, body, state and labels follow GitHub.

After every real pull bh lists the public store and verifies that each row's `external_ref` is
an issue URL of **this** repository and that no URL appears twice. Any violation is reported
and the command exits non-zero. A dry run previews (`would import` / `would update`) and writes
nothing.

## Failure isolation

The projection only ever runs bd against the public store. A refusal (no public store, wrong
source or sync, conflicting store config) happens before any bd write. A bd failure is reported
with the credential scrubbed. In neither case is the internal store opened, a route changed, or
anything published; tests assert the internal `.beads` is untouched.

## Mapper limits (bd 1.3.0) — documented, not implemented

| GitHub data | In the public store |
|---|---|
| title, body | `title`, `description` — kept in sync |
| state | `open` → `open`, `closed` → `closed`; `status::in_progress` / `blocked` / `deferred` labels set those statuses on open issues |
| labels | unscoped labels copied verbatim (a bare `bug` stays a label and also sets the type); scoped `type::` / `priority::` / `status::` labels are consumed into type / priority / status and **not** kept |
| type | `bug`, `feature` (also `enhancement`), `task`, `epic`, `chore`, `decision`, `spike`, `story`, `milestone` from a bare or `type::` label; anything else becomes `task` |
| priority | from a `priority::` label (`critical` 0, `high` 1, `medium` 2, `low` 3, `none` 4); default `2` |
| assignee | the single `assignee` login; multiple assignees are not represented |
| comments | **not imported** (the bead's `comment_count` stays 0) |
| milestones, projects, reactions | **not imported** |
| pull requests | filtered out by bd (the Issues API lists them too) |
| deletions / transfers | **not represented** — the pull is upsert-only; a deleted or transferred issue leaves its bead behind unchanged |
| locked / pinned / closed reason | not represented |
| bead vocabulary with no GitHub form | gates, molecules, dependencies, deferrals, `model:` / `harness:` labels and other Beadhive conventions never arise from GitHub and are not synthesised |

These records are a **projection**: the public store is not operational, so none of them can be
claimed, assigned or dispatched (`store_target.require_operational`). Turning a public issue
into internal work is the intake loop's job ([external-issue-intake.md](external-issue-intake.md)),
out of scope here.

## Superseded beads

- `bh-7vu4` (local-canonical `bh sync` wrapper over `bd <tracker> sync`, `--push-only`
  default) — **superseded**. Its useful scope — a thin wrapper over bd's native tracker sync
  rather than a bespoke syncer, and the comment / milestone / delete limits — is carried here;
  the push direction is rejected by the two-store ADR.
- `bh-t9pe` (one-shot GitHub import keyed by a `gh:<number>` label) — **superseded**. bd's
  `external_ref` (the issue URL) is the stable identity instead of a provenance label, and a
  re-pull updates in place rather than skipping.

## Tests

`tests/test_github_projection.py` replays **recorded** bd output
(`tests/fixtures/github_projection/`: two pulls, a dry run and the resulting `bd list` rows,
captured from bd 1.3.0 against a scratch store and a local GitHub stub). No test opens a
socket, runs `bd github`, or calls a GitHub API.
