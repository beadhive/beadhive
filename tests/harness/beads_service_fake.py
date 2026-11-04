"""A fake Beads v1.3 HTTP service over a ``FakeBd`` world, for the intake report store's API route.

``handle`` is an ``httpx.MockTransport`` handler: a real :class:`BeadsSession` (its generated
client, its context negotiation, its request models) talks to it, so what the store sends is
checked on the wire, not against a hand-written stand-in for the session. It serves only the
operations the report store uses (get / list issues, update, close, add / remove dependency,
compare-and-set metadata) over the SAME issue and edge tables a ``FakeBd`` keeps, so a CLI-only
operation (a gate, ``set-state``) and an API one see one world.

Every request is recorded in ``requests`` as ``(method, path, query, body)``; ``fail`` scripts a
one-shot failure for ``"<METHOD> <path-suffix>"``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

NOW = "2026-01-01T00:00:00+00:00"
BLOCKING = {"blocks", "conditional-blocks", "waits-for"}
CAPABILITIES = [
    "project.enforce",
    "issues.get",
    "issues.list",
    "ready.list",
    "issues.update",
    "issues.close",
    "issues.casMetadata",
    "dependencies.add",
    "dependencies.remove",
]


def problem(status: int, code: str, detail: str = "") -> httpx.Response:
    body = {"status": status, "title": code, "code": code, "request_id": "r-1"}
    if detail:
        body["detail"] = detail
    return httpx.Response(status, json=body)


def _issue(row: dict[str, Any]) -> dict[str, Any]:
    """A fake-bd row as the service's ``Issue`` (the required members filled in)."""
    out = {
        "id": row["id"],
        "title": row.get("title", "t"),
        "priority": row.get("priority", 2),
        "created_at": NOW,
        "updated_at": NOW,
        "revision": "0",
        "status": row.get("status", "open"),
        "issue_type": row.get("issue_type", "task"),
        "labels": sorted(row.get("labels") or []),
    }
    for key in ("description", "external_ref", "assignee", "metadata", "close_reason"):
        if key in row:
            out[key] = row[key]
    return out


