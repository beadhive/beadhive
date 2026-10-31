"""`ws plan adopt` — seed a plan FRAME from promoted intake report(s).

The planning-plane ADOPT path (epic, bead). It is the
planner-side consumer of the triage promote disposition: a report handed
to the planner carries ``intake:promoted`` (``state.is_promoted``), and ``adopt`` fleshes it into
the opening FRAME of a molecule spec — the epic seed a planner then decomposes into issues and
files via ``ws plan file``. It is source-agnostic: any channel (report / github / import) that
was promoted is adoptable.

Two provenance facets carry through to the filed epic (see ``ws/state.py``):

  * **System-of-record** — the NATIVE ``source_system`` + ``external_ref`` pair (e.g. github /
    ``gh-9``) survives onto the epic so a GitHub-sourced request stays traceable. ``source_system``
    is settable only at bead birth, so an adopted epic that carries it is born via ``bd import``
    (``plan_filing.import_epic``); ``bd create``/``update`` expose no flag for it.
  * **Originating link** — on ``ws plan file`` each origin report is linked as CHILD-OF the epic
    (report depends-on epic, ``parent-child``). The epic OWNS the report, never the reverse — so
    the report is NEVER a blocker of the epic (it can't wrongly gate the molecule on an open
    report) and it rides the epic to completion. A ``blocks`` edge is not usable here: bd forbids
    blocking dependencies between an epic and a task, so ``parent-child`` is the sanctioned link.

A BOUND report (one imported from an external issue, carrying its ``intake.source`` document; see
``docs/design/external-intake-module-adr.md``, D-c) is different: it is held for life and is never
work, and a second bead carrying its URL would make every later import of that issue refuse. So it
is NOT folded into ``adopts`` and carries no provenance onto the epic: the frame lists it under
``tracks``, ``ws plan file`` links it ``tracks`` (report to epic, non-blocking) instead of
child-of, no ``external_ref`` is copied, and only its id and title seed the epic text (the issue
body is untrusted public text and is read on the report, not copied into dispatchable work).

This module is Typer-free and bd-free — pure spec shaping over already-read bead JSON. ``plan.py``
owns the CLI verb plus every bd read/write (the ``plan.run`` test seam), so the two planes share
one subprocess seam and the shaping logic stays trivially unit-testable.
"""

from __future__ import annotations

from . import state
from .adapters.intake import SOURCE_KEY

# Native system-of-record provenance fields carried from an origin report onto the filed epic.
PROVENANCE_FIELDS = ("source_system", "external_ref")


class AdoptError(Exception):
    """Frame seeding failed (e.g. no beads to adopt). Typer-free; the CLI maps it to exit 1."""


# ---- frame seeding (pure; over bead JSON already read by the caller) ---------


def _seed_title(beads: list[dict]) -> str:
    """The seed epic title: ``Adopt: <first report title>`` (+ a count when several are folded)."""
    first = str(beads[0].get("title") or beads[0].get("id") or "untitled").strip()
    extra = len(beads) - 1
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return f"Adopt: {first}{suffix}"


def is_bound_report(bead: dict) -> bool:
    """True for a report imported from an external issue: the bead row carries the module's
    ``intake.source`` document. Such a report is tracked by the epic, never adopted (D-c)."""
    metadata = bead.get("metadata")
    return isinstance(metadata, dict) and SOURCE_KEY in metadata


def _seed_description(beads: list[dict]) -> str:
    """Seed the epic description from the report text(s) so the planner opens with the full ask.

    A bound report contributes its id and title only: its body is untrusted public text."""
    lines = ["Adopted from promoted intake report(s):", ""]
    for bead in beads:
        bid = str(bead.get("id") or "?")
        title = str(bead.get("title") or "").strip()
        lines.append(f"- {bid}: {title}".rstrip())
        body = str(bead.get("description") or "").strip()
        if body and not is_bound_report(bead):
            lines.append(f"  {body}")
    return "\n".join(lines)


