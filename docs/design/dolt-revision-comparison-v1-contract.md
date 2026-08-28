# Dolt revision comparison v1

`bh hive sync peers HIVE... --dry-run --json` is the read-only machine surface for comparing
each selected hive's local Dolt revision with its configured federation peer. `--json` is
accepted only with `--dry-run`; the command never calls `sync_state`, `bd federation sync`,
push, or pull.

The observation policy is part of every response as `networkPolicy`. Version 1 performs one
`bd federation status --json` call per selected hive, uses the engine's 60-second subprocess
timeout, and admits at most four calls concurrently. A timeout or unreachable hive becomes an
`unavailable` comparison; it does not discard successful comparisons for the other hives.
Clients that refresh frequently should cache or throttle this explicit bounded-fetch surface.

Each record returns `relativeTo`, nullable `ahead` and `behind`, `comparisonState`,
`observedAt`, nullable `remoteObservedAt`, `sourceRevision`, and `coverage`. Counts of `0` are
measured values. `null` means the axis was not comparable and must never be rendered as zero.

The v1 states are:

| State | Meaning |
|---|---|
| `equal` | both measured counts are zero |
| `ahead` | only the local side has revisions |
| `behind` | only the remote side has revisions |
| `diverged` | both sides have revisions, or conflicts are reported |
| `unconfigured` | no matching federation peer is configured |
| `stale` | dated remote knowledge is outside the five-minute freshness window |
| `incomparable` | the peer answered but did not provide usable counts |
| `unavailable` | the status call or remote observation failed |

`observedAt` is when bh assembled the local record. `remoteObservedAt` is present only when bd
reports dated remote knowledge (`Status.LastSync`); bd's zero-time is represented as `null`.
`sourceRevision` is a deterministic digest of the comparison facts, not a Dolt commit hash.
Top-level and per-record coverage make partial and missing observations explicit.
