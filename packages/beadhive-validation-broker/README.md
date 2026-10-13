# beadhive-validation-broker

The validation broker **port**: the six-operation protocol, the typed job, reservation, lease and
grant model, the error types, an in-memory fake with an injected clock, and reusable conformance
scenarios. A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md): no
`plugin.json`, stdlib only, and it never imports the root `beadhive` distribution. The design is
[validation-broker-adr.md](../../docs/design/validation-broker-adr.md).

## Public surface

| Module | Holds |
|---|---|
| `beadhive_validation_broker` | `BrokerPort` (`submit`, `wait_grant`, `report`, `cancel`, `status`, `events`); `JobSpec`, `Reservation` (cpu min/max, memory bytes, named pools), `Capacity`, `Owner`, `PriorityClass`; `Grant`, `Lease`, `LEASE_ENV` (`BH_VALIDATION_LEASE`); `Outcome`, `Usage`, `BrokerStatus`, `BrokerEvent`, `EventKind`, `BrakeLevel`; `BrokerError`, `BrokerUnavailable`, `GrantRefused`, `GrantTimeout`, `LeaseLost`; `InMemoryBroker` |
| `beadhive_validation_broker` (engines) | `LocalEngine` + `PackerPolicy` (the local in-process engine); `FailoverBroker` (primary engine, loud fallback); `Availability` port with `MeminfoAvailability` (stdlib `/proc/meminfo`) and `FakeAvailability`; `pid_alive` liveness probe |
| `beadhive_validation_broker.contract` | `SCENARIOS` (safety, nested, killed-client), `BrokerHarness`, `fake_harness`, `local_harness`, `parametrize_scenarios`, `run_scenario`, `job`, `granted`, `ManualClock` |

## Rules every engine keeps

* **Backend down:** every operation raises `BrokerUnavailable`; the client falls back to the
  host file-lock slots, loudly. `report` is accounting only and never changes a verdict.
* **Nested request:** a job with `parent_lease` (the `BH_VALIDATION_LEASE` of its parent) draws
  only from the parent's grant and never re-enters the host queue. One that can never fit is
  refused with `GrantRefused` rather than held. Releasing a parent revokes its nested leases.
* **Killed client:** the engine reclaims the dead owner's lease and every nested lease under it
  and emits `ORPHAN_RECLAIMED`.

## Engines

* `InMemoryBroker` — the test double: `(priority, arrival)` order, no packer.
* `LocalEngine` (`bh-qrixp`) — production-grade and in-process. Whole-minimum-reservation
  admission against `min(budget, measured available - floor)` less what is granted, in cpu,
  memory and named pools; strict priority classes, then admitted gates first and laggard first
  within a gate; generalised EASY backfill; flexible grants (`laggard_flex`, `headroom_cpu`);
  nested leases served only from the parent; dead owners reclaimed on every admission pass.
  Clock, availability and liveness are ports, so its tests own time and the host.
* The `slots` engine that reproduces today's host admission is root-side
  (`beadhive.validation_broker_slots`), as is the selection between `slots` and `local`
  (`beadhive.validation_broker_engine`, `work.validation_broker.engine`).

Every engine passes `contract.SCENARIOS` by supplying a `BrokerHarness`. A harness whose engine
admits on another thread provides `settle()`; one that admits by count, not by a sized
reservation, sets `sized = False` and the sizing-only scenario skips.
