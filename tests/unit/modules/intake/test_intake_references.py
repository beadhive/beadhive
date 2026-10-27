"""Pure classification of the operator's input string."""

from __future__ import annotations

import pytest

from beadhive.modules.intake.domain import ReferenceShape, classify_reference


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("https://tracker.example/acme/widgets/issues/7", ReferenceShape.URL),
        ("  http://x/y ", ReferenceShape.URL),
        ("bh-ge9bw", ReferenceShape.BEAD_ID),
        ("bh-l3zvc.5", ReferenceShape.BEAD_ID),
        ("pub-12ab", ReferenceShape.PUBLIC_STORE_ID),
        ("#12", ReferenceShape.OTHER),
        ("just words", ReferenceShape.OTHER),
        ("", ReferenceShape.OTHER),
    ],
)
def test_classify_reference(text: str, shape: ReferenceShape) -> None:
    assert classify_reference(text, public_prefixes=("pub",)) is shape


def test_a_public_prefix_only_matters_when_one_is_declared() -> None:
    assert classify_reference("pub-12ab") is ReferenceShape.BEAD_ID
