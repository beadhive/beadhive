"""The **contributor** seat — persistent, hive-scoped intelligence about ONE external upstream we
contribute to (Contribution plane, epic bh-uxam). A dedicated AGF role, NOT a one-shot analyst
call: it UNDERSTANDS the external repo and carries two duties.

1. **DOSSIER** — owns a contribution profile (a four-layer read of the upstream's contribution
   norms) that yields an explicit go/no-go signal + authorship strategy BEFORE we plan work. The
   specialized anti-slop reviewer (bh-uxam.5) and the gated PR path (bh-uxam.6) consume it.

2. **OUTBOUND EDITOR** — consumes the external hive's ``outbound:pending`` report queue (staged by
   ``bh report``'s external terminal, bh-p1r4.1), dedupes/aggregates it into a curated, minimal set
   of well-formed issues, and files each upstream via the gated single-item publish path behind a
   hard, human-only publication gate. contributor : upstream issues :: specialized-reviewer :
   upstream PRs — the editorial gate that keeps our outward footprint from reading as slop.

Read-only re: OUR code and reports (mirrors the analyst contract, but persistent + hive-scoped): it
does not write code or resolve gates. Its ONE write is the gated external publish, and even that is
refused unless the actor is a ``contrib/`` seat (the generalized write-guard,
``guard.publish_refusal``) AND a human has resolved the publication gate.

Dossier storage
---------------
The dossier is structured data keyed by the hive TRIPLET, refreshed when stale. It is persisted in a
dedicated cache store (``$BH_CACHE/contrib-profiles.json``), NOT inlined onto the flow-style
``managed_repos`` config entry: a rich four-layer profile would bloat the fleet registry, and
writing ``config.yaml`` collides with the control-plane partitioning (managed_repos is the
director's fleet partition — the contributor must not mutate it). Keying by ``registry.hive_key``
mirrors the
``metadata`` cache precedent (structured per-hive data, refreshed when stale).

Provenance / typer-free core: this module owns the DECISIONS (posture detection, verdict, queue,
gated publish) and returns ``(exit, error, …)`` tuples; ``cli`` renders. All bd writes go through
sanctioned beads-native primitives with ``--actor`` provenance.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

from . import bd, config, guard, registry
from .state import OUTBOUND_PENDING, PUBLISH_APPROVED, is_outbound_candidate

# ---------------------------------------------------------------------------
# AI-PR posture — layer 4 of the dossier
# ---------------------------------------------------------------------------

# The upstream's stance on AI-authored contributions, deliberately a small closed set. A "no AI PRs"
# repo yields a NO-GO verdict — an explicit advisory to a human, NOT a silent proceed and NOT an
# auto-block.
POSTURE_WELCOME = "welcome"  # AI-assisted contributions explicitly welcomed
POSTURE_RESTRICTED = "restricted"  # allowed WITH disclosure / conditions
POSTURE_FORBIDDEN = "forbidden"  # AI-generated PRs not accepted → NO-GO
POSTURE_UNKNOWN = "unknown"  # unstated — conservative advisory GO

VERDICT_GO = "GO"
VERDICT_NO_GO = "NO-GO"

# Case-insensitive needles mined from the upstream's contribution text. Kept small + advisory —
# precision is not safety-critical (the verdict is advice to a human, never an auto-block).
_FORBIDDEN_NEEDLES = (
    "no ai-generated",
    "no ai generated",
    "not accept ai",
    "do not accept ai",
    "don't accept ai",
    "no ai contributions",
    "no ai prs",
    "no ai pull requests",
    "ai-generated ... rejected",
    "no llm-generated",
    "no llm generated",
    "prohibit ai",
    "ban ai",
    "ai is not allowed",
)
_RESTRICTED_NEEDLES = (
    "disclose",
    "ai-assisted",
    "ai assisted",
    "must be reviewed",
    "allowed if",
    "with attribution",
)
_WELCOME_NEEDLES = (
    "ai contributions welcome",
    "ai-assisted contributions welcome",
    "we welcome ai",
    "ai is welcome",
)


def detect_ai_posture(text: str) -> str:
    """The upstream's AI-PR posture inferred from its combined contribution text (CONTRIBUTING /
    PR template / CoC). FORBIDDEN wins over every other signal (fail-safe toward NO-GO); then an
    explicit WELCOME; then a RESTRICTED (disclosure) signal; else UNKNOWN. Advisory — the caller
    surfaces it to a human, never auto-acts on it."""
    low = (text or "").lower()
    if any(n in low for n in _FORBIDDEN_NEEDLES):
        return POSTURE_FORBIDDEN
    if any(n in low for n in _WELCOME_NEEDLES):
        return POSTURE_WELCOME
    if any(n in low for n in _RESTRICTED_NEEDLES):
        return POSTURE_RESTRICTED
    return POSTURE_UNKNOWN


def verdict_for(posture: str) -> str:
    """The go/no-go signal for a posture: a FORBIDDEN ("no AI PRs") upstream yields NO-GO (an
    explicit advisory), everything else GO. NO-GO is advice to a human — not an auto-block."""
    return VERDICT_NO_GO if posture == POSTURE_FORBIDDEN else VERDICT_GO


def authorship_strategy_for(posture: str) -> str:
    """The recommended authorship strategy for a posture — how a contribution should be authored so
    it clears the upstream's bar (the actionable half of the go/no-go decision)."""
    return {
        POSTURE_FORBIDDEN: (
            "NO-GO: upstream does not accept AI-generated contributions. Do NOT open AI-authored "
            "PRs/issues here — advise a human to decide before any contribution."
        ),
        POSTURE_RESTRICTED: (
            "GO with disclosure: AI-assisted work is allowed under conditions. Human reviews + "
            "signs off, disclose AI assistance per CONTRIBUTING, honor DCO sign-off."
        ),
        POSTURE_WELCOME: (
            "GO: AI-assisted contributions welcomed. Still human-reviewed with DCO sign-off where "
            "required."
        ),
        POSTURE_UNKNOWN: (
            "GO (advisory): posture unstated — default to human-authored + disclosed and confirm "
            "with a maintainer before scaling contributions."
        ),
    }.get(posture, "GO (advisory): confirm the upstream's AI-PR posture with a maintainer.")


