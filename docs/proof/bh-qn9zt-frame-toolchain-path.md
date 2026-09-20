# `bh-qn9zt`: deterministic frame toolchain PATH

## Decision

The frame launcher is the authoritative owner of the environment inherited by primary agents,
subagents, interactive command execution, and resumed or unattended work. Its implementation is
outside the Beadhive and Frame repositories available on this host, so this repository cannot
patch that launcher without inventing a false ownership boundary.

For `beadhive-factory`, the validated remediation is a user-owned policy with two entry points and
one identical value:

- `~/.profile` and `~/.bashrc` reconstruct PATH for new login, interactive, and command-wrapper
  shells. The shell setup clears an inherited stale `__ETC_PROFILE_NIX_SOURCED` guard before
  sourcing Nix's official daemon profile, then reasserts the canonical PATH.
- `~/.config/environment.d/60-beadhive-toolchain.conf` supplies the same PATH to the systemd user
  manager for bare non-login, unattended, and resumed services.

The canonical order is `~/.local/bin`, `~/.nix-profile/bin`, the system Nix profile, then explicit
system directories. Entries are unique. No root profile or launcher-provided ambient directory is
retained. `~/.local/bin/bd` and `dolt` are user-owned compatibility symlinks into the user profile,
so this ordering does not select a second installation. If the user profile is absent, its PATH
entry is harmless; `nix` remains reachable from `/nix/var/nix/profiles/default/bin`, while missing
user-profile tools fail normally and visibly.

The external launcher should eventually construct this environment directly and use the same
constructor for all launch modes. The host remediation is necessary because a running process's
environment cannot be changed retroactively; all newly launched command contexts are covered.

## Reproduction and regression probe

On 2026-09-20 the original frame was uid `8335`, HOME `/home/bees`, and inherited
`__ETC_PROFILE_NIX_SOURCED=1` while PATH omitted both Nix profile bins. Sourcing the official Nix
script returned immediately because of that stale guard. A clean environment sourcing the same
script resolved the tools, isolating the fault to launcher environment construction rather than
the profile generation.

Run the committed probe directly for a fresh shell. For the unattended/bare non-login case, run it
as a transient user service without sourcing shell initialization:

```sh
systemctl --user daemon-reload
systemd-run --user --wait --pipe --collect \
  --unit=bh-qn9zt-probe --working-directory="$PWD" \
  ./scripts/probe-frame-toolchain-path.sh
```

The real transient service completed `0/SUCCESS` on `beadhive-factory`. It reported uid `8335`,
HOME `/home/bees`, the canonical deduplicated PATH, Beads `1.3.0` at commit `f45b249ce6b4`, Dolt
`2.3.5`, and Nix `2.35.1`. The same probe passed from a fresh login command wrapper. This proves the
installed release artifacts are usable without manual shell initialization; it does not claim the
unavailable external launcher source has been changed.
