# Release channel branches ADR — `latest` on every publish, `stable` on promotion

**Status:** proposed · **Date:** 2026-08-07 · **Decision bead:** `bh-7daa6.1` · **Epic:** `bh-7daa6`
**Supersedes:** nothing · **Amends:** no other ADR · **Retires:** bead `bh-wp6h`, whose premise this
answers rather than satisfies (Decision 4)
**Related:** [deployment-isolation-direction-adr.md](deployment-isolation-direction-adr.md)
(Decision 5 — the managed local-install path this flake ref serves),
[toolchain-declaration.md](toolchain-declaration.md), `INSTALL.md`, `src/beadhive/setup.py`

Records why `bh`'s install instructions stop naming a version and start naming a **channel**, what
the two channels mean, and which three alternatives were measured and rejected. Decided by the
operator on 2026-08-07 after researching how the Nix ecosystem actually distributes channels.

Every measurement below was taken on 2026-08-07 against beadhive @ `4a6c810` with the released
version at **0.8.4**, and is reproducible from the command shown.

---

## Context — a hardcoded pin does not go stale as a risk

`INSTALL.md` tells every new user to install the toolchain from a literal tag:

    nix profile install github:beadhive/beadhive/v0.8.0#default

`v0.8.0` was four releases behind the published `0.8.4`. Nothing in CI notices when that drifts,
and nothing ever will, because the drift is not an error state: it is the *normal* state between a
release and someone remembering to hand-edit a document.

That is the whole argument. A hardcoded pin does not go stale as a **risk**; it goes stale as a
**certainty**, on a schedule set by the release cadence. `bh-wp6h` — "keep the hardcoded tag in
sync" — was filed to bail that boat. This ADR replaces the boat.

### Where the pin lives — four sites, and why a reader may count three

**`bh-7daa6.7` must retire four sites:**

| file:line | what it is |
|---|---|
| `INSTALL.md:23` | the prose comment defending the tag ("Tag refs are immutable") |
| `INSTALL.md:29` | the machine-readable `kind: script` install entry an agent executes |
| `INSTALL.md:115` | the human instruction |
| `README.md:43` | mirrors `INSTALL.md:115` **byte for byte**, deliberately |

**A reader who greps `main` today will find only the first three**, and should not conclude this
document is wrong. `README.md:43` was written by epic `bh-r59o1`, which rewrote the README's
install section and merged that work into **its own container branch** (`wt/bead/epic/bh-r59o1`) —
not into `main`. It reaches `main` at `bh work finish bh-r59o1`, which lands before this molecule's
docs bead runs. So the count is **three on `main` as of `4a6c810`, four on the tree `bh-7daa6.7`
will actually edit**, and four is the number that bead is correctly written against. Verify with:

    git grep -n 'github:beadhive' wt/bead/epic/bh-r59o1 -- README.md INSTALL.md

**The general pattern is worth naming, because it caused two false findings during this work:** an
epic's merged children are invisible from `main` until that epic finishes. Cross-epic claims — file
contents, line numbers, and especially `blob/main/...` links — must be checked against the branch
that will be in the tree at execution time, not against `main`.

The byte-identical duplication between `README.md:43` and `INSTALL.md:115` is itself an argument
for this ADR: the pin will shortly live in two files that must be edited together, and the only
thing keeping them in agreement is that someone remembers both. A channel ref removes the reason to
edit either.

---

## The model — two branches, both forward-only

| branch | moved by | can it rot? | who points at it |
|---|---|---|---|
| `latest` | CI, fast-forwarded to every tag that **successfully publishes** | **no** — every release moves it | install docs, new users |
| `stable` | explicit promotion only (human or agent), never automatic | **yes** — silently, the moment nobody promotes | operators who opt in |

A channel is a **named ref that lags the tip on purpose**. It is not "the newest commit" and not
"the newest tag"; it is the newest artifact that passed the gate the channel is defined by. For
nixpkgs that gate is Hydra. For us it is `needs: publish` — the PyPI publish job — so `latest` can
only ever name a version that actually reached PyPI. A release that fails to publish leaves the
channel where it was.

