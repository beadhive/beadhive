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
* reports the rows the pull left in the store's Dolt working set (``bd diff HEAD WORKING``) —
  uncommitted, so an audit before publish must cover them — and, only when the opt-in
  ``tracker_sync.commit_after_pull`` is on, commits them (bh-cm4er);
* only when the opt-in ``tracker_sync.overlap_repull`` is on, re-pulls the issues the tracker
  port reports since bh's own inclusive checkpoint, covering edits bd's end-of-sync
  ``last_sync`` skips (:mod:`beadhive.github_repull`, bh-clqa5);
* verifies identity afterwards: every projected row carries ``external_ref`` = the issue's
  GitHub URL for THIS repository, and no URL appears twice (a re-pull updates in place).

Nothing here writes to GitHub, touches the internal store, or changes routing. Mapper limits
(comments, milestones, deletions, label vocabulary) are documented in
docs/design/github-issue-projection.md.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from beadhive_plugins.credentials import ResolvedToken, TokenSource, scrub

from . import bd_cli, guard, store_target
from .github_repull import (
    OverlapRepull,
    RepullReport,
    batches,
    first_run_floor,
    list_changed,
    read_checkpoint,
    write_checkpoint,
)
from .store_target import SCOPE_PUBLIC, StoreTarget

#: The only ``stores.public.source`` this projection serves.
SOURCE_GITHUB_ISSUES = "github-issues"
#: ``--json`` contract version for ``bh hive project-issues`` (see ``jsonout``).
PROJECT_ISSUES_SCHEMA = 1
#: A pull pages through every issue; bd's default 120s bound is too tight for a large tracker.
PULL_TIMEOUT_SECONDS = 900.0

_REPO_KEYS = ("github.owner", "github.repo", "github.repository")
_PULLED = re.compile(r"Pulled (\d+) issues \((\d+) created, (\d+) updated\)")
#: What bd 1.3.0 prints (exit 0) for ``bd diff HEAD WORKING --json`` on a clean working set.
_NO_WORKING_CHANGES = "No changes between HEAD and WORKING"
_WOULD_IMPORT = re.compile(r"\[dry-run\] Would import: (\d+) - ")
_WOULD_UPDATE = re.compile(r"\[dry-run\] Would update local issue: (\d+) - ")

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
    #: Rows in the store's Dolt working set that HEAD does not hold, after the pull (and after the
    #: opt-in commit). 0 = clean; None = could not be read (always accompanied by a warning).
    uncommitted: int | None = 0
    #: True when ``tracker_sync.commit_after_pull`` committed the pulled rows.
    committed: bool = False
    #: The ``tracker_sync.overlap_repull`` leg; None when it is off (or on a dry run).
    repull: RepullReport | None = None
    #: Visible, non-fatal findings (an uncommitted working set); never silent, never an exit code.
    warnings: list[str] = field(default_factory=list)
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


def ambient_token(environ: Mapping[str, str]) -> ResolvedToken | None:
    """The ambient GitHub credential, with provenance, from bh's one chain
    (:func:`run.resolve_github_token`): ``GITHUB_TOKEN``, then ``GH_TOKEN``, then a fresh
    ``gh auth token`` — never persisted, never logged.

    None when none is available — bd may still find a host-local ``github.token`` in the store's
    own config.yaml. The value is never stored or printed by bh."""
    from .run import resolve_github_token  # lazy: keep import-light

    return resolve_github_token(environ)


def child_env(projection: Projection, token: str | None) -> dict[str, str]:
    """Variables added to the bd child ONLY: the repository, and the credential if resolved."""
    env = {"GITHUB_OWNER": projection.owner, "GITHUB_REPO": projection.repo}
    if token:
        env["GITHUB_TOKEN"] = token
    return env


def _default_routes(target: StoreTarget, extra_env: Mapping[str, str]) -> Any:
    return store_target.routes(target, extra_env=extra_env)


