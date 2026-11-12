"""The GitHub snapshot: ``fetch`` (network) writes it, :mod:`.reader` parses it (pure).

Design: ``docs/design/backfill-history-pipeline-adr.md`` (D6, the ``github`` reader). Re-extraction
stays deterministic and offline because the only network step is this one, and its output is a
plain JSON file the reader replays.

The fetch reuses the plugin's own transport and credential path: it takes any ``get`` callable
shaped like :meth:`~beadhive_tracker_github.adapter.transport.Session.get` (``get(target, params)``
returning a ``Reply``), so the host passes ``session.get`` from ``Transport.operation(...)`` and
tests pass a fake. No GitHub client is imported here (githubkit stays lazily inside ``adapter/``).

Snapshot schema ``bdlg.github-snapshot/1`` (every key always present, keys sorted, rows sorted by
number, no clock value, so the same repository state is the same bytes)::

    {"schema": "bdlg.github-snapshot/1", "repo": "owner/name", "complete": true,
     "resume": null (complete) or {"stage": pulls|commits|issues, "next": url|null, "pull": N|null},
     "pulls":  [{"number", "title", "body", "state" (open|closed|merged), "draft",
                 "opened_at", "closed_at", "merged_at"   (as GitHub wrote them, or null),
                 "commits" (shas, API order, or null until fetched), "closes" (issue numbers)}],
     "issues": [{"number", "title", "body", "state" (open|closed), "state_reason", "labels"
                 (sorted names), "assignees" (logins, API order), "opened_at", "closed_at"}]}

``closes`` are the issue numbers named by a closing keyword (``closes #N``, ``fixes owner/repo#N``)
in the pull request's title or body: the REST API has no closing-reference field (that is GraphQL
only), so this is the text GitHub itself acts on, not the platform's resolved link.

Paginated and resumable: pages are followed by ``Link: rel="next"`` verbatim (after the host
check, :func:`check_next`); ``resume`` records the stage and the next URL, so a partial snapshot
passed back as ``previous`` continues where it stopped. ``max_pages`` bounds one call (the
snapshot is then ``complete: false``); ``on_progress`` is called after every page with the
snapshot so far, for the caller to persist before a later page fails.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .adapter.urls import next_link

SNAPSHOT_SCHEMA = "bdlg.github-snapshot/1"
PAGE_SIZE = 100

_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?[ \t]+(?:[\w.-]+/[\w.-]+)?#([0-9]+)", re.I
)


class SnapshotError(ValueError):
    """A snapshot file or payload outside ``bdlg.github-snapshot/1``."""


class Getter(Protocol):
    """``Session.get``: GET an API path (or a followed ``next`` URL) and return a ``Reply``."""

    def __call__(self, target: str, params: Mapping[str, str] | None = None) -> Any: ...


def check_next(url: str, api_base: str) -> str:
    """``url`` when it is under ``api_base`` (same origin and path prefix), else a refusal:
    a ``next`` link is never followed to another host with the credential attached."""
    base, link = urlsplit(api_base.rstrip("/")), urlsplit(url)
    prefix = base.path.rstrip("/")
    if (link.scheme, link.netloc) != (base.scheme, base.netloc) or not (
        link.path == prefix or link.path.startswith(prefix + "/")
    ):
        raise SnapshotError("a next link left the API origin")
    return url


def closing_numbers(*texts: str | None) -> list[int]:
    """The issue numbers named by closing keywords in ``texts``, ascending, de-duplicated."""
    found = {int(m.group(1)) for text in texts for m in _CLOSING.finditer(text or "")}
    return sorted(found)


def _pull(row: Mapping[str, Any]) -> dict[str, Any]:
    merged = row.get("merged_at")
    state = "merged" if merged else str(row.get("state") or "open")
    return {
        "number": int(row["number"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "state": state,
        "draft": bool(row.get("draft", False)),
        "opened_at": row.get("created_at"),
        "closed_at": row.get("closed_at"),
        "merged_at": merged,
        "commits": None,
        "closes": closing_numbers(row.get("title"), row.get("body")),
    }


def _issue(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "number": int(row["number"]),
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "state": str(row.get("state") or "open"),
        "state_reason": row.get("state_reason"),
        "labels": sorted(
            str(label["name"] if isinstance(label, Mapping) else label)
            for label in row.get("labels") or ()
        ),
        "assignees": [str(a["login"]) for a in row.get("assignees") or ()],
        "opened_at": row.get("created_at"),
        "closed_at": row.get("closed_at"),
    }


def empty_snapshot(repo: str) -> dict[str, Any]:
    """A fresh, incomplete snapshot of ``repo`` (``owner/name``) at the first stage."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "repo": repo,
        "complete": False,
        "resume": {"stage": "pulls", "next": None, "pull": None},
        "pulls": [],
        "issues": [],
    }


