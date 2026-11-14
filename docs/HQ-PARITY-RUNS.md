# hq-service shadow-parity runs

Records of `tests/harness/hq_parity.py` runs of the `*_int` suites against the Go `hq-parity`
binary (ADR 0005 migration step 3). Newest first. Never edit the corpus to hide a diff.

## 2026-11-12 (bh-b2pb3): beadhive 071a89ae vs hq-service f69779c

Dolt 2.3.5, Go 1.27.1, host in TRUSTED authority mode, `-n 2`, 137 log lines.

| surface | baseline | this run |
|---|---|---|
| hive lease | 19 match + 1 commit-race | 18 match + 1 commit-race + 1 diff |
| heartbeat | 37 match | 37 match |
| registration | 9 match | 8 match + 1 diff |
| authority | 62 match + 1 floor-lock | 69 match + 1 floor-lock + 1 diff |

New diffs:

- hive lease and registration (2): Python accepts after an unrelated fleet-config publish moved
  the config head; Go refuses `authority_failed`. Porting gap: bh-u67ve (config-head tolerance,
  27da31b1), not yet in hq-service.
- authority (1): the test makes the credential unavailable; Python refuses, the Go shadow call
  errors in the harness (`fnox credential unavailable`) before deciding. Harness gap: the shadow
  cannot represent a credential-outage case. Corpus untouched.

Setup pitfall: the refusal-code tables load from `HQ_{LEASE,HEARTBEAT,REGISTRATION,AUTHORITY}_CORPUS`
or `GIT_WORKSPACE`. With neither set every refusal compares as a diff (17 spurious diffs in a first
run). Point the variables at `hq-service/conformance/<surface>/v1`.
