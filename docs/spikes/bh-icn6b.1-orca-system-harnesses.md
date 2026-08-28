# Spike `bh-icn6b.1` — Orca system harnesses and normal homes

**Bead:** `bh-icn6b.1` · **Seat:** `dev/host-runtime-spike` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-icn6b.3`

## Question

Can current Orca desktop and `serve` use target-user Codex/Claude binaries from a dedicated
profile and their normal homes, without Orca owning another harness runtime?

## Method

I reused `bh-eqvhe`'s v1.4.175 serve findings, inspected authoritative Orca main at
`3d796b82cba90ffec6018410931bfd40e2287318`, then ran the publisher's v1.4.190 AppImage from
`/tmp/orca-190-spike` with isolated `HOME`, XDG directories, repository, and port 16768. Fake
Codex/Claude executables in two profile generations recorded argv and effective environment.
Only the throwaway login shell exported `$HOME/.nix-profile/bin`; active Orca, harness homes,
Nix profiles, and configuration were untouched.

## Evidence

1. The v1.4.190 `serve` runtime reported ready with `appVersion: 1.4.190`. Without an isolated
   `.bash_profile` PATH entry, Orca's login Bash reset the process PATH and both terminals said
   `command not found`. With `$HOME/.nix-profile/bin` exported by that login shell, Orca launched
   both fakes. This is supported host-shell discovery, not an Orca wrapper.
2. Orca invoked Codex as `app-server` during preflight and as
   `--dangerously-bypass-approvals-and-sandbox` in a terminal; Claude received
   `--dangerously-skip-permissions`. Both saw `HOME=/tmp/orca-190-spike/home` and the fake Nix
   profile first on PATH.
3. Contrary to the earlier source-only inference, released v1.4.190 still set
   `CODEX_HOME` and `ORCA_CODEX_HOME` to
   `$HOME/.config/orca/codex-runtime-home/home` for the observed Codex terminal. It therefore did
   **not** use normal `$HOME/.codex`. Claude saw `CLAUDE_CONFIG_DIR` unset and therefore used its
   normal `$HOME/.claude`. Issue [#8612](https://github.com/stablyai/orca/issues/8612) and current
   source show the intended real-home capability, but the released runtime result is controlling.
4. Atomically switching the isolated `$HOME/.nix-profile` symlink from generation 1 to generation
   2 while `serve` remained running caused newly created Orca terminals to log `gen2` for both
   harnesses; no Orca reinstall or restart occurred.
5. Moving the isolated profile symlink away made new terminals print `bash: codex: command not
   found` and `bash: claude: command not found`. Orca did not install a private executable. The
   failure is visible, though it is shell-level rather than a dedicated unavailable-status UI.

| mode/harness | resolved executable | HOME | CODEX_HOME | CLAUDE_CONFIG_DIR | state/auth | class |
|---|---|---|---|---|---|---|
| desktop/Codex | **UNMEASURED** | **UNMEASURED** | **UNMEASURED** | n/a | **UNMEASURED** | impossible to approve from this run |
| desktop/Claude | **UNMEASURED** | **UNMEASURED** | n/a | **UNMEASURED** | **UNMEASURED** | impossible to approve from this run |
| serve/Codex | `$HOME/.nix-profile/bin/codex` | isolated passwd-style home | Orca-private runtime home | n/a | Orca-private, not `$HOME/.codex` | **unsupported for catalog contract** |
| serve/Claude | `$HOME/.nix-profile/bin/claude` | isolated passwd-style home | n/a | unset | `$HOME/.claude` | supported |

Desktop was not launched and is not inferred from serve. The experiment proves executable
generation switching for serve, not desktop.

## Verdict — **NO-GO**

Orca v1.4.190 can consume profile binaries, but measured `serve` still owns a parallel Codex home,
and desktop remains unmeasured. Therefore Orca cannot yet satisfy the optional-plugin ownership
contract for every cataloged mode and harness.

## Recommendation

Exclude Orca from the implementation molecule. Minimum upstream capability: a released,
documented system-default option that leaves user `CODEX_HOME` unchanged (or unset for
`$HOME/.codex`) in desktop and serve, plus a machine-readable unavailable result. Re-spike that
release with the same inside-Orca matrix before admission; source tests alone are insufficient.