# ---------------------------------------------------------------------------
# Layer 1 — explicit requirements (mechanically detectable from the upstream clone)
# ---------------------------------------------------------------------------

# {requirement key: candidate paths under the upstream repo root, first hit wins}. Read from the
# local fork/clone working tree — offline, no network, unit-testable against a fixture dir.
_REQUIREMENT_FILES: dict[str, tuple[str, ...]] = {
    "contributing": ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"),
    "pr_template": (
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/pull_request_template.md",
        "docs/pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
    ),
    "issue_template": (
        ".github/ISSUE_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE",
    ),
    "code_of_conduct": ("CODE_OF_CONDUCT.md", ".github/CODE_OF_CONDUCT.md"),
}

# Config/marker files that, when present, evidence a style/lint/format or CI regime.
_STYLE_MARKERS = (
    ".editorconfig",
    ".pre-commit-config.yaml",
    ".ruff.toml",
    ".eslintrc",
    ".eslintrc.json",
    ".prettierrc",
    "rustfmt.toml",
)

# Text needles that evidence a DCO / Developer-Certificate-of-Origin sign-off requirement.
_DCO_NEEDLES = ("signed-off-by", "developer certificate of origin", "dco", "sign-off")


def read_upstream_files(root: str | Path) -> dict[str, str]:
    """Read the layer-1 contribution artifacts + style/CI markers present under an upstream clone
    ``root`` into ``{relpath: content}`` (a directory marker maps to ``""``). The mechanical input
    to :func:`scan_requirements` — a pure filesystem read, injectable in tests. Missing paths are
    simply absent from the map; a read error skips the file (best-effort)."""
    base = Path(root)
    out: dict[str, str] = {}
    candidates: list[str] = []
    for paths in _REQUIREMENT_FILES.values():
        candidates.extend(paths)
    candidates.extend(_STYLE_MARKERS)
    candidates.append(".github/workflows")
    for rel in candidates:
        p = base / rel
        try:
            if p.is_dir():
                out[rel] = ""
            elif p.is_file():
                out[rel] = p.read_text(errors="replace")
        except OSError:
            continue
    return out