def _tail(res: Any, secrets: list[str]) -> str:
    """bd's last diagnostic line — stderr first, then stdout — with the credential scrubbed."""
    for name in ("stderr", "stdout"):
        raw = scrub(str(getattr(res, name, "") or ""), secrets)
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


def commit_after_pull_enabled(cfg: Mapping[str, Any] | None) -> bool:
    """``tracker_sync.commit_after_pull`` of a loaded config (default False, so absent = off).
    :func:`project` never reads config itself: its callers pass this in."""
    section = (cfg or {}).get("tracker_sync") or {}
    return isinstance(section, Mapping) and section.get("commit_after_pull") is True


def uncommitted_rows(routes: Any) -> int | None:
    """How many rows the store's Dolt working set holds that HEAD does not (``bd diff HEAD
    WORKING --json``); None when bd could not answer.

    Recorded bd 1.3.0: dirty is a JSON list of ``{IssueID, DiffType, OldValue, NewValue}``
    entries; clean is exit 0 with the plain text ``No changes between HEAD and WORKING`` (not
    ``[]``), which counts as 0, as does empty output."""
    res = routes.working_set_diff()
    if getattr(res, "returncode", 1) != 0:
        return None
    out = str(getattr(res, "stdout", "") or "").strip()
    if not out or out == _NO_WORKING_CHANGES:
        return 0
    try:
        rows = json.loads(out)
    except ValueError:
        return None
    return len(rows) if isinstance(rows, list) else None


def _warn_uncommitted(report: ProjectionReport) -> None:
    if report.uncommitted is None:
        report.warnings.append(
            "could not read the store's uncommitted rows (`bd diff HEAD WORKING`); audit the "
            "working set before publishing"
        )
    elif report.uncommitted:
        report.warnings.append(
            f"{report.uncommitted} row(s) from the pull are uncommitted in the store working "
            "set (Dolt HEAD does not hold them); commit before an audit or publish, or set "
            "tracker_sync.commit_after_pull"
        )


def _settle_working_set(
    routes: Any, report: ProjectionReport, projection: Projection, *, commit: bool
) -> None:
    """Count the pulled rows left uncommitted; with ``commit`` commit them first. A refused or
    failed commit is a problem (the operator asked for it); a count is only ever a warning."""
    report.uncommitted = uncommitted_rows(routes)
    if commit and report.uncommitted != 0:
        message = (
            f"bh: pull {report.pulled} GitHub issue(s) from {projection.slug} "
            f"({report.created} created, {report.updated} updated)"
        )
        res = routes.dolt_commit(message)
        if getattr(res, "returncode", 1) != 0:
            report.problems.append(
                f"tracker_sync.commit_after_pull: `bd dolt commit` failed ({_tail(res, [])})"
            )
        else:
            report.uncommitted = uncommitted_rows(routes)
            report.committed = report.uncommitted == 0
    _warn_uncommitted(report)