That gating is why the channel must move on the **CI side of the tag push**, not inside
`just bump` → `cz bump`. `cz bump` runs locally and creates the annotated `v$version` tag
(`tag_format = "v$version"`, `annotated_tag = true` in `pyproject.toml`); at that moment nothing
has been published yet, so a channel moved there would be naming a version that may never exist on
PyPI.

## Decision 1 — both channels are forward-only, and that is a simplification

`stable` never rewinds. If a release turns out bad, the fix is a **patch that `stable` rolls
forward onto**, not a demotion to an older version.

This is deliberately recorded as a *design* property, not merely a policy, because three
simplifications fall out of it and a future "let's allow demotion" request should have to overturn
all three on purpose:

1. **Promotion is a fast-forward**, so the promotion workflow can refuse anything that is not one —
   outright, as a hard error — rather than force-pushing behind a guard it also has to get right.
2. **Branch protection needs no force-push exception.** A channel that could rewind would need one,
   and that exception would then be available to every other actor with write access.
3. **A consumer's `git fetch` is always sufficient.** Branches fast-forward on a plain fetch with no
   flags; the entire failure mode described in [Rejected: a moving git tag](#rejected-a-moving-git-tag)
   exists only for refs that move backwards or sideways.

Demotion is not thereby impossible — it is *out of band*: delete and re-create the branch as a
deliberate, audited act, not a supported verb.

## Decision 2 — install docs point at `latest`, and `stable` is the one that can rot

Install docs point at **`latest`**, not `stable`. This is against the usual convention and it is
the section most likely to be "fixed" by someone acting on general principle, so the reasoning is
written out.

**The asymmetry.** `latest` **cannot rot**, structurally: every release moves it, and the act that
would leave it stale — publishing a release — is the same act that advances it. There is no state
in which someone forgot. `stable` **can rot**, silently, the moment someone forgets to promote —
and a `stable` that nobody has promoted for four releases is **exactly the hardcoded-pin problem
relocated, not solved**. It even fails the same way: quietly, correctly-looking, with no CI signal.

So the instinct that "stable is the safer default" is inverted here. `stable` is safer *per
install* and riskier *over time*; `latest` is the reverse. While adoption is early, the population
we are protecting is new users who should get current features and track them, and the failure we
are protecting against is a documented ref that nobody notices has frozen. That points at `latest`.

Two things follow:

- **Do not switch the docs to `stable` as a tidy-up.** Switching is a real decision that needs a
  real reason — a support burden from users on the newest release, or a cadence fast enough that
  `latest` outruns the docs describing it. Recording that reason means amending this decision.
- **`stable`'s rot is a monitored condition, not an accepted one.** `bh-7daa6.6` puts it in
  `bh doctor`: a `stable` lagging the newest published version, or either branch pointing at a
  commit that is not a release tag, is a reportable finding. Rot we can see is a different object
  from rot we cannot.

## Decision 3 — build the whole mechanism now and dogfood it on 0.8.5

Seeding, the `latest` automation, the `stable` promotion path, the doctor check and the docs switch
all land in this molecule. The next release is the proof, not a follow-up bead: `bh-7daa6.8` cuts
0.8.5 and records what the automation actually did, against what this ADR says it should do.

The ordering constraint is that **neither branch exists yet**. `latest` self-seeds on the next
release, but the docs cannot switch before the branch exists or `INSTALL.md` ships a ref that 404s
for every reader in the interval. Seeding is therefore its own bead (`bh-7daa6.4`), ordered before
the docs bead (`bh-7daa6.7`), and the push path has to be confirmed against repo rulesets first
(`bh-7daa6.5`) — a channel CI cannot push is a channel that silently stops advancing, which is the
`stable` rot mode arriving through the back door.

One mechanical trap is already visible in the file and is called out here so it is not rediscovered
in a red run: `.github/workflows/release.yml` declares `permissions: contents: read` at workflow
level, and **job-level `permissions` replace rather than extend** the workflow-level block — the
existing `publish` job re-lists `contents: read` for exactly this reason. The new channel job needs
`contents: write` and will 403 without it.

## Decision 4 — this supersedes `bh-wp6h`

`bh-wp6h`'s premise is "keep the hardcoded tag in sync". That premise is answered by "stop
hardcoding it", so the bead is closed into this molecule (`bh-7daa6.9`) as superseded rather than
worked as written. Any sync mechanism built for it would have been a second thing to keep correct,
whose failure mode is identical to the one it exists to prevent.

---

## Prior art — measured, not recalled

Every claim below was re-verified on 2026-08-07 rather than transcribed, because the whole decision
rests on "this is what the ecosystem does" and that is a claim, not an axiom.

### We already consume a channel branch — this is not a new pattern for us

**`flake.nix:19` reads `inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable"`.** That is a
*branch* in the ref position — precisely the shape this ADR is asking consumers to adopt — and
`flake.lock` records it resolved to rev `e72e4f2`. Nix accepts a branch, a tag or a rev in that
slot identically.

So the decision is not "adopt an unfamiliar distribution model". **It is: stop asking our users to
do something we ourselves do not do.** Every `bh` build already depends on a mutable channel branch
advancing correctly, and has since the flake was written. This is the single strongest argument
available and everything below is corroboration of it.

### The rest of the ecosystem

**nixpkgs channels are named branches gated on Hydra.** `git ls-remote --heads NixOS/nixpkgs`
returns `nixos-unstable`, `nixos-unstable-small`, `nixos-25.11`, `nixos-26.05` and the historical
series back to `nixos-20.03`, plus `-aarch64` and `-small` variants. The gap is the point:

    gh api repos/NixOS/nixpkgs/compare/nixos-unstable...master
    → {"status": "ahead", "ahead_by": 1237, "behind_by": 0, "total_commits": 1237}

`master` is 1,237 commits ahead of the channel and **zero behind** — the channel is a strict
ancestor, i.e. forward-only, advanced only after Hydra's builds and tests pass. That `behind_by: 0`
is the load-bearing half: it is the same invariant Decision 1 adopts, observed in the largest
deployment of this pattern.

**home-manager does the same at single-project scale.** `release-25.05`, `release-25.11`,
`release-26.05` alongside `master` — the same shape without a Hydra, which is the scale we are at.

**npm and Docker both separate a mutable name from an immutable id.** Live from the npm registry
for `typescript`: `latest → 7.0.2`, `next → 7.1.0-dev.20260807.1`, alongside `beta`, `rc`,
`insiders`. The dist-tag is a *pointer*; the version it names is immutable and can never be
rewritten. Docker does the same with tag → `sha256:` digest.

**A git tag is the one common identifier that collapses those two layers.** It is simultaneously
the human-facing name and the artifact id, with no second layer underneath to stay immutable. That
is not a defect of git tags — it is what makes `v0.8.4` mean something — but it means making one
move removes immutability from the exact layer that exists to provide it. A branch has the same two
layers as npm and Docker do: a moving name (`latest`) over an immutable object (the commit, and the
`v0.8.5` tag on it). That is why the channel is a branch.

---

## Rejected: omit the ref

`github:beadhive/beadhive#default` resolves the repository's **default branch**, which is not "the
latest release" and is not gated on anything.

    git log --oneline v0.8.4..main | wc -l   → 4

`main` was 4 commits ahead of the published `v0.8.4`. Combined with the adjacent instruction —
`uv tool install beadhive`, which pulls the **released wheel** — that gives every new user
toolchain-from-`main` + bh-from-release, and the skew is invisible because both commands succeeded.
The comment already in `INSTALL.md:23` makes this argument, and it is correct; the note in
`src/beadhive/setup.py:490-492` measured the same default branch **31 commits** stale on 2026-08-06.

The honest detail, because it cuts the other way:

    git diff v0.8.4..main -- flake.lock flake.nix   → empty

Those 4 commits changed neither the flake nor its lock, so on 2026-08-07 the omitted ref would have
installed a **byte-identical** toolchain. The skew is **latent, not active**. That is the argument
for fixing it now: the failure is currently free to fix and its cost is entirely in the future, on
a date nobody chooses, discovered by whoever is least equipped to diagnose it — a first-time user
whose two install commands both printed success.

## Rejected: a moving git tag

Mechanically this works: nix treats branch, tag and rev identically in a `github:` flakeref, so
`github:beadhive/beadhive/latest#default` would resolve. It fails operationally and socially.

**`git fetch` will not update an already-fetched tag, and mostly will not say so.** Measured on git
2.55.0 with a local upstream whose `latest` tag and `chan` branch were both moved from `c1` to `c2`
after the clone:

| in the clone | branch `chan` | tag `latest` | output |
|---|---|---|---|
| `git fetch origin` | c1 → **c2** | c1 (unchanged) | mentions only the branch — **silent** on the tag |
| `git fetch --tags origin` | — | c1 (unchanged) | `! [rejected] latest -> latest (would clobber existing tag)` |
| `git pull` | fast-forwarded | c1 (unchanged) | mentions only the fast-forward |
| `git fetch --force --tags origin` | — | **c2** | `t [tag update] latest -> latest` |

Only the last form moves it. **Be precise about which rows are silent**, because the shorthand
"git keeps a stale tag silently" is only true of two of them: bare `git fetch` and `git pull`
advance the branch and say *nothing at all* about the stale tag, while `git fetch --tags` is
conspicuously **loud** — it just refuses. So a reader who runs `--tags` and sees the `! [rejected]`
line has not caught this document overstating the case; that row is the best case, and a refusal is
still not an update.

The consequence stands either way: a consumer who fetched the channel once keeps that version
forever unless they know to add `--force --tags`, and on the default path nothing tells them. A ref
whose whole purpose is to move must be a ref kind that moves under the command people actually run.

**It would put two species of tag in one namespace.** `src/beadhive/setup.py:496`'s
`toolchain_flake_ref()` builds immutable `v{version}` tags, and `INSTALL.md:23`'s comment leans
explicitly on "Tag refs are immutable" to justify the current pin. Adding a mutable `latest` tag
makes that sentence conditionally false — true for `v*`, false for the channel — and every reader
and script now has to know which species it is holding. Splitting the namespace by prefix
convention is possible and is exactly the second thing to keep correct that Decision 4 rejects.

## Rejected: FlakeHub, now

FlakeHub is the closest thing in this ecosystem to npm dist-tags, and it is **deferred by operator
decision, not dismissed**. It gives flakes real semver *ranges* — `*`, `1.*`, `1.2.*`, `=0.1.15` —
at `https://flakehub.com/f/:org/:project/:constraint`, for tagged or rolling releases. Verified
live on 2026-08-07:

    curl -sIL 'https://flakehub.com/f/NixOS/nixpkgs/*'
    → 301 https://api.flakehub.com/f/NixOS/nixpkgs/*
    → 307 https://api.flakehub.com/f/pinned/NixOS/nixpkgs/
             0.2605.1011297+rev-445d861c6d31b4af0c79d8d4be2331f762a361d7/…/source.tar.gz

That is the mutable-name → immutable-id separation done properly: the constraint `*` is a query,
and what comes back is a version *and* a rev. Note the resolved rev `445d861c…` is byte-for-byte
the head of `refs/heads/nixos-26.05` measured in the same minute — FlakeHub's answer for nixpkgs
*is* the channel branch, republished with a version number attached.

Deferred because:

1. **Two fixed channel names are the actual requirement.** Nobody has asked for `0.8.*`. Ranges
   solve a problem we do not have, and pre-1.0 they solve it weakly: `major_version_zero = true`
   means a breaking change bumps the *minor*, so `0.*` spans breakage and `0.8.*` is a range of
   patches — a narrower question than the one ranges are for.
2. **It adds a third-party service to the bootstrap path.** The channel exists to be the first
   thing a user with nothing installed touches; a branch ref adds no availability dependency beyond
   the GitHub we are already cloning from.
3. **It is not exclusive with this.** Channel branches and FlakeHub publication can coexist; a
   later FlakeHub push would republish the same tags.

**Revisit when** any of: (a) users ask to pin a range rather than a channel, most likely once 1.0
makes `1.*` meaningful; (b) two fixed channel names stop being enough — a third or a
per-minor-series channel is proposed; or (c) we want the resolved-rev provenance and cache
integration FlakeHub provides for the managed install path. Any of those makes this a live
question again rather than a settled one.

---

## Scope boundary — `bh setup toolchain` keeps deriving `v{version}`

**The channel is for the bootstrap case only**, where no `bh` is installed yet and therefore no
version exists to derive from. It is not a general replacement for the tag ref, and specifically it
must **not** be propagated into `bh setup toolchain` for consistency's sake.

`src/beadhive/setup.py:496`, `toolchain_flake_ref()`, derives its ref from the **installed package
version** — `f"{TOOLCHAIN_FLAKE_REPO}/v{version}#default"` — and its docstring says so in capitals:
*"THE VERSION IS DERIVED, NEVER TYPED."* That is strictly better than a channel for that call site,
because it resolves to the immutable tag matching the `bh` that is *running*, so the toolchain and
the tool can never disagree. A channel there would reintroduce skew in the one place the code
currently guarantees its absence.

Two guards already exist and should be read as intentional, not incidental:

- `tests/test_flake_toolchain.py::test_the_toolchain_flake_ref_is_a_tag_and_carries_no_version_literal`
  asserts the ref is a `v*` tag and that no version literal appears in `setup.py`.
- The bare-repo fallback (`{REPO}#default`, when the version is unreadable) is documented as honest
  rather than good: an unresolvable version means we cannot name a tag, and a *wrong* tag is worse
  than the default branch because it fails in a way that looks deliberate. Switching that fallback
  to `latest` would make it look correct while still being unpinned, which is worse again.

Summarised: **`INSTALL.md` names a channel because there is no version to derive; `setup.py` names
a tag because there is.**

---

## Consequences

1. **The install docs stop carrying a version.** After `bh-7daa6.7` all four sites read
   `github:beadhive/beadhive/latest#default`, and the release process no longer has a documentation
   step. `INSTALL.md:23`'s comment is rewritten — its "tag refs are immutable" argument survives, as
   the reason `setup.py` still uses one — and `README.md:43` stays a byte-identical mirror of
   `INSTALL.md:115`, which is now cheap because neither line changes again.
2. **The channel lags the tag, on purpose.** Between the tag push and the `publish` job clearing its
   `pypi-prod` approval gate, `latest` names the *previous* release. That window is a feature — it
   is the gate — but it means "the tag exists" and "the channel moved" are different events, and
   anything that asserts equality between them will flap.
3. **A failed publish leaves the channel correct.** No cleanup, no revert, no manual step: the
   channel simply did not move.
4. **CI gains `contents: write` on one job.** Narrowed to the channel job, not the workflow.
5. **`stable` acquires an owner or it acquires rot.** Decision 2 accepts this and Decision 1 makes
   it non-catastrophic (a stale `stable` is old, never wrong); `bh-7daa6.6` makes it visible.

## What this does not settle

- **Who promotes `stable`, and on what trigger.** `bh-7daa6.3` builds the mechanism —
  human- or agent-triggered, forward-only, refusing an unpublished version — but the *policy*
  ("after N days without a regression report") is not decided here and should not be inferred from
  this document.
- **Whether `stable` ever becomes the documented default.** Explicitly a later decision (Decision 2),
  requiring an amendment here.
- **Per-series channels** (`0.8`, `release-0.8`). Not proposed; if they are, re-open the FlakeHub
  comparison at the same time, since that is one of its named revisit triggers.
- **Nothing here has run.** Every measurement above is of the *current* world and of prior art;
  the automation is unbuilt at the time of writing, and `bh-7daa6.8` — cutting 0.8.5 — is the first
  observation of this design in motion. Treat the CI behaviour described in Decision 3 as intent
  until that bead reports.

## The molecule

| bead | role |
|---|---|
| `bh-7daa6.1` | this ADR |
| `bh-7daa6.4` | seed `latest` and `stable` at the current release — ordered before the docs switch |
| `bh-7daa6.5` | confirm CI can push both branches past repo rulesets |
| `bh-7daa6.2` | `release.yml` fast-forwards `latest` after a successful publish |
| `bh-7daa6.3` | `stable` promotion — forward-only, refuses an unpublished version |
| `bh-7daa6.7` | point every `INSTALL.md` flake ref at `latest` |
| `bh-7daa6.6` | `bh doctor` flags a channel that has stopped tracking |
| `bh-7daa6.8` | cut 0.8.5 and record what the automation actually did |
| `bh-7daa6.9` | close `bh-wp6h` as superseded |
