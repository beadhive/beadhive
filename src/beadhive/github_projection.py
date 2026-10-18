"""Project a repository's GitHub Issues into its PUBLIC bead store — pull-only (bh-lniwf.6).

docs/design/two-store-scope-adr.md section 3: the public store is GitHub-authoritative and
pull-only. This module is the one sanctioned way to fill it, and it deliberately does very
little itself: Beads already ships the GitHub integration (``bd github sync``), so bh only

* resolves the PUBLIC store and nothing else (:func:`store_target.resolve_store` with scope
  ``public`` — a legacy or internal-only hive raises ``StoreNotConfigured``, never a fallback),
  and requires ``source: github-issues`` + ``sync: pull-only``;
* derives owner/repository from the hive's own identity triplet, refuses a store whose bd
  config names a different repository, and hands both to the child as ``GITHUB_OWNER`` /
  ``GITHUB_REPO`` — never written to any config;
* passes the credential through AMBIENTLY (``GITHUB_TOKEN`` / ``GH_TOKEN`` / ``gh auth token``)
  into the child environment only, and scrubs it from everything echoed back;
* runs exactly ``bd github sync --pull-only --prefer-github [--dry-run]`` through the store's
  transport (``-C <store root>`` + ``BEADS_DIR``), re-checked by
  :func:`guard.public_store_github_refusal` so no push shape can be built here;
* verifies identity afterwards: every projected row carries ``external_ref`` = the issue's
  GitHub URL for THIS repository, and no URL appears twice (a re-pull updates in place).

Nothing here writes to GitHub, touches the internal store, or changes routing. Mapper limits
(comments, milestones, deletions, label vocabulary) are documented in
docs/design/github-issue-projection.md.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from . import bd_cli, guard, store_target
from .store_target import SCOPE_PUBLIC, StoreTarget

#: The only ``stores.public.source`` this projection serves.
SOURCE_GITHUB_ISSUES = "github-issues"
#: ``--json`` contract version for ``bh hive project-issues`` (see ``jsonout``).
PROJECT_ISSUES_SCHEMA = 1
#: A pull pages through every issue; bd's default 120s bound is too tight for a large tracker.
PULL_TIMEOUT_SECONDS = 900.0

_REPO_KEYS = ("github.owner", "github.repo", "github.repository")
_PULLED = re.compile(r"Pulled (\d+) issues \((\d+) created, (\d+) updated\)")
_WOULD_IMPORT = re.compile(r"\[dry-run\] Would import: (\d+) - ")
_WOULD_UPDATE = re.compile(r"\[dry-run\] Would update local issue: (\d+) - ")
_REDACTED = "***"

#: ``(environ) -> token or None``. The default reads the ambient environment, then ``gh``.
TokenSource = Callable[[Mapping[str, str]], "str | None"]
#: ``(target, extra_env) -> CliRoutes``. Tests inject a recorded fake.
RoutesFactory = Callable[[StoreTarget, Mapping[str, str]], Any]


class ProjectionError(RuntimeError):
    """The projection refused, or bd reported a failure; nothing was written to GitHub."""


@dataclass(frozen=True)
class Projection:
    """The resolved public store plus the one GitHub repository it projects."""

    target: StoreTarget
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class ProjectionReport:
    hive: str
    store: str
    beads_dir: str
    repository: str
    dry_run: bool
    pulled: int = 0
    created: int = 0
    updated: int = 0
    would_import: list[int] = field(default_factory=list)
    would_update: list[int] = field(default_factory=list)
    projected: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


def resolve_projection(entry: Mapping[str, Any]) -> Projection:
    """The hive's public store and GitHub repository, or a refusal naming why not.

    Never answers from the internal or legacy store: :func:`store_target.resolve_store` raises
    ``StoreNotConfigured`` for a hive without a public store."""
    target = store_target.resolve_store(entry, SCOPE_PUBLIC)
    if target.scope != SCOPE_PUBLIC or target.operational:  # defence in depth
        raise ProjectionError(f"{target.hive}: refusing to project into the {target.scope} store")
    if target.source != SOURCE_GITHUB_ISSUES:
        raise ProjectionError(
            f"{target.hive}: the public store's source is {target.source!r}, not "
            f"{SOURCE_GITHUB_ISSUES!r}; nothing to project"
        )
    if target.sync != "pull-only":
        raise ProjectionError(
            f"{target.hive}: the public store's sync is {target.sync!r}; the GitHub projection "
            "runs only for a pull-only store"
        )
    if str(entry.get("provider", "")) != "github":
        raise ProjectionError(f"{target.hive}: not a GitHub repository")
    owner, repo = str(entry.get("org", "") or ""), str(entry.get("repo", "") or "")
    if not owner or not repo:
        raise ProjectionError(f"{target.hive}: the hive identity names no owner/repository")
    return Projection(target=target, owner=owner, repo=repo)


def ambient_token(environ: Mapping[str, str]) -> str | None:
    """The ambient GitHub credential through bh's one child-env launcher (``run.child_env``):
    ``GITHUB_TOKEN``, then ``GH_TOKEN`` mirrored, then a fresh ``gh auth token`` — never
    persisted, never logged.

    None when none is available — bd may still find a host-local ``github.token`` in the store's
    own config.yaml. The value is never stored or printed by bh."""
    from .run import GITHUB_TOKEN_VAR
    from .run import child_env as launcher_env  # lazy: keep import-light

    return launcher_env(environ, github_token=True).get(GITHUB_TOKEN_VAR) or None


def child_env(projection: Projection, token: str | None) -> dict[str, str]:
    """Variables added to the bd child ONLY: the repository, and the credential if resolved."""
    env = {"GITHUB_OWNER": projection.owner, "GITHUB_REPO": projection.repo}
    if token:
        env["GITHUB_TOKEN"] = token
    return env


def _default_routes(target: StoreTarget, extra_env: Mapping[str, str]) -> Any:
    return store_target.routes(target, extra_env=extra_env)


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def _tail(res: Any, secrets: list[str]) -> str:
    """bd's last diagnostic line — stderr first, then stdout — with the credential scrubbed."""
    for name in ("stderr", "stdout"):
        raw = _scrub(str(getattr(res, name, "") or ""), secrets)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"exit {getattr(res, 'returncode', '?')}"


