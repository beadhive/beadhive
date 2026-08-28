# Spike `bh-icn6b.1` — Orca system harnesses and normal homes

**Bead:** `bh-icn6b.1` · **Seat:** `dev/host-runtime-spike` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-icn6b.3`

## Question

Can current Orca desktop and `serve` use target-user Codex/Claude binaries from a dedicated
profile and their normal homes, without Orca owning another harness runtime?

## Method

I reused `bh-eqvhe`'s v1.4.175 serve measurement, inspected (read-only) this host's harness
versions, and shallow-cloned authoritative Orca main at
`3d796b82cba90ffec6018410931bfd40e2287318` into `/tmp/orca-upstream`. I inspected launch/home
routing and tests, especially `src/main/ipc/pty/host-env/codex-home.ts`,
`pty-spawn-env-codex-home-routing.test.ts`, and `pty/codex-shell-launch-preflight.ts`. No active
Orca, Codex, Claude, configuration, or Nix profile was changed.

## Evidence

1. This host resolves `/home/bees/.local/bin/codex` v0.147.0 and
   `/home/bees/.local/bin/claude` v2.1.241. `bh-eqvhe` proves v1.4.175 `serve` ran as `bees`,
   `HOME=/home/bees`, and used the service-account-derived PATH and existing Claude credentials.
2. Publisher docs say remote sessions use the server's
   [PATH, home and credentials](https://www.onorca.dev/docs/remote-servers), and instruct users
   to install/authenticate provider CLIs on that server. Desktop and serve share the runtime.
3. Orca issue [#8612](https://github.com/stablyai/orca/issues/8612) records the old Codex defect:
   “system default” received Orca's private `CODEX_HOME`. Current main implements the supported
   correction: system-default real-home routing strips an Orca-owned override, preserves a
   user-owned override, and otherwise uses native `~/.codex`. Its launch preflight resolves
   `codex` using the session shell's `command -v`; no Orca-private executable pin is required.
4. Claude system-default has no managed account home selected and therefore does not set
   `CLAUDE_CONFIG_DIR`; managed accounts alone apply a config-dir patch. Normal default is
   `$HOME/.claude`. There is no evidence of automatic private CLI installation on absence;
   preflight/provider availability reports absence.

Measured/evidenced matrix (the executable path is the controlled profile path the channel must
prepend; current-host paths merely show the installed versions):

| mode/harness | resolved executable | HOME | CODEX_HOME | CLAUDE_CONFIG_DIR | state/auth | class |
|---|---|---|---|---|---|---|
| desktop/Codex | `$HOME/.local/state/beadhive/profiles/host/bin/codex` via PATH | passwd home | unset (system default) | n/a | `$HOME/.codex` | supported on current main; v1.4.175 private behavior is unsupported |
| desktop/Claude | profile `bin/claude` via PATH | passwd home | n/a | unset | `$HOME/.claude` | supported |
| serve/Codex | same profile PATH | passwd home | unset | n/a | `$HOME/.codex` | supported on current main |
| serve/Claude | same profile PATH | passwd home | n/a | unset | `$HOME/.claude` | supported; also measured by `bh-eqvhe` |

Profile-generation upgrade proof is POSIX executable resolution: a synthetic `bin/codex` and
`bin/claude` selected through a stable profile symlink resolve the new target after atomically
changing that symlink and starting a new shell/session. Orca's source performs `command -v` at
launch; it does not persist that result as a private pin. Existing sessions intentionally keep
their process generation. When absent, `command -v` is empty and the mode is unavailable; no
desired-state-escaping install path was found. This is a legible preflight failure, not repair.

## Verdict — **GO**

Current upstream supports system-default real-home Codex and Claude plus PATH-based executable
discovery in both host modes. The catalog must require an Orca release containing #8606's
real-home route; v1.4.175 is explicitly too old for Codex.

## Recommendation

Pin a qualifying release (v1.4.190 or later after release-level verification), set the
target-user profile first in PATH, leave both home overrides unset, select “system default”, and
gate upgrades with an inside-Orca probe of executable/version/environment. Exclude older Orca
from this channel rather than wrapping it.
