"""Reproduction: bd's incremental GitHub pull skips an issue edited during a sync (bh-clqa5).

Integration: the REAL ``bd`` (1.3.x) runs ``bd github sync --pull-only --prefer-github`` — the
exact shape :mod:`beadhive.github_projection` runs — against an embedded scratch store in
``tmp_path`` and an in-process GitHub REST stub on loopback (``GITHUB_API_URL``). No production
store, no network, no sleep.

bd records ``github.last_sync`` = end-of-sync wall clock rounded UP to the next whole second, and
sends it as the next pull's ``since``. The stub edits issue #2 right after serving one sync's
listing — after the fetch began, before bd writes its mark — stamping the edit with its injected
clock floored to the second (GitHub's resolution). The outcome is ordering-determined, not
timing-dependent: the stamp is at most bd's end-of-sync second, and the mark is that second plus
one, so the stamp is always strictly older than the next ``since``.

Sync 1 lands three issues and the test commits them (as ``tracker_sync.commit_after_pull`` would):
until rows are in Dolt history bd also re-fetches every linked issue one by one, which hides the
skip. Sync 2 only moves the mark past that commit. Sync 3's listing triggers the edit; sync 4 asks
for issues updated since a mark newer than the edit and never sees it. Re-pulling the number —
the overlap re-pull's ``--issues=2`` — repairs it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from beadhive_bd_cli import github_pull_only_argv, github_repull_argv

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed"),
]

OWNER, REPO = "acme", "widgets"
_TIMEOUT = 120
_SHARED_SERVER_ENV = frozenset(
    {"BEADS_DOLT_SHARED_SERVER", "BEADS_SHARED_SERVER_DIR", "BEADS_DOLT_SERVER_PORT"}
)
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).strftime(_STAMP)


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC).timestamp()


class GitHubStub:
    """``GET /repos/{o}/{r}/issues`` (GitHub's inclusive ``since``) and ``/issues/{n}``.

    ``edit_on_list`` names the listing (1-based) right after which issue #2 is edited, stamped
    ``floor(clock())``. Every listing is logged with its query and what it returned."""

    def __init__(self, *, clock: Callable[[], float], edit_on_list: int) -> None:
        base = _epoch("2026-01-01T00:00:00Z")
        self.issues = {
            n: {
                "id": 1000 + n,
                "number": n,
                "title": f"issue {n}",
                "body": f"body {n}",
                "state": "open",
                "labels": [],
                "user": {"login": "octo", "id": 1},
                "html_url": f"https://github.com/{OWNER}/{REPO}/issues/{n}",
                "created_at": _iso(base + n),
                "updated_at": _iso(base + n),
                "closed_at": None,
            }
            for n in (1, 2, 3)
        }
        self.clock = clock
        self.edit_on_list = edit_on_list
        self.listings: list[dict] = []
        self.edit_stamp: str | None = None

    def handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, code: int, body: object) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                parts = [p for p in url.path.split("/") if p]
                if parts == ["repos", OWNER, REPO, "issues"]:
                    return self._send(200, stub.list(query))
                if parts[:4] == ["repos", OWNER, REPO, "issues"] and len(parts) == 5:
                    row = stub.issues.get(int(parts[4]))
                    return self._send(200, row) if row else self._send(404, {})
                return self._send(404, {"message": "Not Found"})

        return Handler

    def list(self, query: dict) -> list:
        rows = sorted(self.issues.values(), key=lambda row: row["created_at"])
        if "since" in query:
            rows = [r for r in rows if _epoch(r["updated_at"]) >= _epoch(query["since"])]
        if query.get("page", "1") != "1":
            rows = []
        served = json.loads(json.dumps(rows))  # the page as it stood when it was served
        if query.get("page", "1") == "1":
            self.listings.append({"since": query.get("since"), "returned": len(served)})
            if len(self.listings) == self.edit_on_list:
                self.edit_stamp = _iso(self.clock())
                self.issues[2].update(title="issue 2 EDITED during a sync")
                self.issues[2].update(updated_at=self.edit_stamp)
        return served


@pytest.fixture
def stub() -> Iterator[tuple[GitHubStub, str]]:
    # The stub's clock is GitHub's: an edit is stamped "now", as GitHub would. bd's own clock
    # cannot be injected; the assertion relies only on the ordering argued in the module doc.
    github = GitHubStub(clock=time.time, edit_on_list=3)
    server = ThreadingHTTPServer(("127.0.0.1", 0), github.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield github, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--prefix", "ghs", "--quiet", "--non-interactive"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env=_env(tmp_path, ""),
        timeout=_TIMEOUT,
    )
    return tmp_path


def _env(root: Path, api: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SHARED_SERVER_ENV}
    for name in ("GH_TOKEN", "GITHUB_REPOSITORY"):
        env.pop(name, None)
    env.update(
        BEADS_DIR=str(root / ".beads"),
        GITHUB_OWNER=OWNER,
        GITHUB_REPO=REPO,
        GITHUB_TOKEN="stub-token-not-real",
        GITHUB_API_URL=api,
        BD_NON_INTERACTIVE="1",
    )
    return env


def _bd(root: Path, api: str, *args: str) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["bd", "-C", str(root), *args],
        env=_env(root, api),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert res.returncode == 0, (args, res.stdout, res.stderr)
    return res


def _title(root: Path, api: str, number: int) -> str:
    rows = json.loads(_bd(root, api, "list", "--all", "--json", "--limit", "0").stdout)
    ref = f"https://github.com/{OWNER}/{REPO}/issues/{number}"
    return next(row["title"] for row in rows if row.get("external_ref") == ref)


def test_bd_incremental_pull_skips_an_issue_edited_during_a_sync(store, stub):
    github, api = stub
    _bd(store, api, *github_pull_only_argv())  # sync 1: three issues created
    _bd(store, api, "dolt", "commit", "-m", "commit the pulled rows")
    _bd(store, api, *github_pull_only_argv())  # sync 2: the mark passes the commit
    _bd(store, api, *github_pull_only_argv())  # sync 3: #2 is edited after its listing
    assert github.edit_stamp is not None
    _bd(store, api, *github_pull_only_argv())  # sync 4

    after_edit = github.listings[3]["since"]
    assert _epoch(after_edit) > _epoch(github.edit_stamp)  # bd's mark is newer than the edit
    assert github.listings[3]["returned"] == 0
    assert _title(store, api, 2) == "issue 2"  # GitHub says EDITED; the store never will

    _bd(store, api, *github_repull_argv([2]))  # the overlap re-pull's shape
    assert _title(store, api, 2) == "issue 2 EDITED during a sync"
