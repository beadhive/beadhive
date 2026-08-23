"""The normalized, agent-facing alert surface.

Alert sources are deliberately small functions returning :class:`Alert` records.  The
first source adapts the warnings that ``bh doctor`` already calculates; later sources
can register a rule here without teaching every harness integration about it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from . import doctor


@dataclass(frozen=True)
class Alert:
    """One active condition an agent or operator should be steered toward."""

    severity: str
    code: str
    message: str
    remediation: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


AlertSource = Callable[[], list[Alert]]
_SOURCES: list[AlertSource] = []


def register(source: AlertSource) -> AlertSource:
    """Register an alert source and return it, so sources can use decorator syntax."""
    _SOURCES.append(source)
    return source


@register
def doctor_warnings() -> list[Alert]:
    """Adapt existing doctor warnings without creating a second warning rule set."""
    return [
        Alert(
            severity="warning",
            code="doctor.warning",
            message=message,
            remediation=(
                "Run `bh doctor` for the full diagnostic context, then address the condition "
                "named in this alert."
            ),
        )
        for message in doctor.warning_messages()
    ]


def active() -> list[dict[str, str]]:
    """Return all currently active alerts in the stable resource/CLI shape."""
    return [alert.as_dict() for source in _SOURCES for alert in source()]
