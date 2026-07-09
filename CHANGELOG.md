# Changelog

All notable changes to this project are documented in this file. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet committed to
a formal versioning cadence beyond [SemVer](https://semver.org/).

## [1.0.0] — Unreleased

First public release. Headline: the **ws → Beadhive (`bh`) rebrand** — same tool, same AGF
process, new name and package identity to remove the collision with the many other CLIs
named `ws`.

### Rebrand: ws → Beadhive / `bh`

- Python package renamed `ws` → `beadhive`; console scripts renamed `ws`/`ws-mcp` →
  `bh`/`bh-mcp`.
- Injected/managed asset marker and file layout migrated to the `beadhive`/`bh` naming
  (`bh rig migrate` added to move an existing onboarded rig over in place).
- MCP resource URI scheme renamed `ws://` → `beadhive://`.
- Docs, skills, and agent definitions across the AGF plugin swept for the rename; the
  abstract process is now framed as **Beadflow** (an implementation of **AGF** on beads),
  with **Beadhive** as the umbrella product/workspace name.
- Config/runtime state continues to live under `~/.ws/` (unchanged) — only the package,
  command names, and resource scheme changed; on-disk data and Dolt-backed issue history
  are unaffected.
- See `docs/design/limn-naming-strategy-adr.md` for the naming decision record and
  `docs/AGF.md` for the AGF/Beadflow process this tool drives.

### Notes on this release

- Version `1.0.0` was chosen deliberately for this first release (rather than a `0.x`
  pre-release): the `bh` CLI surface, config layout, and AGF/`ws work` verb set are already
  in active daily use across multiple rigs and are considered stable enough to commit to
  under SemVer compatibility promises going forward. Breaking changes to command names,
  flags, or on-disk config layout will bump the major version.
- Publish-to-PyPI, the `v1.0.0` git tag, and the GitHub release are prepared but
  intentionally **not** pushed/created as part of the change that introduces this changelog
  entry — that final step requires explicit human sign-off. See
   for tracking.
