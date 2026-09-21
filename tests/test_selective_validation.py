from __future__ import annotations

from beadhive import selective_validation


def test_impact_fallback_warning_is_prominent_and_sent_to_stderr(capsys) -> None:
    selective_validation.warn_impact_fallback(
        "pants: error: RuntimeError: Pants peek failed after retry"
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "!!! WARNING: IMPACT RESOLUTION FALLBACK !!!" in captured.err
    assert "Pants peek failed after retry" in captured.err
    assert "running every attestation key" in captured.err
