# Beadhive in a box — the container plane

One agent node in a container: `bh`, `bd`, an embedded Dolt, git, `gh`, git-workspace, and a
place for an agent harness — with the durable state split across four named volumes so each area
can be sized, backed up, and thrown away independently.

**There is no published image.** You bake it locally. `docker compose pull` will never work here,
and `pull_policy: never` in `docker-compose.yml` makes that a clear error rather than a confusing
registry 404. Publishing is deliberately out of scope — it lives in the `bh-erwe` epic along with
the obligations that attach to shipping an image.

## Build it

Baking needs a `docker-container` buildx builder; `just image-builder` creates one if absent and
every image recipe depends on it. buildx itself is pinned in `.mise.toml`.

```bash
just image                # bake core + agent from the RELEASED bh on PyPI, --load into the daemon
just image-local          # same, but with bh built from THIS working tree
just image-cross          # multi-arch; needs QEMU emulators (just image-qemu) — slow
```

`just image-local` is what you want while developing `bh` itself: the released version on PyPI
cannot contain changes that have not shipped yet.

Both recipes bake **both targets by default**, and that default matters. When `image-local`
defaulted to `core` alone, the two images silently drifted a full release apart — same `:dev` tag,
different `bh` — and the symptom was a stale agent image reporting `✗ missing: docker`, which
points at the wrong fix entirely. `just image-drift` compares each image's manifest against the
other and against your checkout:

```bash
just image-drift
#   exit 1  the images disagree with EACH OTHER — not one build
#   exit 2  nothing baked yet (not a failure)
#   behind HEAD is reported but does NOT fail — that is normal mid-session
```

**A second host bakes its own image.** Nothing is transferred between hosts; the recipe is the
distribution mechanism.

### What "proven" means

An image is proven when it passes the component matrix in
[`proof/bh-pc2a.17-image-proof.md`](proof/bh-pc2a.17-image-proof.md) — presence and version of
every component cross-checked against the image manifest, `bh` actually driving `bd`, the Dolt
store surviving a recreate, and authenticated git/gh/git-workspace against a real remote. Scope is
the *combination*: a component reporting `--version` proves nothing about whether `bh` can drive
it.

## The two targets

| target | contains | for |
|---|---|---|
| `core` | `bh`, `bd`, `dolt`, `git`, `gh`, `git-workspace`, `jq`, `yq`, `just`, `python`, `uv` | scheduler / HQ-sync shape — no agent harness |
| `agent` | everything in `core`, plus Node and the means to install a harness | an agent seat |

Every component is pinned in `docker-bake.hcl` with its licence declared, and
`tests/test_component_licenses.py` fails on a pin with no declaration or one outside the allowed
set. See [ASSURANCE](ASSURANCE.md#the-images-own-policy--what-beadhive-redistributes) for the
policy and its limits.

### The harness is not in the image

`agent` ships Node and `bh harness`, not Claude Code. Claude Code's package declares
`SEE LICENSE IN README.md` rather than an SPDX identifier — baking it would make anyone who
publishes the image a redistributor of proprietary software. Install it yourself:

```bash
bh harness list              # what is installed, and on whose terms
bh harness install claude    # names the licence before acting; --yes for headless
```

bh-hsus.1 moved this from `npm install -g` to Anthropic's own native installer — the same one
`curl -fsSL https://claude.ai/install.sh | bash` runs on a bare host — because npm was never how a
real machine gets this binary and was quietly building a second, PATH-shadowing copy next to an
already-present native install. The native installer writes under `~/.local` (not `~/.claude`), so
**whether it survives `down && up` in THIS container is no longer proven** — that install path
predates the switch. Filed and tracked as **bh-h5if**, not verified here. `--version` (or
`$BH_CLAUDE_CODE_VERSION`, which the image still sets) pins the target for that first bootstrap
only; `claude update` owns it from there. Codex *is* baked: it declares Apache-2.0 and is freely
redistributable — the rule is about proprietary components, not permissive ones.

## The four volumes

The split is achieved entirely with environment variables `bh` already honours, so it is
configuration and needs no `bh` code.

| volume | mount | variable | role |
|---|---|---|---|
| `bh-hq` | `~/.beadhive` | `BH_HOME` / `BH_HQ` | durable, git-backed |
| `bh-workspace` | `/workspace` | `GIT_WORKSPACE` | rebuildable managed clones |
| `bh-worktrees` | `/worktrees` | `BH_WORKTREES` | scratch, prunable |
| `bh-harness` | `~/.claude` | `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GH_CONFIG_DIR`, npm prefix | durable, secret-bearing |

`~` is the agent user's home. That user is a **build arg** (`AGENT_USER`, default `bees`, UID/GID
`8335:8335`), so the two home-relative mounts are written as `/home/${BH_AGENT_USER}` in compose
rather than a literal `/home/bees`. **`BH_AGENT_USER` must match the `AGENT_USER` the image was
baked with.** Override one
and not the other and compose mounts volumes at paths the image does not contain — Docker creates
them root-owned and the container comes up healthy but inert.

