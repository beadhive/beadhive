# Spike `bh-icn6b.2` — compliant Orca delivery and lifecycle

**Bead:** `bh-icn6b.2` · **Seat:** `dev/host-runtime-spike` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-icn6b.3`

## Question

Can `bh` install, pin, upgrade, roll back, verify, and uninstall Orca without silently
accepting terms or exceeding upstream's redistribution grant?

## Method

On 2026-08-28 I read the publisher's repository, MIT license, Terms (last updated
2026-04-04), install/remote-server documentation, and v1.4.190 release. I copied the already
installed v1.4.175 AppImage into `/tmp/orca-spike.HthJMg`, then downloaded the authoritative
v1.4.190 x86-64 AppImage into `/tmp/orca-190-spike`. I exercised a versioned install root,
atomic update/rollback selector, extraction, runtime verification, and uninstall. `/opt/orca`
and user state were never changed.

## Evidence

1. Lovecast Inc.'s public `stablyai/orca` repository calls Orca free/open source and ships
   [MIT](https://github.com/stablyai/orca/blob/main/LICENSE), whose grant expressly includes
   copying and distribution provided the notice is retained. The publisher's
   [Terms](https://www.onorca.dev/terms) identify the application as MIT too. This is factual
   license text; the cache recommendation below is engineering inference, not legal advice.
2. Authoritative [install docs](https://www.onorca.dev/docs/install) publish AppImage and
   `.deb`, retain older GitHub releases, describe stable auto-update and rollback; the
   [latest release](https://github.com/stablyai/orca/releases/tag/v1.4.190) is v1.4.190.
   GitHub asset URLs are version-addressed. GitHub's authoritative asset metadata publishes
   digest `sha256:f5b321576d9c909f9e6987aa3bd20e8ff9f214d881b43c7109281cbc87878cde`
   for `orca-linux.AppImage`; the downloaded 205,952,095-byte file matched it. No detached Linux
   signature was present; provenance is GitHub/Lovecast release hosting plus its asset digest.
3. Desktop and `serve` are the same Linux package. The remote-server docs require installing
   Orca and invoking its bundled CLI; `bh-eqvhe` already measured v1.4.175 serve, Electron
   dependencies, service PATH, Xvfb/pairing/exposure, and AppImage extraction. Those findings
   are reused, not repeated here.

| surface | platform | version discovery | artifact URL | integrity | license ID | redistribute/cache |
|---|---|---|---|---|---|---|
| desktop | macOS arm64/x64, Windows x64, Linux AppImage/`.deb` | GitHub stable/prerelease tags | versioned release asset; latest redirects | computed SHA-256; Linux signature UNKNOWN | `MIT-Lovecast-2026` plus Terms dated 2026-04-04 | permitted by MIT if notice retained |
| `orca serve` | Linux package, with Electron host libraries | same version as desktop package | same asset | same | same | same |

Isolated lifecycle result for v1.4.190 (SHA-256 above), using v1.4.175
(`2b49edcf41a56d7b24bce3eb9d3b5377391d2eee86f96272f391a7e6f02e30f5`) as rollback:

| action | exact isolated operation | result / exit |
|---|---|---|
| install | `install -m 0755` to `root/versions/1.4.190.AppImage` | size/hash matched, rc 0 |
| verify | `--appimage-extract`, inspect updater metadata, then isolated `serve` + `status --json` | extraction rc 0; runtime ready, `appVersion: 1.4.190` |
| update | create `current.next -> versions/1.4.190.AppImage`; `mv -Tf` over selector | readback selected 1.4.190, rc 0 |
| rollback | same atomic selector operation targeting retained 1.4.175 | readback selected 1.4.175, rc 0 |
| uninstall | remove selector and both isolated version files; assert empty | rc 0; active install/state untouched |

Desktop state removal and `serve` state removal are deliberately separate and opt-in; deleting
the executable does not delete user state. Native unattended installer/uninstaller: **UNSUPPORTED**
for AppImage. In-app automatic update cannot satisfy exact desired-state reconciliation.

Delivery choice: **nix**. A derivation may fetch a versioned upstream asset, require its exact
hash, retain MIT notices, and publish the resulting bytes to a cache. `vendor` adds an unnecessary
installer and mutable latest lookup; `manual` loses atomic exact-set lifecycle. Orca bytes may
enter the public flake source/binary cache only with the MIT notice and pinned source/hash. No
license click is legally required by MIT, but an auditable receipt remains useful:

```json
{"user":"uid/name","license_id":"MIT-Lovecast-2026+terms-2026-04-04","source_url":"https://github.com/stablyai/orca/releases/download/v1.4.190/<asset>","content_sha256":"<sha256>","accepted_at":"RFC3339"}
```

Re-accept when `license_id` or the fetched license/Terms content hash changes; a generic forever
`--accept-eula` is invalid. Re-running the same version/hash is idempotent.

## Verdict — **GO**

`bh` may lifecycle-manage Orca without becoming an unauthorized distributor: MIT explicitly
permits distribution, and Nix supplies immutable version/hash and atomic rollback semantics.

## Recommendation

Supersede only `bh-eqvhe`'s hand-downloaded AppImage assumption with a pinned Nix package;
retain its measured serve/systemd/security requirements as a separate provisioning concern.
