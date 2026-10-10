# `bh hq placement` visibility: approved wire compatibility exemption

Bead: **bh-vl0r3 — hq.placement_mode self; `bh hq placement` promoted to a documented verb**.

## Decision — 2026-10-10

Operator decision 2026-10-10, bh-vl0r3: hq.placement hidden→visible is a documentation change,
not a wire break; the operation's parameters and results are unchanged.

Stay on wire release **2.9.0** (unpublished on the branch; renumbered from 2.7.0 when main
published 2.7.0 and 2.8.0). No major release is cut.

## Scope of the exemption

The wire gate normally rejects any change to a published operation within a major, and wire
releases 2.5.0 through 2.8.0 published `hq.placement` with `surfaces.cli.declared_hidden` and
`effective_hidden` both `true`. `scripts/check_wire_schema_compat.py`
(`_APPROVED_UNHIDDEN_OPERATIONS`) exempts only:

- the operation `hq.placement`;
- both CLI flags moving together from `true` to `false`.

The reverse direction, a single flag, any other operation, and any other edit to
`hq.placement` (parameters, results, privilege, projections) keep their rejection. This decision
does not establish a general rule that un-hiding a verb is compatible.

The release-evidence report (`beadhive.contract_release_evidence`) still classifies the 2.5.0 through
2.8.0 comparisons as `policy-divergence-retained` with exactly the two flag changes;
`tests/test_contract_release_evidence.py` pins that set.
