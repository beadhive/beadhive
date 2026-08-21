# Bifrost-compatible local complexity scorer

Beadhive's provider-neutral complexity contract is its own stable API. The bundled local scorer
is a modified, dependency-free Python port of the open-source Bifrost complexity analyzer. This
best-effort bridge is deliberately temporary; this note pins the research basis so a later
Bifrost change cannot silently change Beadhive routing or turn the port into an accidental
permanent fork.

## Pinned source and licence

- Repository: [maximhq/bifrost](https://github.com/maximhq/bifrost)
- Commit: [`c1b84fdc5a85176975c2943e8a5f965705dbeb16`](https://github.com/maximhq/bifrost/tree/c1b84fdc5a85176975c2943e8a5f965705dbeb16)
- Algorithm and tier mapping:
  [`plugins/routing/complexity/analyzer.go`](https://github.com/maximhq/bifrost/blob/c1b84fdc5a85176975c2943e8a5f965705dbeb16/plugins/routing/complexity/analyzer.go)
- Weights and keyword corpus:
  [`plugins/routing/complexity/keywords.go`](https://github.com/maximhq/bifrost/blob/c1b84fdc5a85176975c2943e8a5f965705dbeb16/plugins/routing/complexity/keywords.go)
- Types, defaults, and thresholds:
  [`plugins/routing/complexity/config.go`](https://github.com/maximhq/bifrost/blob/c1b84fdc5a85176975c2943e8a5f965705dbeb16/plugins/routing/complexity/config.go)
- Matching mechanics:
  [`plugins/routing/complexity/matcher.go`](https://github.com/maximhq/bifrost/blob/c1b84fdc5a85176975c2943e8a5f965705dbeb16/plugins/routing/complexity/matcher.go)
- Word-count curve:
  [`plugins/routing/complexity/utils.go`](https://github.com/maximhq/bifrost/blob/c1b84fdc5a85176975c2943e8a5f965705dbeb16/plugins/routing/complexity/utils.go)
- Upstream regression anchors:
  [`plugins/routing/complexity/analyzer_test.go`](https://github.com/maximhq/bifrost/blob/c1b84fdc5a85176975c2943e8a5f965705dbeb16/plugins/routing/complexity/analyzer_test.go)

Bifrost is Apache-2.0, Copyright 2025 H3 Labs Inc. Beadhive's modified file identifies its origin
and changes. Distributions must continue to include the Apache-2.0 licence, retain applicable
copyright and attribution notices, and mark modified source. The repository's root `LICENSE`
supplies the licence text. No Bifrost trademark rights are granted or implied.

## Compatibility target

The v1 Python backend preserves the parts used for classifying stable bead text:

- dimensions and weights: code 30%, strong reasoning 25%, technical 25%, simple -5%, and word
  count 10%;
- match-count saturation at 3 code, 2 reasoning, 3 technical, and 2 simple signals;
- the three-segment word-count curve (below 15, 15 through 400, and above 400 words);
- default thresholds at 0.15, 0.35, and 0.60, with an exact threshold entering the higher tier;
- the strong-reasoning override: two reasoning signals, or one reasoning signal plus a code or
  technical dimension above 0.5, forces REASONING;
- UNKNOWN when no configured lexical signal matches.

The classifier version `1.0.0+bifrost.c1b84fdc5a85` changes only when those semantics or the
pinned research basis change. The source id `beadhive/bifrost-compatible-local` distinguishes this
backend from a future upstream or remote classifier.

## Intentional deviations

Beadhive classifies one deterministic render of issue type, title, description, design, and
acceptance criteria. It does not have Bifrost's chat concepts, so system-prompt assistance,
conversation-history blending, continuation phrases, and raw request-shape extraction do not
apply. Mutable status, comments, dependencies, timestamps, assignees, and labels are excluded.

Matching is Unicode-aware, case-insensitive exact phrase or word-boundary matching. The Python
port does not reproduce Bifrost's Porter stemming or its large-input lookup optimization. This is
an acknowledged lexical-parity deviation, not a tier-contract change. Regression fixtures pin
the representative Beadhive behavior instead of promising byte-for-byte parity with Go.

Bifrost leaves an unclassified request on its existing route. Beadhive also preserves UNKNOWN in
the raw result, but a routable bead must have a tier. Required classification therefore maps
UNKNOWN to a configurable tier (MEDIUM by default) and records explicit fallback provenance.

## Replacement options

The `ComplexityClassifier` protocol and backend-neutral `ComplexityResult` are the replacement
boundary. A later implementation can use any of these without changing scheduling consumers:

1. import Bifrost's upstream Go package in a Go-owned execution path;
2. ship a compiled or intentionally forked helper behind the same Python protocol;
3. call a future Bifrost classification API if one is added (the researched gateway has routing
   integration but no standalone classify endpoint).

Replacement requires new source/version values, parity tests for this corpus, and an explicit
decision about UNKNOWN and required fallback behavior.