def _provenance(beads: list[dict]) -> tuple[str, str]:
    """The system-of-record provenance to carry onto the epic: the ``(source_system, external_ref)``
    of the FIRST report that carries either (a GitHub-sourced report keeps its ``gh-<n>`` trace).
    ``('', '')`` when no report carries native provenance (a born-native cross-hive report)."""
    for bead in beads:
        if is_bound_report(bead):  # D-c: a bound report's external_ref stays on the report
            continue
        source_system = str(bead.get("source_system") or "").strip()
        external_ref = str(bead.get("external_ref") or "").strip()
        if source_system or external_ref:
            return source_system, external_ref
    return "", ""


def frame_from_beads(beads: list[dict]) -> dict:
    """Build the seed molecule FRAME (epic seed + empty ``issues``) from promoted report bead(s).

    The epic records the originating report id(s) under ``adopts`` and any native provenance under
    ``source_system`` / ``external_ref``; ``ws plan file`` reads these to link + carry them. Issues
    are left empty for the planner to decompose. Typer-free; raises ``AdoptError`` on empty input.
    """
    if not beads:
        raise AdoptError("no intake beads to adopt")
    epic: dict = {
        "title": _seed_title(beads),
        "description": _seed_description(beads),
        "adopts": [str(b.get("id")) for b in beads if b.get("id") and not is_bound_report(b)],
    }
    tracked = [str(b.get("id")) for b in beads if b.get("id") and is_bound_report(b)]
    if tracked:
        epic["tracks"] = tracked
    source_system, external_ref = _provenance(beads)
    if source_system:
        epic["source_system"] = source_system
    if external_ref:
        epic["external_ref"] = external_ref
    return {"epic": epic, "issues": []}


# ---- file-time helpers (read by plan.file_molecule; pure) --------------------


def adopts_of(epic: dict) -> list[str]:
    """The originating report id(s) an adopted epic links back to (``[]`` when not adopted)."""
    return [str(x) for x in (epic.get("adopts") or []) if str(x).strip()]


def tracks_of(epic: dict) -> list[str]:
    """The BOUND report id(s) an epic tracks (``[]`` when none): linked ``tracks``, report to epic,
    and left open and held; never adopted as children (D-c)."""
    return [str(x) for x in (epic.get("tracks") or []) if str(x).strip()]


def provenance_of(epic: dict) -> tuple[str, str]:
    """The ``(source_system, external_ref)`` provenance declared on an epic (``''`` when unset)."""
    return (
        str(epic.get("source_system") or "").strip(),
        str(epic.get("external_ref") or "").strip(),
    )


# Mirrors ``beadhive_core.planning.DEFAULT_EPIC_PRIORITY`` (src/beadhive never imports the core
# package eagerly); tests pin both to P2 via the filed `bd create` and the import record.
DEFAULT_EPIC_PRIORITY = 2


def epic_import_record(epic: dict, labels: list[str]) -> dict:
    """A ``bd import`` JSONL record that BIRTHS the epic carrying native provenance.

    ``source_system`` can only be set when a bead is created, so a provenance-carrying epic is
    imported rather than ``bd create``-d. Carries title/type + priority + description/design + the
    identity triplet labels + the native ``source_system`` / ``external_ref`` pair. The priority
    is the epic's declared one (``0`` honored) or :data:`DEFAULT_EPIC_PRIORITY` when omitted —
    ``bd import`` would otherwise store the unset zero value, i.e. P0.
    """
    priority = epic.get("priority")
    record: dict = {
        "title": str(epic.get("title") or ""),
        "issue_type": "epic",
        "priority": int(priority) if priority not in (None, "") else DEFAULT_EPIC_PRIORITY,
    }
    for key in ("description", "design"):
        val = str(epic.get(key) or "").strip()
        if val:
            record[key] = val
    source_system, external_ref = provenance_of(epic)
    if source_system:
        record["source_system"] = source_system
    if external_ref:
        record["external_ref"] = external_ref
    if labels:
        record["labels"] = list(labels)
    return record


# ---- read-side: distinguish an origin report from a work sibling -------------


def is_origin_report(labels) -> bool:
    """True for an ADOPTED origin report linked under an epic — a promoted intake bead
    (``intake:promoted``) or one carrying an ``origin:`` channel label. Used to keep origin reports
    OUT of the molecule's work-sibling set (they carry no acceptance and demand no kickoff gate)
    while still surfacing them in ``ws plan show``."""
    return state.is_promoted(labels) or state.origin_of(labels) is not None