def _first_present(files: dict[str, str], paths: tuple[str, ...]) -> str:
    """The first candidate path present in ``files`` (an empty-string value still counts as
    present — it's a detected directory marker), or ``""`` when none is."""
    for path in paths:
        if path in files:
            return path
    return ""


def scan_requirements(files: dict[str, str]) -> dict:
    """Layer 1 of the dossier: the explicit contribution requirements detectable from ``files``
    (a ``{relpath: content}`` map, e.g. from :func:`read_upstream_files`). Reports which artifact
    satisfied each requirement (path or ``""``) plus booleans for DCO sign-off, style/lint, and
    CI — the concrete checklist the reviewer/PR path consult."""
    contributing_path = _first_present(files, _REQUIREMENT_FILES["contributing"])
    contributing_text = files.get(contributing_path, "").lower() if contributing_path else ""
    has_dco = any(n in contributing_text for n in _DCO_NEEDLES) or any(
        "dco" in k.lower() for k in files
    )
    has_style = any(m in files for m in _STYLE_MARKERS)
    has_ci = ".github/workflows" in files
    return {
        "contributing": contributing_path,
        "pr_template": _first_present(files, _REQUIREMENT_FILES["pr_template"]),
        "issue_template": _first_present(files, _REQUIREMENT_FILES["issue_template"]),
        "code_of_conduct": _first_present(files, _REQUIREMENT_FILES["code_of_conduct"]),
        "dco_sign_off": has_dco,
        "style_lint_format": has_style,
        "test_and_ci": has_ci,
    }


def _requirements_text(files: dict[str, str]) -> str:
    """The combined text of the contribution-governing artifacts (CONTRIBUTING / PR template /
    CoC) — the corpus :func:`detect_ai_posture` mines for the layer-4 AI-PR posture."""
    keys: list[str] = []
    for req in ("contributing", "pr_template", "code_of_conduct"):
        path = _first_present(files, _REQUIREMENT_FILES[req])
        if path:
            keys.append(path)
    return "\n".join(files.get(k, "") for k in keys)


# ---------------------------------------------------------------------------
# Dossier model + storage
# ---------------------------------------------------------------------------

_STORE_FILENAME = "contrib-profiles.json"
_TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%SZ"
# Default staleness backstop for a dossier: 7 days (upstream norms drift slowly). Config-free —
# the dossier is advisory, so a coarse default is fine; refresh via `contrib-profile build`.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class Dossier:
    """A four-layer contribution profile for one external upstream, keyed by the hive triplet.

    Layers: (1) ``requirements`` — explicit, mechanically-detected contribution requirements;
    (2) ``conventions`` — historical conventions mined from merged PRs (seat-enriched);
    (3) ``pushback`` — recurring maintainer push-back patterns (seat-enriched); (4) the AI-PR
    ``posture`` → explicit ``verdict`` (GO/NO-GO) + ``authorship_strategy``. Layers 2-3 are seeded
    empty by the mechanical build and enriched by the contributor seat's PR-history reading."""

    hive: str  # registry.hive_key triplet
    upstream: str  # the upstream owner/repo this dossier profiles
    built_at: str
    requirements: dict = field(default_factory=dict)
    conventions: list[str] = field(default_factory=list)
    pushback: list[str] = field(default_factory=list)
    posture: str = POSTURE_UNKNOWN
    verdict: str = VERDICT_GO
    authorship_strategy: str = ""
    notes: str = ""


_DOSSIER_FIELDS = frozenset(f.name for f in fields(Dossier))


def _now() -> str:
    return datetime.now(UTC).strftime(_TIMESTAMP_FMT)