def project(
    entry: Mapping[str, Any],
    *,
    dry_run: bool = False,
    commit_after_pull: bool = False,
    overlap_repull: OverlapRepull | None = None,
    token_source: TokenSource | None = None,
    routes_factory: RoutesFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProjectionReport:
    """Pull ``entry``'s GitHub Issues into its public store (or preview with ``dry_run``).

    Raises :class:`ProjectionError` / ``StoreResolutionError`` for a refusal; a bd failure is
    raised too, with the credential scrubbed. Identity problems after a real pull are returned
    in :attr:`ProjectionReport.problems` (the caller exits non-zero), as is a failed opt-in
    ``overlap_repull`` leg (:func:`beadhive.github_repull.for_config` builds it; None = off)."""
    projection = resolve_projection(entry)
    target = projection.target.require_present()
    environ = dict(os.environ if environ is None else environ)
    resolved = token_source() if token_source else ambient_token(environ)
    token = resolved.value if resolved else None
    secrets = [token] if token else []
    routes = (routes_factory or _default_routes)(target, child_env(projection, token))

    store_config = routes.config_list()
    conflicts = _repo_conflicts(store_config, projection)
    if conflicts:
        raise ProjectionError(
            f"{target.hive}: the public store's bd config names another repository "
            f"({', '.join(conflicts)}); expected {projection.slug} from the hive identity"
        )

    argv = bd_cli.package().github_pull_only_argv(dry_run=dry_run)
    refusal = guard.public_store_github_refusal(argv)
    if refusal is not None:  # pragma: no cover - the route is pinned; this is the backstop
        raise ProjectionError(refusal)

    overlap = None if dry_run else overlap_repull
    started = overlap.clock() if overlap else None  # before bd's fetch begins
    res = routes.github_pull_only(dry_run=dry_run, timeout=PULL_TIMEOUT_SECONDS)
    if getattr(res, "returncode", 1) != 0:
        raise ProjectionError(
            f"{target.hive}: `bd github sync --pull-only` failed ({_tail(res, secrets)}); "
            "the internal store and routing were not touched"
        )
    out = scrub(str(getattr(res, "stdout", "") or ""), secrets)
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
    mark = None
    if overlap:
        mark = _overlap_repull(routes, report, projection, overlap, started, secrets)
    report.projected, problems = verify_identity(routes.issue_list(all_=True, limit=0), projection)
    report.problems += problems
    _settle_working_set(routes, report, projection, commit=commit_after_pull and report.ok)
    if overlap and mark and report.ok and report.repull is not None:
        write_checkpoint(overlap.checkpoint_path, projection.slug, mark)
        report.repull.checkpoint_saved = True
    return report


def _overlap_repull(
    routes: Any,
    report: ProjectionReport,
    projection: Projection,
    overlap: OverlapRepull,
    started: Any,
    secrets: list[str],
) -> str | None:
    """Re-pull every issue the tracker reports at or after bh's saved checkpoint (first run: every
    issue updated since shortly before ``started``); return the checkpoint to save once the whole
    projection succeeded, or None after a failure (recorded as a problem, so the next run retries
    from the old mark). See :mod:`beadhive.github_repull`."""
    token = read_checkpoint(overlap.checkpoint_path, projection.slug)
    report.repull = leg = RepullReport(from_checkpoint=token is not None)
    floor = None if token else first_run_floor(started)
    try:
        numbers, mark, warnings = list_changed(overlap.reader, token, _issue_ref(projection), floor)
    except Exception as exc:  # noqa: BLE001 - adapter refusals are typed; never echo their text
        report.problems.append(
            f"tracker_sync.overlap_repull: listing {projection.slug} through the tracker port "
            f"failed ({type(exc).__name__}); the checkpoint was not advanced"
        )
        return None
    leg.listed = len(numbers)
    report.warnings += warnings
    for batch in batches(numbers):
        res = routes.github_repull(batch, timeout=PULL_TIMEOUT_SECONDS)
        if getattr(res, "returncode", 1) != 0:
            report.problems.append(
                f"tracker_sync.overlap_repull: `bd github sync --pull-only --issues=` failed "
                f"({_tail(res, secrets)}); the checkpoint was not advanced"
            )
            return None
        leg.repulled += batch
        pulled = _PULLED.search(scrub(str(getattr(res, "stdout", "") or ""), secrets))
        if pulled:
            leg.created += int(pulled.group(2))
            leg.updated += int(pulled.group(3))
    return mark


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
    if report.repull is not None:
        leg = report.repull
        since = "since the saved checkpoint" if leg.from_checkpoint else "first run, no checkpoint"
        lines.append(
            f"  overlap re-pull ({since}): {len(leg.repulled)} issue(s) re-pulled, "
            f"{leg.created} created, {leg.updated} updated"
            + ("; checkpoint saved" if leg.checkpoint_saved else "; checkpoint NOT advanced")
        )
    if report.committed:
        lines.append("  committed the pulled rows (tracker_sync.commit_after_pull)")
    lines += [f"  warning: {warning}" for warning in report.warnings]
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
    "commit_after_pull_enabled",
    "payload",
    "project",
    "render",
    "resolve_projection",
    "uncommitted_rows",
    "verify_identity",
]
