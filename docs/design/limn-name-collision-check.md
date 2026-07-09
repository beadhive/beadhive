# Name collision check — beadhive / bh

> Status: **findings, not a decision.** Feeds the naming ADR. This bead
> only tracks reservation status; nothing here is actually registered/purchased yet.

Checked 2026-07-09 against live registry/whois APIs (see commands below each table). "Free"
means the registry returned a not-found response at check time — it is not a purchase-time
guarantee (names can be claimed by someone else before we register).

## Package registries — `beadhive` / `bead-hive` / `beadhivecli`

| Registry | `beadhive` | `bead-hive` | `beadhivecli` |
|---|---|---|---|
| PyPI | **free** (404) | **free** (404) | **free** (404) |
| crates.io | **free** | **free** | **free** |
| npm | **free** (404) | not requested | not requested |

Checked with:

- PyPI: `curl -s -o /dev/null -w '%{http_code}' https://pypi.org/pypi/<name>/json`
- crates.io: `curl -s -H 'User-Agent: <contact>' https://crates.io/api/v1/crates/<name>`
  (crates.io's API rejects requests with no identifying User-Agent — set one)
- npm: `curl -s -o /dev/null -w '%{http_code}' https://registry.npmjs.org/<name>`

**All clear.** No blocking collision on the canonical name across any registry checked.

## `bh` — the binary/short-token name

The bead's acceptance criteria asks for `bh` "confirmed free of collisions on
PyPI/npm/crates.io/Homebrew and as a common shell alias." Actual result: **not fully free** —
`bh` is already a published package name on three of the four registries:

| Registry | `bh` status | What's there |
|---|---|---|
| PyPI | **taken** (200) | `bh` — "Fuzzy Linear Discriminant Analysis", v0.0.9, low-traffic/likely-dormant |
| npm | **taken** (200) | `bh` — "Template engine. BEMJSON => HTML processor" (Yandex BEM tooling), latest v4.2.1, multi-version history — not dormant-looking |
| crates.io | **taken** | `bh` — "BountyHub CLI", v0.6.0, updated 2025-11-20, ~39 recent downloads — actively maintained |
| Homebrew core formula | **free** (404) | — |

Additionally, GitHub code search (`gh search code "alias bh="`) turns up `bh` used as a
personal shell alias across many unrelated dotfiles repos (`brew home`/`brew help`,
BloodHound, `git branch`, `buildhtml`, tmux/game launchers, etc.) — no single dominant
convention, but confirms `bh` is a commonly-aliased short token, i.e. real (if soft) risk of
local muscle-memory collision for individual users. There's also one exact-name GitHub repo
`claudiob/bh` ("Bootstrap Helpers for Ruby", 832★, unrelated) — doesn't block an org/repo
name, just another data point that `bh` is a popular short handle.

**What this does and doesn't block:** these are all separate, unrelated, currently-published
projects — none can be defensively reserved out from under their owners. This does **not**
block using `bh` as *our* binary/console-script name, since a package's registry name and its
installed executable name are independent (e.g. the npm package `typescript` installs the
binary `tsc`). It **does** mean:

- We cannot publish a package literally named `bh` on PyPI, npm, or crates.io — those slots
  are permanently occupied by unrelated maintainers.
- The package name stays `beadhive` (PyPI/crates.io) with `bh` exposed only as the console
  script / cargo binary name inside that package — which requires no separate registry
  reservation and has no collision.
- Homebrew: a `bh` formula name is free today, but note the token is popular as a personal
  alias — expect some users to need to override or unalias `bh` locally; not something we can
  fix from our end.

**Recommendation:** proceed with canonical package = `beadhive`, binary = `bh` inside that
package. Do not attempt to reserve a standalone `bh` package on PyPI/npm/crates.io — it isn't
available and isn't required for the `bh` binary name to work.

## GitHub

| Target | Status |
|---|---|
| org `beadhive` | **free** (404 via `GET /orgs/beadhive`) |
| user `beadhive` | **free** (404 via `GET /users/beadhive`) |
| repo `beadhive/workspace` | **free** (org doesn't exist yet, so implicitly free) |
| org `beadhive-workspace` (fallback) | **free** (404) |

**All clear.**

## Domains

WHOIS isn't reliably reachable via `curl`, but the `whois` binary was available in this
environment and returned real, referral-chased registry responses (not just the IANA TLD
stub):

| Domain | Registry queried | Result |
|---|---|---|
| beadhive.ai | whois.nic.ai | "Domain not found" — **appears unregistered** |
| beadhive.io | whois.nic.io | "Domain not found" — **appears unregistered** |
| beadhive.org | whois.publicinterestregistry.org | "Domain not found" — **appears unregistered** |

Treat as a strong signal, not a final guarantee — confirm again at actual registrar
purchase-flow time (a domain can be registered between this check and checkout, and some
registrars/registries return ambiguous responses for privacy-protected or on-hold domains).

## Overall recommendation

- **Canonical published name = `beadhive`** stands: no blocking collision on PyPI, crates.io,
  npm, GitHub org/repo, or (per whois) the three target domains.
- **`bh` as the binary name inside the `beadhive`/`beadhivecli` package is fine to proceed
  with** — it needs no separate registry reservation.
- **`bh` as a standalone reservable package name on PyPI/npm/crates.io is NOT available** —
  three unrelated, currently-published packages already hold it. This is a real (if narrow)
  gap against the bead's literal acceptance criterion; flagging for the human operator /
  naming ADR rather than resolving unilaterally, since it may affect how
  aggressively we want to advertise `bh` as *the* short name for the tool versus just its
  binary.
- No blocking issue found that would force reconsidering `beadhive`/`bh` as the chosen brand.