def _store_path() -> Path:
    return config.cache_dir() / _STORE_FILENAME


def _load_store() -> dict:
    """The whole ``{hive_key: dossier-dict}`` store, or ``{}`` when absent/unparseable."""
    path = _store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def store_dossier(dossier: Dossier) -> None:
    """Persist ``dossier`` under its hive key, atomically (temp + replace) so a reader never sees a
    half-file. Merges into the existing store (other hives' dossiers are preserved)."""
    store = _load_store()
    store[dossier.hive] = asdict(dossier)
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(store, indent=2))
    tmp.replace(path)


def load_dossier(hive_key: str) -> Dossier | None:
    """The stored dossier for ``hive_key`` (the triplet), or ``None`` when none is stored / the
    record is malformed. Unknown extra keys are ignored so a forward-compatible store loads."""
    rec = _load_store().get(hive_key)
    if not isinstance(rec, dict):
        return None
    try:
        return Dossier(**{k: rec[k] for k in _DOSSIER_FIELDS if k in rec})
    except TypeError:
        return None


def _age_seconds(built_at: str | None) -> float:
    if not built_at:
        return float("inf")
    try:
        dt = datetime.strptime(built_at, _TIMESTAMP_FMT).replace(tzinfo=UTC)
    except ValueError:
        return float("inf")
    return (datetime.now(UTC) - dt).total_seconds()


def is_stale(dossier: Dossier | None, ttl: float = DEFAULT_TTL_SECONDS) -> bool:
    """Whether ``dossier`` should be rebuilt: absent, or older than ``ttl`` seconds. ``ttl<0``
    never expires; ``ttl==0`` always rebuilds."""
    if dossier is None:
        return True
    if ttl < 0:
        return False
    return _age_seconds(dossier.built_at) >= ttl


# ---------------------------------------------------------------------------
# Dossier build
# ---------------------------------------------------------------------------


def _upstream_of(entry) -> str:
    """The upstream ``owner/repo`` a contribution-target hive profiles: the explicit ``upstream``
    field (forks + external hives record it), else ``org/repo`` as a best effort."""
    up = str((entry or {}).get("upstream") or "")
    if up:
        return up
    return f"{entry.get('org', '')}/{entry.get('repo', '')}"


def build_dossier(hive: str, cfg=None, reader=read_upstream_files) -> Dossier:
    """Build (or refresh) the contribution dossier for ``hive`` and return it — the mechanical
    scaffold the contributor seat then enriches.

    Reads the upstream clone's layer-1 artifacts via ``reader`` (injectable for tests), detects the
    layer-4 AI-PR posture, and derives the explicit verdict + authorship strategy. Layers 2-3
    (historical conventions, push-back patterns) are seeded empty — they require the seat's
    PR-history reading. Does NOT persist; the caller stores it (so it can preview first)."""
    cfg = cfg if cfg is not None else config.load()
    entry = registry.resolve_hive(cfg, hive)  # raises typer.Exit on no/ambiguous match
    files = reader(registry.hive_dir(entry))
    requirements = scan_requirements(files)
    posture = detect_ai_posture(_requirements_text(files))
    return Dossier(
        hive=registry.hive_key(entry),
        upstream=_upstream_of(entry),
        built_at=_now(),
        requirements=requirements,
        conventions=[],
        pushback=[],
        posture=posture,
        verdict=verdict_for(posture),
        authorship_strategy=authorship_strategy_for(posture),
    )


