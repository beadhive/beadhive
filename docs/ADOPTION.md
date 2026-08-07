# Adoption — the four rungs

This is the **depth** axis: it works, so what is the next rung and what does it buy? It is not
about *how* you install `bh` — that is [INSTALL.md](../INSTALL.md)'s route comparison (managed
path / PyPI / Docker), and this document deliberately does not restate it.

Four rungs. Each one names what it **buys**, what it **costs**, and a **"you are here if"**
probe you can actually run — so you can jump straight to your rung instead of reading this
top to bottom.

**They are not one straight line, and reading them as one sends you through work you do not
need:**

- **Rung 3 is orthogonal.** The managed toolchain is about tool *integrity*, not reach. Take it
  before rung 2, after rung 4, or never.
- **Rung 4 hard-requires rung 2.** A second machine joins by **cloning HQ**, and you cannot
  clone an HQ that exists only on one laptop. There is no way around this one.

| Rung | Shape | Buys | Costs |
|---|---|---|---|
| **1** | one laptop, local-only | the whole loop on one repo | no backup; no second machine yet |
| **2** | HQ has a remote | backup + more than one repo | a private repo, and a push discipline |
| **3** | the managed toolchain (nix) | four driven tools, pinned together | root for the nix install; ~130s, 2–3 GB cold |
| **4** | a Linux executor in HQ | a machine that keeps working while you sleep | rung 2 first; a VM to run |

---

## Rung 1 — one laptop, local-only

You are trying it out on one repo. This machine is **both** the supervisor interface and the
HQ, and HQ has no remote — that is the posture, not an omission.

```sh
bh config init                              # scaffold ~/.beadhive
bh mcp install                              # Claude Code: claude mcp add bh --scope user
bh hq init                                  # local-only HQ; no remote wired, deliberately
bh hive onboard <provider>/<org>/<repo>     # zero-footprint by default
bh work ready
```

From a checkout of this repo, `just local-install posture=laptop` runs the same shape.

**Buys.** The whole loop — plan, dispatch, review, merge — against one repo, on one machine.
Nothing is held back at this rung; every `bh work` verb works.

**Costs, stated.** No backup of HQ, and no second machine until a remote is wired.

**You are here if:**

```sh
bh hq status    # "has no remote configured" → rung 1
```

---

## Rung 2 — HQ remote: backup, and more than one repo

The graduation step, and the one most people reach for the day HQ becomes worth keeping.

Create an empty **private** repo for HQ under your account or org, then:

```sh
bh hq init --create     # or wire hq.remote yourself, then:
bh hq push              # publishes BOTH halves; refuses if no remote is configured
bh hq status            # ahead/behind for the git half AND the Dolt half
```

With a remote wired, more than one hive starts paying off:

```sh
bh hive onboard <provider>/<org>/<repo>   # …and repeat
bh sync                                   # hydrate the cross-hive aggregate
bh hq bd ready                            # one ready list across every hive
bh hq intake                              # fleet-wide untriaged inbox
```

**Buys.** A durable backup of HQ (the git half *and* the Dolt half), the cross-repo aggregate
view, and the precondition for rung 4.

**Costs.** A private repo, and a push discipline — `bh hq push` is not automatic.

**Related.** `bh backup usage` for what the three backup roots are consuming;
`bh hq restore --list` for what a pre-push snapshot can get you back.

**You are here if:**

```sh
bh hq status    # prints git + dolt ahead/behind instead of "no remote configured"
```

---

## Rung 3 — the managed toolchain

**Orthogonal to rung 2 — take it in any order, or skip it.** This rung is not about reach at
all. `bh` drives four other tools — `bd`, `dolt`, `gh` and `git-workspace` — and this is the
rung where they are installed and **version-pinned together** by `flake.lock`, rather than
being whatever the machine happened to have.

