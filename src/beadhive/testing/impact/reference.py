"""A reference :class:`ImpactBackend` that answers straight from the fixture's ground truth.

It is what a correct backend's raw answer looks like for each scenario, the baseline the kit's
own tests mutate into deliberately broken backends, and a worked example of fault injection: a
real backend's harness injects the same three faults through its tool query instead.
"""

from __future__ import annotations

from ...modules.work.domain.impact import BackendImpact, ImpactRequest
from .fixture import ImpactFixture

#: Faults a harness can inject into its backend's tool (rule 3).
FAULT_ERROR = "error"
FAULT_TIMEOUT = "timeout"
FAULT_VERSION_MISMATCH = "version-mismatch"
FAULTS = frozenset({FAULT_ERROR, FAULT_TIMEOUT, FAULT_VERSION_MISMATCH})
#: Every harness must be able to inject these; a version mismatch only where the tool reports one.
REQUIRED_FAULTS = frozenset({FAULT_ERROR, FAULT_TIMEOUT})

REFERENCE_VERSION = "reference-1"


class ReferenceBackend:
    """Answers the three questions for :class:`ImpactFixture` exactly as the fixture defines them.

    ``fault`` simulates the tool failing: raising (error), exceeding its budget (timeout), or
    answering as a different version than the backend expects (version mismatch)."""

    def __init__(
        self,
        fixture: ImpactFixture,
        *,
        fault: str | None = None,
        version: str = REFERENCE_VERSION,
    ) -> None:
        if fault is not None and fault not in FAULTS:
            raise ValueError(f"unknown fault {fault!r}")
        self.name = fixture.backend
        self.version = version
        self.fixture = fixture
        self.fault = fault

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        if self.fault == FAULT_ERROR:
            raise RuntimeError("reference tool unavailable")
        if self.fault == FAULT_TIMEOUT:
            raise TimeoutError("reference tool exceeded its budget")
        fixture = self.fixture
        key_units = {key.name: fixture.selected_units(key.name) for key in request.keys}
        proven = frozenset(
            name
            for name, units in key_units.items()
            if units and all(fixture.unit(unit).proven for unit in units)
        )
        answered = self.version
        if self.fault == FAULT_VERSION_MISMATCH:
            answered = f"{self.version}+other"
        return BackendImpact(
            backend_version=answered,
            owners={change.path: fixture.owners(change) for change in request.changed},
            affected_units=fixture.affected_units(request.changed),
            key_units=key_units,
            proven_keys=proven,
            global_inputs=(fixture.global_input,),
        )


__all__ = [
    "FAULTS",
    "FAULT_ERROR",
    "FAULT_TIMEOUT",
    "FAULT_VERSION_MISMATCH",
    "REFERENCE_VERSION",
    "REQUIRED_FAULTS",
    "ReferenceBackend",
]
