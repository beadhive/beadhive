"""Provider- and transport-neutral work capability: build-system-agnostic impact resolution.

The bead lifecycle half of this capability (``WorkLifecycleService`` and its callback ports) was a
pass-through over the shell's own ``impl_*`` functions and was deleted in the beadhive-core
cutover (bh-sy36q.6): bead-state policy now lives in ``beadhive_core`` and the shell calls its
validation, submission, review and merge implementations directly. What remains is the impact
policy the Attested Green ledger consumes, imported from its submodules
(``domain.impact``, ``contracts.impact``, ``contracts.impact_resolution``, ``application.impact``).
"""
