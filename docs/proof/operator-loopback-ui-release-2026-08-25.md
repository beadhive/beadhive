# Loopback operator UI release proof — 2026-08-25

## Proven source

The UI-owned authoritative release recipe was run from a clean beadhive-ui `main` checkout
against the clean core runtime tip containing daemon core, operator REST/OpenAPI, and SSE:

| Component | Version | Proven commit |
| --- | --- | --- |
| beadhive core | `0.14.0` | `038df72459140330624bffc637d32bfcdc8005c4` |
| beadhive-ui app | `0.9.1` | `9dccd355eff8488649bbe81f62e395137c685ea0` |
| UI phase-one product leaf (`bhui-61s9.15`) | `0.9.1` | `aee7df29afd2291d01d24296ea8b724207788c4c` |
| UI clean-checkout proof repair (`bhui-5wy3`) | `0.9.1` | `0f450299dfc4a97f35b0e46fb82b9dfc27082ec5` |

The runtime wire versions exercised were flat FactorySnapshot v1, direct hive snapshot and run
activity responses, and `operator-event` schema version 1. The local proof toolchain reported
Node `v24.18.1`, pnpm `10.34.5`, and just `1.57.0`.

The later changes in this bead are documentation, the manual delegate recipe, and fast static
drift tests. They record evidence for the already-proven runtime tree and do not alter it.

## Supported workflow and result

From beadhive-ui `main`:

```text
just check-operator-release /tmp/bh-worktrees/github/beadhive/beadhive/bh-76a7z-11
```

The repaired recipe owned its complete clean-checkout preconditions: frozen dependency install,
ten package builds, the legacy and operator product bundles, both CLI bundles, and then the
cross-repository browser test. The supported run exited `0` with:

```text
Tasks:    10 successful, 10 total
✔ first release runs end to end against the real core daemon composition (10275.281217ms)
tests 1
pass 1
fail 0
duration_ms 12099.681659
```

An earlier preflight attempt correctly exposed that the original UI recipe assumed generated
artifacts from a prior build. No product process had started. UI repair `bhui-5wy3` made the
recipe own install and build, after which the clean supported workflow above passed. The failed
preflight is not treated as release evidence.

## Behaviors observed

The one UI-owned real-browser scenario covered the release boundary end to end:

- initial daemon-down refusal and explicit retry;
- two hives selected by full canonical `provider/org/repo` identity;
- factory and hive coverage rendered from authoritative responses;
- snapshot-first state followed by a visible live mutation;
- exact-run activity rendering;
- retained replay after the active SSE response was dropped;
- one clean resnapshot after source reset;
- daemon restart, new producer epoch, checked `409`, and recovery without a stale retry;
- hostile `Host` rejected with `400` and hostile `Origin` rejected with `403`;
- `/mcp`, terminal control surfaces, and legacy UI relay routes absent;
- browser daemon requests carrying neither cookies nor `Authorization`; and
- no command, terminal, supervisor, write, or MCP controls in the rendered product.

The test registered cleanup before launch, terminated each detached process group, waited for
the group leader to exit, escalated to `SIGKILL` only within a bounded fallback, and asserted no
descendant remained. Chromium and the temporary state directory were also closed by test
cleanup. The green result therefore includes process-tree cleanup rather than merely an HTTP
success.

## Release claim

This evidence supports only the literal-loopback, unauthenticated, read-only phase-one profile.
It does not support MCP over HTTP, activity `POST`, terminal access, a non-loopback listener,
remote access, multi-user operation, or privileged control. The legacy Node relay remains a
non-product oracle and rollback path. Authentication and the broader daemon contract remain
phase-two work in kickoff-pending epic `bh-xw03t`.
