"""The one error the envelope raises for a safe refusal or an audited failed apply."""

from __future__ import annotations


class BackfillError(RuntimeError):
    """A safe refusal (nothing written) or a failed apply that was rolled back and audited.

    Callers render it without a traceback.
    """