def render_dossier(dossier: Dossier) -> str:
    """A human-readable rendering of a dossier (what ``contrib-profile show`` prints)."""
    r = dossier.requirements or {}

    def mark(v) -> str:
        return "✓" if v else "✗"

    lines = [
        f"# Contribution dossier — {dossier.hive}",
        f"upstream: {dossier.upstream}",
        f"built:    {dossier.built_at}",
        "",
        f"verdict:  {dossier.verdict}  (AI-PR posture: {dossier.posture})",
        f"strategy: {dossier.authorship_strategy}",
        "",
        "## Layer 1 — explicit requirements",
        f"  {mark(r.get('contributing'))} CONTRIBUTING          {r.get('contributing') or '—'}",
        f"  {mark(r.get('pr_template'))} PR template           {r.get('pr_template') or '—'}",
        f"  {mark(r.get('issue_template'))} issue template        {r.get('issue_template') or '—'}",
        f"  {mark(r.get('code_of_conduct'))} code of conduct     {r.get('code_of_conduct') or '—'}",
        f"  {mark(r.get('dco_sign_off'))} DCO / sign-off",
        f"  {mark(r.get('style_lint_format'))} style / lint / format",
        f"  {mark(r.get('test_and_ci'))} test-and-CI",
        "",
        f"## Layer 2 — historical conventions ({len(dossier.conventions)})",
        *(f"  - {c}" for c in dossier.conventions),
        f"## Layer 3 — push-back patterns ({len(dossier.pushback)})",
        *(f"  - {p}" for p in dossier.pushback),
    ]
    if not dossier.conventions and not dossier.pushback:
        lines.append("  (seat-enriched from merged-PR history — none recorded yet)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Outbound editor — queue, dedupe, gated publish
# ---------------------------------------------------------------------------

# The publication gate's reason marker (parallel to the review gate's `review <sha>` and the
# warden's `security:` marker) — a hard, human-only gate that must be RESOLVED before an outbound
# bead is filed upstream. `type=human` requires a manual `bd gate resolve`.
PUBLISH_GATE_MARKER = "bh:publish"
_PUBLISH_GATE_TYPE = "human"


def list_outbound(cwd) -> list[dict]:
    """The external hive's ``outbound:pending`` queue — staged outbound candidates not yet filed
    upstream (``publish:approved``). Keyed on the shared ``state`` vocabulary, filtered by
    :func:`state.is_outbound_candidate`. Empty on a read failure."""
    rows = bd.json(["list", "--label", OUTBOUND_PENDING, "--status", "open"], cwd) or []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if is_outbound_candidate(r.get("labels"))]


def outbound_queue(cwd, threshold: float = 0.5) -> dict:
    """The ``{rows, dupes}`` payload for the outbound editor: the ``outbound:pending`` queue plus
    the ``bd find-duplicates`` pairs touching it — the seat's input for aggregating related items
    into a curated, minimal set before publish. Reuses the beads-native dedupe (never reimplements
    it), same shape as the intake inbox."""
    from . import triage  # lazy: reuse the shared bd find-duplicates helpers (DRY)

    rows = list_outbound(cwd)
    ids = [r.get("id") for r in rows]
    pairs = triage.dupes_touching(triage.find_dupes(cwd, threshold=threshold), ids)
    return {"rows": rows, "dupes": pairs}


def _bead_gates(bead, cwd, include_resolved=True) -> list[dict]:
    """Every gate whose description names ``bead`` (open + resolved). Mirrors
    ``work_logic._bead_gates`` but owned here to keep this module off the work-plane import path."""
    gates = bd.json(["gate", "list", "--all", "--limit", "0"], cwd)
    if not isinstance(gates, list):
        return []
    needle = str(bead).lower()
    out = []
    for g in gates:
        if needle in str(g.get("description") or "").lower():
            if include_resolved or str(g.get("status")) == "open":
                out.append(g)
    return out


def publish_gate(bead, cwd) -> tuple[list[dict], list[dict]]:
    """The publication gates for ``bead`` — every gate carrying the ``bh:publish`` marker — split
    ``(open, resolved)``. The hard human gate the contributor consults before filing upstream."""
    matches = [
        g
        for g in _bead_gates(bead, cwd, include_resolved=True)
        if PUBLISH_GATE_MARKER in str(g.get("description") or "").lower()
    ]
    open_ = [g for g in matches if str(g.get("status")) == "open"]
    resolved = [g for g in matches if str(g.get("status")) != "open"]
    return open_, resolved


