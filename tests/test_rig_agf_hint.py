"""`rig._ensure_agf_hint` — non-destructive managed AGF stanza in AGENTS.md / CLAUDE.md.

file absent → create; markers present → idempotent skip (force refreshes); markers absent but
file exists → append, preserving the surrounding user content.
"""

from __future__ import annotations

from ws import rig

MARK = "<!-- ws:agf:start"


def test_creates_when_absent(tmp_path):
    p = tmp_path / "AGENTS.md"
    rig._ensure_agf_hint(p, force=False, flag="--agents")
    assert MARK in p.read_text()


def test_idempotent_when_present(tmp_path):
    p = tmp_path / "AGENTS.md"
    rig._ensure_agf_hint(p, force=False, flag="--agents")
    before = p.read_text()
    rig._ensure_agf_hint(p, force=False, flag="--agents")  # second run is a no-op
    assert p.read_text() == before


def test_appends_preserving_existing_content(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("# My project\n\nHand-written notes.\n")
    rig._ensure_agf_hint(p, force=False, flag="--claude")
    text = p.read_text()
    assert "Hand-written notes." in text  # user content preserved
    assert MARK in text  # stanza added


def test_force_refreshes_block_in_place(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("intro\n\n<!-- ws:agf:start old -->\nstale\n<!-- ws:agf:end -->\n\noutro\n")
    rig._ensure_agf_hint(p, force=True, flag="--agents")
    text = p.read_text()
    assert "stale" not in text  # old block replaced
    assert "intro" in text and "outro" in text  # surrounding content kept
    assert "ws rig ready" in text  # fresh stanza content
