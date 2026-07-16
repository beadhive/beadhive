# Changelog

All notable changes to this project are documented in this file. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project has not yet committed to
a formal versioning cadence beyond [SemVer](https://semver.org/).

## Unreleased

Headline so far: the **ws → Beadhive (`bh`) rebrand** — same tool, same AGF process, new name
and package identity to remove the collision with the many other CLIs named `ws`.

### Rebrand: ws → Beadhive / `bh`

- Python package renamed `ws` → `beadhive`; console scripts renamed `ws`/`ws-mcp` →
  `bh`/`bh-mcp`.
- Injected/managed asset marker and file layout migrated to the `beadhive`/`bh` naming
  (`bh hive migrate` added to move an existing onboarded hive over in place).
- MCP resource URI scheme renamed `ws://` → `beadhive://`.
- Docs, skills, and agent definitions across the AGF plugin swept for the rename; the
  abstract process is now framed as **Beadflow** (an implementation of **AGF** on beads),
  with **Beadhive** as the umbrella product/workspace name.
- Config/runtime state continues to live under `~/.ws/` (unchanged) — only the package,
  command names, and resource scheme changed; on-disk data and Dolt-backed issue history
  are unaffected.
- See `docs/design/limn-naming-strategy-adr.md` for the naming decision record and
  `docs/AGF.md` for the AGF/Beadflow process this tool drives.

### Notes

- An earlier draft of this changelog bumped the package to `1.0.0` and framed it as a first
  public release. That was premature: the project is still under local development and has
  not yet committed to a `1.0.0` SemVer stability promise, so the version has been reverted
  to `0.1.0` pending an actual release decision. The rebrand work above already happened and
  the notes are kept here as a record; only the release framing was walked back. See
  .
