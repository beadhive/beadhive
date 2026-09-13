# Pants shadow qualification

The initial resolution closure is eligible for activation planning, but remains additive and is
not a production lifecycle gate. Nine reversible mutation classes were compared with a same-tree
native oracle. The source and test cases passed under both runners; seven unsafe classes selected
the native fallback; a known-unrelated test avoided the Pants closure while its bounded native
oracle stayed green. There were zero selected-green/native-red escapes.

The measured fresh Pants leaf took 43.377 seconds, a repeat of the identical digest was served from
the shared local store in 10.381 seconds, and a relevant edit invalidated and passed in 11.225
seconds. The receipt identifies Pants 2.32.1, experimental uv, the disposable worktree, and the
shared-immutable/isolated-mutable cache topology. These are development measurements, not claims
about full-suite savings.

`uv run python scripts/pants_shadow_evidence.py` verifies the input digests, complete mutation
matrix, result/count fields, useful warm result, unrelated-test avoidance, and zero correctness
escapes. Any missing, stale, incompatible, or selected-green/native-red evidence makes the command
red and requires native/full routing. The operational volume is one activation-eligible Pants
route, seven observed conservative fallbacks, and zero active production routes.

`just attest` intentionally retains its native `just check-all` authority. The final activation
bead can add Pants package and qualified-test evidence before that native attest without replacing
or weakening it.