Starting from nothing, this is [INSTALL.md's managed path](../INSTALL.md#managed-path-recommended)
— it carries the nix installer (needs root: a system daemon, and an APFS volume on macOS) and
the pinned flake reference. One source for that, not two.

If you already have `bh`, the shortcut needs no checkout and no flake reference:

```sh
bh setup toolchain    # installs bd, dolt, gh, git-workspace from the pinned flake
bh --version          # after any reinstall: must print the released version, not exit 0
bh setup check        # 4 of 4 — that is the whole point of this rung
```

**Buys.** `bh setup check` at **4 of 4**, pinned, reproducible. No hand-installing `bd` from
HEAD and discovering six weeks later that your machine and your VM disagree.

**Costs.** Root for the nix install; ~130 seconds and 2–3 GB cold (measured, Apple Silicon),
almost all of it download rather than compilation.

**Not available on.** Intel macOS — gone from nixpkgs, so there is no managed path there at
all. Linux arm64 evaluates but has not been run in anger.

**Coming from ad-hoc PyPI.** [UPGRADING.md's "Ad-hoc PyPI → the managed
path"](UPGRADING.md#ad-hoc-pypi--the-managed-path-any-version) is the ordered migration, and
`bh doctor` detects the legacy install plane and says so.

**You are here if:**

```sh
bh setup check    # 4 of 4 → rung 3. Anything less is an unmanaged toolchain.
```

---

## Rung 4 — a Linux executor, adopted into HQ

The laptop stops executing and starts supervising. **Requires rung 2** — the new host joins by
cloning HQ from its remote.

On the VM, with `bh` installed:

```sh
bh host provision --role executor    # clones HQ from the wired remote, then adopts this host
```

From a checkout, `just local-install posture=host answers=host.yaml` runs the same path
unattended.

### Roles — one axis

A role says **how readily and how long a host holds a hive's host lease**. That is the whole
axis; it is not a permission grade.

| Role | Tenure | For |
|---|---|---|
| `executor` | 4× the lease TTL (2h on the 30-minute default) | an always-on machine that owns repos; the mature shape is one per repo |
| `transient` | baseline TTL, releases on exit | CI-runner shaped — spun up per task |
| `viewer` | **never primary, by definition** | human laptops. Cannot claim, submit or merge; `bh host lease adopt` refuses before touching either remote |

```sh
bh host list                       # every host, with role and staleness
bh host list --lease-hive <hive>   # who holds that hive's lease
bh host lease adopt <hive>         # become primary: fence the remote, then lease it in HQ
bh host lease release <hive>       # or --all
```

### Filing is not executing

The split that makes the shape work — and the reason a `viewer` laptop is still useful. A
**top-level** bead gets a randomly minted id, so two hosts filing at once merge as an additive
union; a `--parent` child comes from a per-parent counter, and two hosts allocating one
concurrently mint the *same* id. That is where the line is drawn:

| Any host, no lease needed | Requires the hive's lease |
|---|---|
| `bh bd create` — a new **top-level** bead | `claim`, `assign`, `submit`, `merge` |
| every read: `ready`, `list`, `show`, `brief`, `sync` | `bh plan file`, and any `--parent` create |
| **moving the store**: `bd dolt push` / `pull` / `fetch` / `status` | changing a bead that already exists |
| | `bd dolt remote add` / `remove` |

### Not finished yet

- Lease enforcement is advisory until the epoch fence fires again — `bh-ban1j`.
- A provisioned host cannot run an agent seat until provision installs the plugin — `bh-tx2hp`.
- The file-here / execute-there cycle is unproven until the E2E runs — `bh-i7ws9`.

**Buys.** A machine that executes while your laptop sleeps, with one owner of execution per
hive and everyone else still able to file into it.

**Costs.** Rung 2 first, a VM to keep running, and one lease decision per hive.

**You are here if:**

```sh
bh host list    # two hosts, neither stale. One host means you are still on rung 1 or 2.
```

---

## See also

- [INSTALL.md](../INSTALL.md) — the install **route**: managed path, PyPI, Docker.
- [ONBOARDING.md](ONBOARDING.md) — the fresh-machine walkthrough, Phases 0–6.
- [UPGRADING.md](UPGRADING.md) — moving between versions and between install routes.
- [HQ.md](HQ.md) — what Factory HQ is and what it stores.
- [HIVES.md](HIVES.md) — onboarding, hive kinds, prefix and identity derivation.
