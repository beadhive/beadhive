# Pants activation report

The Pants-first decision is activated for one qualified resolution closure. Bazel remains deferred
in the backlog. Baseline native validation ran the complete fast suite for every edit. Shadow mode
proved the resolution closure with a same-tree oracle: 43.377 seconds cold, 10.381 seconds warm,
11.225 seconds after a relevant edit, and zero selected-green/native-red escapes. Activated mode
runs or cache-serves 58 tests for that source/test edge, avoids the closure for known unrelated
documentation/test observations, and routes every unqualified boundary to native/full validation.

Rollback is `BH_PANTS_ROUTING=0`; native `just test`, `just check`, and `just check-all` remain
unchanged. Cache status, capacity, isolated mutable reset, inactive cleanup, and exact-entry
recovery are documented in `docs/PANTS.md`. Remote reads and writes remain off. Pants or uv/lock
upgrades invalidate the digest-bound shadow evidence and therefore fail closed before activation.

Exact-tree release attestation now includes `just pants-attest`: pinned version/config/lock/shadow
checks, `pants package src/beadhive:bh`, and the qualified Pants tests. It supplements rather than
replaces the native `just check-all` authority.