No single command prints all four; watch each where it is consumed:

```bash
bh config show              # config path is under BH_HOME; workspace root IS GIT_WORKSPACE
bh hq init                  # reports the store it stood up, at BH_HQ
bh worktree add --dry-run   # prints the BH_WORKTREES-rooted path it would create
docker system df -v         # per-area sizes — the point of splitting them
```

### Why `bh-harness` collects so much

Everything that holds a credential or a login must land on a volume, or a rebuild silently
forgets it while the container still looks healthy. That failure has been found three times —
Codex's `~/.codex`, `gh`'s `~/.config/gh`, and npm's global prefix — each time by accident rather
than by a check. All three now point inside `~/.claude`.

**The gotcha worth knowing:** `CLAUDE_CONFIG_DIR` must equal the harness mount exactly. Point it
somewhere near but not at the mount and `~/.claude.json` lands outside the volume, so sign-in does
not survive a recreate.

## Credentials

| you have | set | notes |
|---|---|---|
| an interactive host | nothing | log in once inside the container; it persists on `bh-harness` |
| a headless host | `CLAUDE_CODE_OAUTH_TOKEN` | generate with `claude setup-token` on any machine with a browser |
| an API billing route | `ANTHROPIC_API_KEY` | the fallback |
| GitHub, any host | `GH_TOKEN` | `gh` reads it directly — no `gh auth login` needed at all |

All are **passed through**, never baked, so a token cannot leak via `docker history` or a pushed
image. Copy `.env.example` to `.env` to set them.

**Nothing here auto-refreshes.** A fine-grained PAT expires and the factory stops cloning with no
warning; `gh auth login` inside the container is durable across rebuilds but still needs a manual
`gh auth refresh` when it lapses. A GitHub App installation token is the only genuinely renewable
option, and minting them is real infrastructure that is deliberately not built here.

**Known limitation — HQ cannot sync from a container.** `bh hq` wires an SSH remote
(`git@github.com:…`, and `git+ssh://` for the Dolt transport), while the container's only
credential is an HTTPS token. Everything else in the image speaks HTTPS, so a container can clone,
onboard and run beads, but not sync HQ. Tracked on `bh-pc2a.30` under `bh-erwe`. Until it lands,
run HQ operations from a host.

On a macOS host, binding `bh-harness` to your own `~/.claude` does **not** carry Claude Code auth —
that lives in the Keychain. Settings arrive, sign-in does not. That asymmetry is why the default is
an empty named volume filled by one in-container login.

## Resources

```yaml
BH_CPUS=4        # the Docker VM is the real ceiling; a cap above its allocation is theatre
BH_MEMORY=4g     # FLOOR: Claude Code's documented minimum is 4 GB. Do not go below.
BH_PIDS_LIMIT=1024
```

Read your VM's actual size first with `docker info --format 'CPUs={{.NCPU}} Mem={{.MemTotal}}'`.
Two agents do not fit in a 6 GB VM — grow the VM rather than shrinking the cap to make them "fit".
The PID limit is a blast-radius guard against a runaway fork loop, not a tuning knob.