def _normalize(snapshot: dict[str, Any]) -> dict[str, Any]:
    snapshot["pulls"] = sorted(snapshot["pulls"], key=lambda r: r["number"])
    snapshot["issues"] = sorted(snapshot["issues"], key=lambda r: r["number"])
    return snapshot


def _rows(reply: Any) -> list[Any]:
    body = reply.body
    if not isinstance(body, list):
        raise SnapshotError("a list endpoint answered a non-list body")
    return body


def fetch_snapshot(
    get: Getter,
    repo: str,
    *,
    api_base: str = "https://api.github.com",
    previous: Mapping[str, Any] | None = None,
    max_pages: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch the pull requests and issues of ``repo``, resuming ``previous`` if given.

    Stages: ``pulls`` (``/pulls?state=all``), ``commits`` (one ``/pulls/N/commits`` listing per
    pull request still lacking its shas) then ``issues`` (``/issues?state=all``, pull requests
    skipped). Returns the snapshot; ``complete`` is false when ``max_pages`` stopped the call.
    """
    snap = validate_snapshot(previous, repo=repo) if previous is not None else empty_snapshot(repo)
    snap = json.loads(json.dumps(snap))  # never mutate the caller's copy
    if snap["complete"]:
        return snap
    pages = 0
    resume = snap["resume"]

    def budget_spent() -> bool:
        return max_pages is not None and pages >= max_pages

    def fetch_page(target: str, params: Mapping[str, str] | None) -> Any:
        nonlocal pages
        pages += 1
        return get(target, params)

    def progress() -> None:
        _normalize(snap)
        if on_progress is not None:
            on_progress(json.loads(json.dumps(snap)))

    base = f"/repos/{repo}"
    stage = resume["stage"]
    if stage == "pulls":
        target, params = resume["next"], None
        if target is None:
            target = f"{base}/pulls"
            params = {"state": "all", "sort": "created", "direction": "asc", "per_page": "100"}
        else:
            target = check_next(target, api_base)
        while True:
            if budget_spent():
                return _halt(snap, "pulls", target, params)
            reply = fetch_page(target, params)
            known = {r["number"]: r for r in snap["pulls"]}
            for row in _rows(reply):
                known[int(row["number"])] = _pull(row)
            snap["pulls"] = list(known.values())
            nxt = next_link(reply.headers.get("link"))
            if nxt is None:
                snap["resume"] = {"stage": "commits", "next": None, "pull": None}
                progress()
                break
            target, params = check_next(nxt, api_base), None
            snap["resume"] = {"stage": "pulls", "next": target, "pull": None}
            progress()
        stage = "commits"
    if stage == "commits":
        # A pull request whose listing spans pages keeps its shas so far in ``commits`` and its
        # next page in ``resume`` (``pull`` names it); every other one waits as ``None``.
        pending = resume.get("pull") if resume["stage"] == "commits" else None
        for pull in snap["pulls"]:
            continuing = pending == pull["number"] and resume["next"] is not None
            if pull["commits"] is not None and not continuing:
                continue
            shas: list[str] = list(pull["commits"] or []) if continuing else []
            if continuing:
                target, params = check_next(resume["next"], api_base), None
            else:
                target, params = f"{base}/pulls/{pull['number']}/commits", {"per_page": "100"}
            while True:
                if budget_spent():
                    pull["commits"] = shas or None
                    snap["resume"] = {
                        "stage": "commits",
                        "next": target if params is None else None,
                        "pull": pull["number"] if params is None else None,
                    }
                    return _normalize(snap)
                reply = fetch_page(target, params)
                shas.extend(str(c["sha"]) for c in _rows(reply))
                nxt = next_link(reply.headers.get("link"))
                if nxt is None:
                    break
                target, params = check_next(nxt, api_base), None
            pull["commits"] = shas
            snap["resume"] = {"stage": "commits", "next": None, "pull": None}
            progress()
        snap["resume"] = {"stage": "issues", "next": None, "pull": None}
        stage = "issues"
    if stage == "issues":
        target, params = resume["next"] if resume["stage"] == "issues" else None, None
        if target is None:
            target = f"{base}/issues"
            params = {"state": "all", "sort": "created", "direction": "asc", "per_page": "100"}
        else:
            target = check_next(target, api_base)
        while True:
            if budget_spent():
                return _halt(snap, "issues", target, params)
            reply = fetch_page(target, params)
            known = {r["number"]: r for r in snap["issues"]}
            for row in _rows(reply):
                if "pull_request" not in row:
                    known[int(row["number"])] = _issue(row)
            snap["issues"] = list(known.values())
            nxt = next_link(reply.headers.get("link"))
            if nxt is None:
                break
            target, params = check_next(nxt, api_base), None
            snap["resume"] = {"stage": "issues", "next": target, "pull": None}
            progress()
    snap["complete"], snap["resume"] = True, None
    progress()
    return snap


def _halt(
    snap: dict[str, Any], stage: str, target: str | None, params: Mapping[str, str] | None
) -> dict[str, Any]:
    """Stop at the page budget: ``resume`` names the stage and, when the next page is a followed
    link (not the first request of the stage), its URL."""
    snap["resume"] = {"stage": stage, "next": target if params is None else None, "pull": None}
    return _normalize(snap)


_PULL_KEYS = (
    "body", "closed_at", "closes", "commits", "draft", "merged_at", "number", "opened_at",
    "state", "title",
)  # fmt: skip
_ISSUE_KEYS = (
    "assignees", "body", "closed_at", "labels", "number", "opened_at", "state", "state_reason",
    "title",
)  # fmt: skip
_TOP_KEYS = ("complete", "issues", "pulls", "repo", "resume", "schema")


def validate_snapshot(raw: Any, *, repo: str | None = None) -> dict[str, Any]:
    """``raw`` as a snapshot dict, or :class:`SnapshotError` naming what is wrong."""
    if not isinstance(raw, Mapping) or sorted(raw) != list(_TOP_KEYS):
        raise SnapshotError(f"not a {SNAPSHOT_SCHEMA} snapshot: members must be {list(_TOP_KEYS)}")
    if raw["schema"] != SNAPSHOT_SCHEMA:
        raise SnapshotError(f"unknown snapshot schema {raw['schema']!r}")
    if repo is not None and raw["repo"] != repo:
        raise SnapshotError(f"snapshot is of {raw['repo']!r}, not {repo!r}")
    for name, keys in (("pulls", _PULL_KEYS), ("issues", _ISSUE_KEYS)):
        rows = raw[name]
        if not isinstance(rows, list):
            raise SnapshotError(f"{name} must be an array")
        for row in rows:
            if not isinstance(row, Mapping) or sorted(row) != list(keys):
                raise SnapshotError(f"a {name} row must have exactly the members {list(keys)}")
    return dict(raw)


def dumps_snapshot(snapshot: Mapping[str, Any]) -> str:
    """The snapshot as its canonical text: sorted keys, two-space indent, final newline."""
    return json.dumps(snapshot, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_snapshot(path: Path | str, snapshot: Mapping[str, Any]) -> None:
    """Write ``snapshot`` to ``path`` atomically (a resumable fetch rewrites it per page)."""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(dumps_snapshot(snapshot), encoding="utf-8")
    tmp.replace(target)


def load_snapshot(path: Path | str) -> dict[str, Any]:
    """Read and validate a snapshot file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SnapshotError(f"cannot read snapshot {path}: {error}") from error
    return validate_snapshot(raw)


__all__ = [
    "PAGE_SIZE",
    "SNAPSHOT_SCHEMA",
    "Getter",
    "SnapshotError",
    "check_next",
    "closing_numbers",
    "dumps_snapshot",
    "empty_snapshot",
    "fetch_snapshot",
    "load_snapshot",
    "validate_snapshot",
    "write_snapshot",
]