class FakeBeadsService:
    def __init__(
        self, bd: Any, *, capabilities: list[str] | None = None, repo_root: Any = None
    ) -> None:
        self.bd = bd
        self.repo_root = repo_root
        self.capabilities = CAPABILITIES if capabilities is None else capabilities
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.fail: dict[str, int] = {}

    # -- what a test reads ---------------------------------------------------------------------

    def operations(self) -> list[str]:
        """``"<METHOD> <path>"`` of every request after the session's own negotiation."""
        skipped = {"/healthz", "/v0/beads/context", "/v0/beads/ready"}
        return [f"{m} {p}" for m, p, _q, _b in self.requests if p not in skipped]

    def writes(self) -> list[tuple[str, str, Any]]:
        return [(m, p, b) for m, p, _q, b in self.requests if m != "GET"]

    # -- the transport -----------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {key: value for key, value in request.url.params.multi_items()}
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, query, body))
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v0/beads/context":
            context = {
                "api_version": "v0",
                "bd_version": "1.3.0",
                "schema_version": 1,
                "backend": "dolt",
                "dolt_mode": "server",
                "database": "db",
                "project_id": "p",
                "capabilities": self.capabilities,
            }
            if self.repo_root is not None:
                context["repo_root"] = str(self.repo_root)
            return httpx.Response(200, json=context)
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        for suffix, status in list(self.fail.items()):
            method, _, tail = suffix.partition(" ")
            if request.method == method and path.endswith(tail):
                del self.fail[suffix]
                if status >= 500:
                    return problem(status, "internal")
                return problem(status, "invalid_argument", "scripted refusal")
        return self._route(request, path, request.url.params, body)

    def _route(self, request: httpx.Request, path: str, params: Any, body: Any) -> httpx.Response:
        if path == "/v0/beads/issues" and request.method == "GET":
            return self._list(params)
        if path == "/v0/beads/dependencies:add":
            return self._add(body)
        if path == "/v0/beads/dependencies:remove":
            return self._remove(body)
        tail = path.removeprefix("/v0/beads/issues/")
        bead, _, verb = tail.partition(":")
        if bead not in self.bd.issues:
            return problem(404, "not_found", f"no issue {bead}")
        if request.method == "GET":
            return self._get(bead, params)
        if request.method == "PATCH":
            return self._update(bead, body)
        if verb == "close":
            return self._close(bead, body)
        if verb == "casMetadata":
            return self._cas(bead, body)
        return problem(400, "invalid_argument", f"unhandled {request.method} {path}")

    # -- reads -------------------------------------------------------------------------------

    def _edges_of(self, bead: str, *, down: bool) -> list[dict[str, Any]]:
        out = []
        for source, target, kind in self.bd.edges:
            if (source if down else target) == bead:
                other = self.bd.issues[target if down else source]
                out.append({**_issue(other), "dependency_type": kind})
        return out

    def _get(self, bead: str, params: Any) -> httpx.Response:
        row = _issue(self.bd.issues[bead])
        deps = self._edges_of(bead, down=True)
        if deps:
            row["dependencies"] = deps
        if params.get("include_dependents") == "true":
            dependents = self._edges_of(bead, down=False)
            if dependents:
                row["dependents"] = dependents
        return httpx.Response(200, json=row)

    def _list(self, params: Any) -> httpx.Response:
        rows = list(self.bd.issues.values())
        wanted_status = params.get_list("status")
        if wanted_status:
            rows = [r for r in rows if r["status"] in wanted_status]
        elif params.get("all") != "true":
            rows = [r for r in rows if r["status"] != "closed"]
        if params.get("include_gates") != "true":
            rows = [r for r in rows if r.get("issue_type") != "gate"]
        for label in params.get_list("label"):
            rows = [r for r in rows if label in r.get("labels", [])]
        key = params.get("has_metadata_key")
        if key:
            rows = [r for r in rows if key in (r.get("metadata") or {})]
        start = int(params.get("cursor") or 0)
        limit = int(params.get("limit") or 50) or len(rows) or 1
        page = rows[start : start + limit]
        items = [
            {**_issue(r), "dependency_count": 0, "dependent_count": 0, "comment_count": 0}
            for r in page
        ]
        more = start + limit < len(rows)
        out: dict[str, Any] = {"items": items, "has_more": more}
        if more:
            out["next_cursor"] = str(start + limit)
        return httpx.Response(200, json=out)

    # -- writes ------------------------------------------------------------------------------

    def _update(self, bead: str, body: Any) -> httpx.Response:
        row = self.bd.issues[bead]
        patch = body["patch"]
        for key in ("issue_type", "priority", "assignee"):
            if key in patch:
                row[key] = patch[key]
        return httpx.Response(200, json={"issue": _issue(row), "changed": True, "revision": "1"})

    def _close(self, bead: str, body: Any) -> httpx.Response:
        row = self.bd.issues[bead]
        live = [
            target
            for source, target, kind in self.bd.edges
            if source == bead and kind in BLOCKING and self.bd.issues[target]["status"] != "closed"
        ]
        if live and not body.get("force"):
            return problem(409, "not_closable", f"{bead} is blocked by {', '.join(live)}")
        already = row["status"] == "closed"
        if not already:
            row["status"] = "closed"
            row["close_reason"] = body.get("reason", "")
        return httpx.Response(
            200,
            json={
                "issue": _issue(row),
                "already_closed": already,
                "open_children": 0,
                "revision": "1",
            },
        )

    def _add(self, body: Any) -> httpx.Response:
        for edge in body["edges"]:
            for end in (edge["issue_id"], edge["depends_on_id"]):
                if end not in self.bd.issues:
                    return problem(400, "invalid_argument", f"no issue {end}")
            triple = (edge["issue_id"], edge["depends_on_id"], edge["type"])
            if triple not in self.bd.edges:
                self.bd.edges.append(triple)
        return httpx.Response(200, json={"added": body["edges"]})

    def _remove(self, body: Any) -> httpx.Response:
        pair = (body["issue_id"], body["depends_on_id"])
        before = len(self.bd.edges)
        self.bd.edges = [e for e in self.bd.edges if e[:2] != pair]
        return httpx.Response(200, json={"removed": len(self.bd.edges) < before})

    def _cas(self, bead: str, body: Any) -> httpx.Response:
        row = self.bd.issues[bead]
        metadata = row.setdefault("metadata", {})
        key = body["key"]
        present = key in metadata
        held = ("expected" not in body and not present) or (
            "expected" in body and present and metadata[key] == body["expected"]
        )
        if not held:
            out: dict[str, Any] = {"swapped": False}
            if present:
                out["current"] = metadata[key]
            return httpx.Response(200, json=out)
        metadata[key] = body["value"]
        return httpx.Response(200, json={"swapped": True, "current": body["value"]})
