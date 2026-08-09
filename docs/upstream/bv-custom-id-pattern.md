# Upstream: bv custom ID pattern flag

**Issue:** [#188](https://github.com/Dicklesworthstone/beads_viewer/issues/188)

## Summary

Request to expose a CLI flag in Beads Viewer (`bv`) for custom bead ID pattern
matching.

## Problem

bv's `ExplicitMatcher` currently only supports eight default patterns for
matching bead IDs in commit messages. All default patterns require numeric
suffixes (e.g., `PROJECT-123` style), which means beadhive-family trackers
with base36-style alphanumeric IDs cannot be matched.

### Affected Formats

Beadhive IDs such as `bh-8g6cj` and `bhui-pbb` fail to match any of the eight
default patterns:

1. `[A-Za-z]+-\d+` (bracket form: `[ID]`)
2. `(?i)closes?:?\s*#?([A-Za-z]+-\d+)` (closes keyword)
3. `(?i)fix(?:es|ed)?:?\s*#?([A-Za-z]+-\d+)` (fix/fixes/fixed keyword)
4. `(?i)refs?:?\s*#?([A-Za-z]+-\d+)` (ref/refs keyword)
5. `(?i)resolves?:?\s*#?([A-Za-z]+-\d+)` (resolves keyword)
6. `(?i)beads?[-_](\d+)` (beads/bead prefix with numeric only)
7. `(?i)bv[-_](\d+)` (bv prefix with numeric only)
8. `\b([A-Z]{2,10}-\d+)\b` (PROJECT-123 style, numeric suffix)

## Requested Solution

Expose the existing internal Go API as a CLI flag (e.g., `--id-pattern`) to
allow specifying custom regex patterns for matching custom ID formats.

### Existing API

The `pkg/correlation/explicit.go` module already exports:

- `NewExplicitMatcherWithPatterns(repoPath string, patterns []*regexp.Regexp)`
  (line 62–68)
- `AddPattern(pattern *regexp.Regexp)` (line 70–73)

### Example Usage

```bash
bv --robot-triage --id-pattern 'bh-[a-z0-9]{5}' --id-pattern 'bhui-[a-z]{3}'
```

This would be purely additive and would not affect bv's existing four
correlation methods.

## Context

This is required for beadhive to correlate commits to beadhive-family ID
formats without modifying bv's internal assumptions or beadhive's ID format.

The [correlation-yield spike](/docs/spikes/bh-rwryq.3-correlation-yield.md) in
the beadhive repo documents why this matters for cross-repo correlation
workflows.