## Services and reserved seams

The compose file defines **one** service, `bh`. The others are reserved as a comment rather than
as stub services, because a stub that fails the moment its profile is enabled reads as a shipped
feature while being dead config.

| seam | status | blocked on |
|---|---|---|
| `ui` | **not available** — no image exists, licensed or otherwise | `bh-xls2.6` defers the self-host release |
| `scheduler` | **not available** — `bh` has no such command yet | out of scope |
| `dolt` | real today, but **not a profile** — it becomes a *core service* | `bh-erwe.3` |
| `obs` | real today, stays **separate** — drive it with `bh otel up` from the host | decided, `bh-pc2a.35` |

`obs` stays separate for a measured reason: compose interpolates required `${VAR:?…}` variables in
services behind an **inactive** profile, so folding an optional stack in takes the whole file down
for everyone not using it. An `include:`d file is interpolated just as eagerly — only `-f`
composition stays lazy.

`dolt` is the opposite case. Under the shared-server direction it stops being optional and becomes
the database the container's `bd` talks to, which is why its required-password guard is correct
rather than a footgun. **The container runs embedded Dolt today**; `bh-erwe.3` moves it, and that
will change the first-run shape described below.

## Running it

```bash
just image-local            # bake
docker compose up -d
docker compose exec bh bash
```

First run inside the container, in this order — the sequence matters:

```bash
bh config init              # a fresh volume has no config.yaml; `bh config show` fails until this
bh setup check              # reads the image manifest instead of probing; exits 0 unattended
bh hive onboard gh/<org>/<repo> --clone-url https://github.com/<org>/<repo>
bh hive ready -v
```

`bh setup check` gates most verbs. `config`, `doctor` and `harness` are exempt so a fresh install
can bootstrap, diagnose itself, and install a harness before the gate passes.

`bh` never drives a container runtime from inside the container — the host's Docker socket is
deliberately **not** mounted, since mounting it would hand the container host root. `bh dolt up`
and `bh otel up` refuse in here and tell you to run them on the host.

## The adoption ladder

**Stage 0 — one repo on a laptop.** Bake, `compose up`, onboard one hive. No HQ: one repo does not
need a fleet registry, and first run deliberately excludes it.

**Stage 1 — HQ plus a remote.** Once a second repo exists, `bh hq init` stands up the Factory HQ
store and `bh hq clone` brings it to another host. See [HQ](HQ.md). Run this from a host, not the
container, until `bh-pc2a.30` lands.

**Stage 2 — a factory host.** A second machine bakes its own image, clones HQ, and adopts hives
from the laptop. `bh host provision` runs the whole adoption path idempotently, probing before
each step.

### Handing a hive between hosts

The lease is the claim, and it is renewable and fenced — a stale host cannot write after losing it.

```bash
# on the outgoing host
bh host lease release <hive>        # yields THIS host's lease (a tombstone; the epoch survives)
bh host lease release --all

# on the incoming host
bh host lease adopt <hive>          # fences the hive's remote, then leases it in HQ

# inspect
bh host list --lease-hive <hive>    # who holds what, and whether they are stale
bh host lease                       # the leases THIS host holds
```

Decommissioning a host entirely is `bh host retire` — a guarded, ordered teardown that releases
leases, pushes every hive's beads *and* code, reclaims local clones, and deregisters the manifest.
`--dry-run` previews the full plan with zero mutation; `--backup` snapshots unpushed work first.
It never touches fleet registration; that is `bh hive retire` / `bh hive reclaim`.

## See also

- [HQ](HQ.md) — the Factory HQ store, `hq init` / `hq clone`
- [WORKTREES](WORKTREES.md) — the scratch volume's contents and lifecycle
- [CONFIGURATION](CONFIGURATION.md) — the fleet/host config split and env vars
- [ONBOARDING](ONBOARDING.md) — what `bh hive onboard` actually does
- [ASSURANCE](ASSURANCE.md) — what the image redistributes, and on what terms
