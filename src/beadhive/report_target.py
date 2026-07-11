"""`ws report-target [--json]` — ws self-description per the report-channel protocol.

Emits ws's own report_channel discovery document (bead schema): where
and how to file issues about ws itself. The document is the seed a future auto-router consumes
to route ``ws escalate --tool ws``.

Shape emitted (always a conforming discovery document per the schema at
``docs/schemas/report-channel.schema.json``):

    {
      "version": "1",
      "channels": [
        {
          "kind": "beads-rig",
          "target": "<provider>/<org>/<repo>",
          "verb": "ws report <triplet> \\"<title>\\"",
          "labels": ["intake:untriaged"]
        }
      ]
    }

Self-triplet resolution order (no hardcoding where avoidable):
  1. ``git worktree list --porcelain`` → first (main) worktree path → ``workspace_identity``.
     Works from linked worktrees where the process cwd is NOT under ``$GIT_WORKSPACE``.
  2. ``workspace_identity()`` on the process cwd — the common case when run from the repo root.
  3. Return ``None``; the caller emits an error.

No consumption / auto-routing logic here — pure emission only.
"""

from __future__ import annotations

import json

from . import config
from .identity import workspace_identity
from .run import run

# Schema URI (the .2 spec artifact)
_SCHEMA_URI = (
    "https://raw.githubusercontent.com/briancripe/workspace/main/"
    "docs/schemas/report-channel.schema.json"
)

_LABELS = ["intake:untriaged"]


def _resolve_self_triplet() -> tuple[str, str, str] | None:
    """Resolve ws's own ``(provider, org, repo)`` triplet without hardcoding.

    Tries the git main-worktree path first so the command works correctly from a linked
    worktree (where ``git rev-parse --show-toplevel`` returns the worktree path, which lives
    under ``~/.ws/wt/…``, outside ``$GIT_WORKSPACE``).
    """
    # 1. git worktree list → main worktree path → workspace_identity
    res = run(["git", "worktree", "list", "--porcelain"], check=False, capture=True)
    if res.returncode == 0:
        for line in (res.stdout or "").splitlines():
            if line.startswith("worktree "):
                main_wt = line[len("worktree "):].strip()
                ident = workspace_identity(cwd=main_wt)
                if ident:
                    return ident

    # 2. Fallback: process cwd (works when run directly from the repo root)
    return workspace_identity()


def self_document() -> dict | None:
    """Build and return ws's own discovery document, or ``None`` when the triplet cannot be
    resolved (not inside a managed git workspace)."""
    triplet_parts = _resolve_self_triplet()
    if triplet_parts is None:
        return None

    provider, org, repo = triplet_parts
    triplet = f"{provider}/{org}/{repo}"
    verb = f'{config.BINARY_ALIAS} report {triplet} "<title>"'

    return {
        "$schema": _SCHEMA_URI,
        "version": "1",
        "channels": [
            {
                "kind": "beads-rig",
                "target": triplet,
                "verb": verb,
                "labels": _LABELS,
            }
        ],
    }


def emit(as_json: bool = False) -> int:
    """Emit ws's report-channel discovery document.

    Returns an exit code (0 = success, 1 = error).  When ``as_json`` is True the document
    is written as a compact JSON object on stdout.  The human-readable form prints the
    primary channel fields as a short summary.
    """
    import typer

    doc = self_document()
    if doc is None:
        typer.echo(
            f"✗ could not resolve {config.BINARY_ALIAS}'s own rig identity — "
            "run from inside a managed workspace",
            err=True,
        )
        return 1

    if as_json:
        typer.echo(json.dumps(doc))
    else:
        channel = doc["channels"][0]
        typer.echo(f"kind:   {channel['kind']}")
        typer.echo(f"target: {channel['target']}")
        typer.echo(f"verb:   {channel['verb']}")
        typer.echo(f"labels: {', '.join(channel.get('labels', []))}")
    return 0