def _repo_conflicts(config: Any, projection: Projection) -> list[str]:
    """Store-config keys that name a different repository (bd prefers them over the env)."""
    if not isinstance(config, Mapping):
        return []
    want = {
        "github.owner": projection.owner.lower(),
        "github.repo": projection.repo.lower(),
        "github.repository": projection.slug.lower(),
    }
    return [
        f"{key}={config[key]}"
        for key in _REPO_KEYS
        if config.get(key) and str(config[key]).strip().lower() != want[key]
    ]


def _issue_ref(projection: Projection) -> re.Pattern[str]:
    owner, repo = re.escape(projection.owner), re.escape(projection.repo)
    return re.compile(rf"^https?://[^/]+/{owner}/{repo}/issues/(\d+)$", re.IGNORECASE)


def verify_identity(rows: Any, projection: Projection) -> tuple[int, list[str]]:
    """``(projected, problems)`` for the public store's rows after a pull.

    Every row must carry ``external_ref`` = this repository's issue URL, and no URL may appear
    twice — a re-pull updates in place, so a duplicate means identity was lost."""
    if not isinstance(rows, list):
        return 0, ["could not list the public store after the pull"]
    pattern = _issue_ref(projection)
    seen: dict[str, str] = {}
    problems: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        bead, ref = str(row.get("id", "?")), str(row.get("external_ref") or "")
        if not pattern.match(ref):
            problems.append(
                f"{bead}: external_ref {ref or '<none>'} is not a {projection.slug} issue"
            )
            continue
        key = ref.lower()
        if key in seen:
            problems.append(f"{bead}: duplicates {seen[key]} for {ref}")
            continue
        seen[key] = bead
    return len(seen), problems


def project(
    entry: Mapping[str, Any],
    *,
    dry_run: bool = False,
    token_source: TokenSource | None = None,
    routes_factory: RoutesFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProjectionReport:
    """Pull ``entry``'s GitHub Issues into its public store (or preview with ``dry_run``).

    Raises :class:`ProjectionError` / ``StoreResolutionError`` for a refusal; a bd failure is
    raised too, with the credential scrubbed. Identity problems after a real pull are returned
    in :attr:`ProjectionReport.problems` (the caller exits non-zero)."""
    projection = resolve_projection(entry)
    target = projection.target.require_present()
    environ = dict(os.environ if environ is None else environ)
    token = (token_source or ambient_token)(environ)
    secrets = [token] if token else []
    routes = (routes_factory or _default_routes)(target, child_env(projection, token))

    conflicts = _repo_conflicts(routes.config_list(), projection)
    if conflicts:
        raise ProjectionError(
            f"{target.hive}: the public store's bd config names another repository "
            f"({', '.join(conflicts)}); expected {projection.slug} from the hive identity"
        )

    argv = bd_cli.package().github_pull_only_argv(dry_run=dry_run)
    refusal = guard.public_store_github_refusal(argv)
    if refusal is not None:  # pragma: no cover - the route is pinned; this is the backstop
        raise ProjectionError(refusal)

    res = routes.github_pull_only(dry_run=dry_run, timeout=PULL_TIMEOUT_SECONDS)
    if getattr(res, "returncode", 1) != 0:
        raise ProjectionError(
            f"{target.hive}: `bd github sync --pull-only` failed ({_tail(res, secrets)}); "
            "the internal store and routing were not touched"
        )
    out = _scrub(str(getattr(res, "stdout", "") or ""), secrets)
    report = ProjectionReport(
        hive=target.hive,
        store=target.scope,
        beads_dir=str(target.beads_dir),
        repository=projection.slug,
        dry_run=dry_run,
    )
    if dry_run:
        report.would_import = [int(n) for n in _WOULD_IMPORT.findall(out)]
        report.would_update = [int(n) for n in _WOULD_UPDATE.findall(out)]
        return report
    match = _PULLED.search(out)
    if match:
        report.pulled, report.created, report.updated = (int(g) for g in match.groups())
    report.projected, report.problems = verify_identity(
        routes.issue_list(all_=True, limit=0), projection
    )
    return report


def payload(report: ProjectionReport) -> dict[str, Any]:
    """The versioned ``--json`` document (``jsonout`` envelope)."""
    from . import jsonout

    return jsonout.envelope("hive project-issues", PROJECT_ISSUES_SCHEMA, report.to_json())


def render(report: ProjectionReport) -> list[str]:
    head = f"{report.hive}: GitHub {report.repository} -> public store {report.beads_dir}"
    if report.dry_run:
        return [
            head + " (dry run — nothing written)",
            f"  would import {len(report.would_import)} issue(s), "
            f"update {len(report.would_update)}",
        ]
    lines = [
        head,
        f"  pulled {report.pulled} issue(s): {report.created} created, "
        f"{report.updated} updated; {report.projected} projected in the store",
    ]
    lines += [f"  ✗ {problem}" for problem in report.problems]
    return lines


__all__ = [
    "PROJECT_ISSUES_SCHEMA",
    "SOURCE_GITHUB_ISSUES",
    "Projection",
    "ProjectionError",
    "ProjectionReport",
    "ambient_token",
    "child_env",
    "payload",
    "project",
    "render",
    "resolve_projection",
    "verify_identity",
]
