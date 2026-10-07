# Authority modes in 0.24.0

0.24.0 gives HQ authority two modes (`bh-taa04`) and removes the renewal chore that kept the
single-operator factory tied to the operator's laptop (`bh-rjjjo`). Full reference:
[HQ: Authority modes](../HQ.md#authority-modes).

## What ships

1. **Signed authority no longer expires by default** (`bh-y929l`). `signed` stays the default
   and is signed with a finite far-future sentinel, `expires_at` 4102444800 (2100-01-01Z).
   `--duration` is an opt-in, and the duration ceiling defaults to unlimited.
2. **`rebind` replaces `renew`** (`bh-qxabp`). `bh hq authority rebind` re-signs the current
   authority against the current config head. `renew` is a deprecated alias that prints a
   deprecation line.
3. **`trusted` mode** (`bh-dzb8m`, `bh-mk97e`, `bh-l4q0s`). Frames accept authority, heartbeat
   and fleet config without signature, expiry or head binding, and the operator publishes them
   without the key. Cordon, admission and release content still apply. Records carry an
   explicit unsigned marker (`unsigned:trusted` in SQL, a commit with no `gpgsig` and a
   `Bh-Authority-Signature: unsigned (trusted)` trailer in Git).
4. **Frame-local and fleet-default modes** (`bh-dzb8m`, `bh-taa04.3`). `hq.authority_mode`
   (`signed`, `trusted` or `inherit`, default `inherit`) and `BH_HQ_AUTHORITY_MODE`. The fleet
   default `hq.default_authority_mode` is set with `bh hq authority mode-trusted` or
   `mode-signed`, and `bh hq authority mode` shows it.
5. **Open admission** (`bh-taa04.3`). In a trusted fleet `bh hq authority join` admits a
   registering frame with no grant, enrollment or operator key.
6. **Quiet expiry surfaces** (`bh-oguxa`). Warnings, `check` and `doctor` go quiet for a
   non-expiring authority, and `status` and `doctor` report the mode.

## Taking the factory to trusted

Upgrade every frame and operator host, then run once, with the key:

```sh
bh hq authority mode-trusted --operator-key <key> --confirm
```

Back to signed: `mode-signed` (or `rebind`) with the key while the operator is still trusted,
then switch the operator settings away from trusted. Runbook:
[HQ: Switching runbook](../HQ.md#authority-switching).

## Upgrading from 0.23.x

- **An authority signed with an expiry by 0.23.x stays expiring** until one `rebind` without
  `--duration`. Grant, observe and lifecycle verbs preserve the existing expiry. Git HQs are the
  common case: 0.23.x grants were hard-coded to 1 h, so do one `rebind` after upgrading.
- **Mixed versions.** A 0.23.x frame rejects the new fleet key (the fleet schema is strict) and
  rejects unsigned records. Upgrade all frames and operator hosts to 0.24.0 **before**
  `mode-trusted`.
- **Existing Git HQ server hooks** need re-provisioning by a trusted operator to accept
  unsigned pushes. A local-only Git HQ is unaffected.
- **Open-admission limit.** `join` is an authority step only. A new SQL frame still needs its
  DB account, inbox and session tables provisioned (`hq_open_admission.request` computes the
  record).

## Deprecations

- `bh hq authority renew`: use `rebind`.
- `BH_HQ_AUTHORITY_ENFORCE=false`: use `BH_HQ_AUTHORITY_MODE=trusted`. The old switch still
  works and keeps its **old content waiver**: unlike `trusted` it also ignores cordon, admission
  state, release pins and caps. Prefer `trusted`. Removal follows in 0.24.1 (`bh-ihckx`, Plan D).

## Trust delta

| Mode | What you give up |
|---|---|
| `signed` (default) | No dead-man switch: a stolen or forgotten authority does not lapse, only an operator action ends it. Cordon and retire stay immediate. |
| `trusted` (opt-in) | HQ write access is admin. In a trusted fleet anything that can reach HQ with write access and register can join. Cordon and retire stay immediate. |

The operator key never reaches a frame in either mode.

## Not in this release

Removing `BH_HQ_AUTHORITY_ENFORCE` and the remaining legacy paths (Plan D, `bh-ihckx`) follows in
0.24.1.
