# hq-service shadow-parity runs

Records of `tests/harness/hq_parity.py` runs of the `*_int` suites against the Go `hq-parity`
binary (ADR 0005 migration step 3). Newest first. Never edit the corpus to hide a diff.

## 2026-11-20 (bh-jwymz): admission, operator and release-eligibility corpora vs hq-service v0.1.0

No `hq-parity` run: these three surfaces are not shadow-parity kinds. `hq-parity` (hq-service
`cmd/hq-parity`) decides `hive_lease`, `heartbeat`, `registration` and `authority` against a live
Dolt server, and `tests/harness/hq_parity.py` shadows the same four. Recorded honestly as
unsupported, not as a pass:

| corpus (hq-service `conformance/`) | cases | Python runner (`BH_HQ_SERVICE_CONFORMANCE` set) | hq-parity |
|---|---|---|---|
| `release-eligibility/v1` | 15 | 15 pass (Python is the reference: `release_policy.admits`) | unsupported |
| `admission/v1` | 242 | 145 pass, 97 skipped by shape | unsupported |
| `operator/v1` | 178 | 105 pass, 73 skipped by shape | unsupported |

The admission and operator corpora are normative for hq-service's write routes and have no Python
reference (Python has no write endpoints), so their Python runners replay them from the client
side: a scripted service answers each case's own expectation and the test asserts the exact
method, path and body the adapter sends and what it returns or raises. A skip is by shape, never
by name: a request a correct client cannot produce (malformed body, bad path or query, a derived
`$` value), `GET /eligibility` cases (answered under a verified authority read the case does not
carry), and for operator the apply cases that alter or refuse a signature (service rules, and the
unrouted catalog revoke). The server rules (each case's `effects`) are asserted by hq-service's own
Go runner, not here. Without the env var the pinned fixtures in
`tests/fixtures/hq_service_contract/` are replayed instead.

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
