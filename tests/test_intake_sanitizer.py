"""``ScrubSanitizer``: the intake ``Sanitizer`` port over the shared ``scrub()`` (the interim
egress sanitizer until ``bh-o2mjz``), and its effect through the outbox's leak check.

The shared scrubber is reached only through the adapter, so this stays a root test of the adapter
and not a direct dependent of the ``beadhive-plugins`` package.

Pure: no clock, no process, no network.
"""

from __future__ import annotations

from beadhive.adapters.intake import ScrubSanitizer
from beadhive.modules.intake import Sanitizer
from beadhive.modules.intake.domain import LeakKind, LeakPolicy, leak_check

TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8"
PAT = "github_pat_" + "A" * 30


def test_it_implements_the_port() -> None:
    assert isinstance(ScrubSanitizer(), Sanitizer)


def test_scan_finds_exact_secrets_and_token_shapes_without_repeating_them() -> None:
    text = f"a {TOKEN} b my-db-password c {PAT}"
    hits = ScrubSanitizer(["my-db-password"]).scan(text)
    assert [(h.kind, text[h.start : h.end]) for h in hits] == [
        ("token", TOKEN),
        ("secret", "my-db-password"),
        ("token", PAT),
    ]
    assert all(TOKEN not in repr(h) and "my-db-password" not in repr(h) for h in hits)


def test_overlapping_hits_are_reported_once() -> None:
    hits = ScrubSanitizer([TOKEN]).scan(f"x{TOKEN}y")
    assert len(hits) == 1 and hits[0].kind == "secret"


def test_scrub_redacts_exact_secrets_and_token_shapes_like_the_shared_scrubber() -> None:
    text = f"token {TOKEN} and hunter2 and hunter2-long"
    assert ScrubSanitizer(["hunter2", "hunter2-long"]).scrub(text) == "token *** and *** and ***"
    assert ScrubSanitizer().scrub(text) == "token *** and hunter2 and hunter2-long"


def test_clean_text_has_no_hits() -> None:
    assert ScrubSanitizer(["", "s3cret"]).scan("Thanks, this shipped in v1.2.") == ()


def test_a_token_in_a_payload_is_a_never_overridable_leak_finding() -> None:
    policy = LeakPolicy(bead_prefixes=frozenset({"bh"}))
    (finding,) = leak_check(f"use {TOKEN}", policy, sanitizer=ScrubSanitizer())
    assert finding.kind is LeakKind.SECRET and not finding.overridable
    assert TOKEN not in repr(finding)