def open_publish_gate(cwd, bead, actor) -> tuple[int, str]:
    """Open the hard, human-only publication gate blocking ``bead`` (idempotent — reuses an already
    open one). Contributor-only: a non-contributor seat is refused (the seat that stages an outbound
    item for publish is the same seat that files it). Returns ``(exit, error)``."""
    if not guard.is_contributor(actor):
        return 1, (
            f"opening a publication gate is the contributor seat's job (contrib/<name>) — "
            f"{actor!r} is not a contributor."
        )
    open_gates, _resolved = publish_gate(bead, cwd)
    if open_gates:
        return 0, ""  # already gated — idempotent
    reason = f"{PUBLISH_GATE_MARKER} {bead} — human publication gate (external upstream)"
    res = bd.run(
        ["gate", "create", "--blocks", bead, "--type", _PUBLISH_GATE_TYPE, "--reason", reason],
        cwd,
        actor,
        capture=True,
    )
    if res.returncode:
        return res.returncode, f"could not open publication gate: {bd.err_line(res)}"
    return 0, ""


def publish(cwd, bead, actor, external_ref: str = "") -> tuple[int, str, str]:
    """File ONE curated outbound bead upstream, behind every gate. The contributor's single write.

    Refuses, in order: a non-contributor seat / a bare-sync-or-multi-item push (the generalized
    write-guard, ``guard.publish_refusal`` — "dirty" pushes); an already-published bead; an UNGATED
    push (no RESOLVED human publication gate). Only then files via the gated single-item path
    ``bd github push --issues <bead>``, flips ``outbound:pending`` → ``publish:approved``, and
    stamps ``external_ref`` (gh-#) for the resolution watch (bh-haak). Returns
    ``(exit, error, message)``."""
    push_args = ["github", "push", "--issues", bead]
    refusal = guard.publish_refusal(push_args, actor)
    if refusal is not None:
        return 1, refusal, ""

    data = bd.show(bead, cwd)
    if data is None:
        return 1, f"{bead} not found", ""
    if not is_outbound_candidate(data.get("labels")):
        return 1, (
            f"{bead} is not an outbound candidate ({OUTBOUND_PENDING} and not {PUBLISH_APPROVED}) "
            "— nothing to publish"
        ), ""

    _open, resolved = publish_gate(bead, cwd)
    if not resolved:
        return 1, (
            f"{bead} has no RESOLVED publication gate — an outbound push is refused until a human "
            f"resolves the hard publication gate. Open it with `{config.BINARY_ALIAS} contrib "
            "outbound` staging, then a human runs `bd gate resolve`."
        ), ""

    res = bd.run(push_args, cwd, actor, capture=True)
    if res.returncode:
        return res.returncode, f"upstream push failed: {bd.err_line(res)}", ""

    # Stamp the external_ref (gh-#) so the resolution watch (bh-haak) can follow the filed issue.
    if external_ref:
        upd = bd.run(["update", bead, "--external-ref", external_ref], cwd, actor, capture=True)
        if upd.returncode:
            msg = f"filed {bead} but could not stamp external_ref: {bd.err_line(upd)}"
            return upd.returncode, msg, ""

    # Flip outbound:pending → publish:approved (event-sourced, shared state vocabulary).
    reason = f"filed upstream via {config.BINARY_ALIAS} contrib publish"
    flip = bd.run(
        ["set-state", bead, _state_arg(PUBLISH_APPROVED), "--reason", reason],
        cwd,
        actor,
        capture=True,
    )
    if flip.returncode:
        msg = f"filed {bead} but could not flip to {PUBLISH_APPROVED}: {bd.err_line(flip)}"
        return flip.returncode, msg, ""

    ref = f" ({external_ref})" if external_ref else ""
    return 0, "", f"✓ filed {bead} upstream{ref} → {PUBLISH_APPROVED}"


def _state_arg(label: str) -> str:
    """A ``<dim>:<value>`` label-cache constant → the ``<dim>=<value>`` arg ``bd set-state`` takes
    (keeps the state vocabulary single-owned in ``state.py``)."""
    return label.replace(":", "=", 1)
