"""Reconcile a hive's Dolt remote registration with its HQ descriptor (bh-b1scz.5).

Compares HQ desire (``managed_repos[].dolt_remote``) with bd-managed reality (``bd dolt remote
list`` through the Engine seam) and, outside ``--dry-run``, installs a MISSING registration.
That is the only mutation: a conflicting registration is ``migration-required`` and left alone,
nothing is ever removed or re-pointed, and no data is pushed or pulled
(docs/design/hq-dolt-remote-descriptor-adr.md, "Reconciliation never moves data").

Every rendered URL goes through ``dolt_remote.redact_url``; credentials are resolved before the
one mutation and handed to that bd child only. Remote state is never inferred from Git refs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from . import dolt_remote, dolt_remote_credentials, engine, registry, store_locator
from .dolt_remote import RemoteFamily, RemoteOp
from .log import get_logger
from .modules.config.contracts import DoltRemoteDescriptor

SCHEMA = "bh.hive-remote-reconcile/1"


class Outcome(StrEnum):
    NO_DESCRIPTOR = "no-descriptor"
    NO_LOCAL_STORE = "no-local-store"
    IN_SYNC = "in-sync"
    WOULD_ADD = "would-add"
    ADDED = "added"
    MIGRATION_REQUIRED = "migration-required"
    NAME_MISMATCH = "name-mismatch"
    UNSUPPORTED = "unsupported"
    CREDENTIALS_MISSING = "credentials-missing"
    ERROR = "error"


#: Outcomes an operator must act on; any of them makes the run exit non-zero.
FAILING = frozenset(
    {
        Outcome.MIGRATION_REQUIRED,
        Outcome.NAME_MISMATCH,
        Outcome.UNSUPPORTED,
        Outcome.CREDENTIALS_MISSING,
        Outcome.ERROR,
    }
)

FENCE_CAVEAT = (
    "the host epoch fence stays on the Git origin; for this data plane it is a sequenced, "
    "advisory check, not an atomic guarantee (bh-tfapu)"
)
HEALTH_CAVEAT = "ahead/behind is unknown: this remote family has no read-only diff"


@dataclass
class HiveRemoteReport:
    """One hive's comparison. Every URL field is already redacted."""

    hive: str
    outcome: Outcome
    remote: str = ""
    desired: str = ""
    actual: str = ""
    family: str = ""
    support: str = ""
    mutation: str = ""
    detail: str = ""
    credential_source: str = ""
    caveats: list[str] = field(default_factory=list)

    @property
    def failing(self) -> bool:
        return self.outcome in FAILING

    def to_json(self) -> dict:
        data = asdict(self)
        data["outcome"] = self.outcome.value
        return data


def _hive_label(entry) -> str:
    return f"{entry.get('provider', '')}/{entry.get('org', '')}/{entry.get('repo', '')}"


def _scrub(text: str, url: str) -> str:
    redacted = dolt_remote.redact_url(url)
    return (text or "").replace(url, redacted).strip()


def _leased(cfg, entry) -> bool:
    try:
        from . import guard

        return guard.primary_state(cfg=cfg, entry=entry) is not None
    except Exception:  # noqa: BLE001 - a caveat probe must never fail the comparison
        return False


def reconcile_hive(
    entry,
    cfg,
    *,
    dry_run: bool,
    eng: engine.Engine | None = None,
    environ=None,
    home: Path | None = None,
    broker=None,
    hive_dir: Path | None = None,
) -> HiveRemoteReport:
    """Compare and (outside ``dry_run``) install one hive's missing registration."""

    label = _hive_label(entry)
    raw = entry.get("dolt_remote")
    if not raw:
        return HiveRemoteReport(label, Outcome.NO_DESCRIPTOR, detail="no HQ remote policy")
    descriptor = DoltRemoteDescriptor(**dict(raw))
    url, name = descriptor.url, descriptor.name
    hive_dir = Path(hive_dir) if hive_dir is not None else registry.hive_dir(entry)
    base = dolt_remote.classify(url)
    report = HiveRemoteReport(
        label,
        Outcome.IN_SYNC,
        remote=name,
        desired=base.redacted,
        family=base.family.value,
        support=base.capabilities.support.value,
    )
    if base.family != RemoteFamily.GIT:
        report.caveats.append(HEALTH_CAVEAT)
        if _leased(cfg, entry):
            report.caveats.append(FENCE_CAVEAT)
    if not store_locator.embedded_store_dir(hive_dir).parent.is_dir():  # no .beads/
        report.outcome = Outcome.NO_LOCAL_STORE
        report.detail = "no local beads store on this host; onboarding applies the descriptor"
        return report
    target = dolt_remote.for_store_mode(base, embedded=store_locator.is_embedded_mode(hive_dir))
    report.support = target.capabilities.support.value
    eng = eng or engine.get_engine(cfg)
    listing = eng.list_remotes(hive_dir)
    if not listing.ok:
        report.outcome = Outcome.ERROR
        report.detail = f"cannot read the registered remotes ({_scrub(listing.error, url)})"
        return report
    actual = listing.get(name)
    if actual is not None:
        report.actual = dolt_remote.redact_url(actual.url)
        if dolt_remote.same_remote(actual.url, url):
            report.detail = "registration matches HQ"
            return report
        report.outcome = Outcome.MIGRATION_REQUIRED
        report.detail = (
            f"remote {name!r} points elsewhere; moving a corpus is a migration (bh-ksmt0), "
            "nothing was replaced"
        )
        return report
    alias = next((r for r in listing.remotes if dolt_remote.same_remote(r.url, url)), None)
    if alias is not None:
        report.outcome = Outcome.NAME_MISMATCH
        report.actual = dolt_remote.redact_url(alias.url)
        report.detail = f"the HQ URL is registered as {alias.name!r}, not {name!r}; not renamed"
        return report
    if not target.allows(RemoteOp.ADD):
        report.outcome = Outcome.UNSUPPORTED
        report.detail = target.error or target.capabilities.note
        return report
    creds = dolt_remote_credentials.resolve(
        target, descriptor.credentials, cfg, environ=environ, home=home, broker=broker
    )
    report.credential_source = creds.source
    if not creds.ok:
        report.outcome = Outcome.CREDENTIALS_MISSING
        report.detail = creds.missing
        return report
    report.mutation = f"bd dolt remote add {name} {target.redacted}"
    if dry_run:
        report.outcome = Outcome.WOULD_ADD
        report.detail = "registration missing"
        return report
    result = eng.add_remote(hive_dir, name, url, extra_env=dict(creds.env))
    if result.returncode:
        report.outcome = Outcome.ERROR
        tail = (getattr(result, "stderr", "") or "").strip().splitlines()
        report.detail = _scrub(tail[-1] if tail else f"exit {result.returncode}", url)
        return report
    report.outcome = Outcome.ADDED
    report.detail = "registration installed; no data was transferred"
    return report


def _targets(cfg, hive_ids: Iterable[str] | None) -> list:
    if not hive_ids:
        return list(registry.hives(cfg))
    entries = []
    real = registry.hives(cfg)
    for hive_id in hive_ids:
        entry = registry.resolve_hive(cfg, hive_id)
        if entry not in real:
            raise ValueError("HQ is not a hive; it has no HQ remote descriptor")
        entries.append(entry)
    return entries


def reconcile(
    hive_ids: Iterable[str] | None = None,
    *,
    dry_run: bool = False,
    cfg,
    reconcile_one: Callable | None = None,
) -> list[HiveRemoteReport]:
    """Reconcile the named hives (or every registered hive) of the loaded ``cfg``, isolating
    each hive's failure so the summary is always complete."""

    reconcile_one = reconcile_one or reconcile_hive
    log = get_logger(__name__)
    reports = []
    for entry in _targets(cfg, hive_ids):
        try:
            report = reconcile_one(entry, cfg, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - fleet mode isolates every hive's failure
            report = HiveRemoteReport(
                _hive_label(entry),
                Outcome.ERROR,
                detail=f"{type(exc).__name__} while reconciling (details withheld)",
            )
        log.info(
            "hive_remote_reconcile",
            hive=report.hive,
            outcome=report.outcome.value,
            remote=report.remote,
            desired=report.desired,
            actual=report.actual,
            dry_run=dry_run,
        )
        reports.append(report)
    return reports


def render(
    reports: list[HiveRemoteReport], *, dry_run: bool, title: str = "reconcile"
) -> list[str]:
    lines = [f"{'DRY-RUN ' if dry_run and title == 'reconcile' else ''}hive remote {title}"]
    for report in reports:
        mark = "✗" if report.failing else "✓"
        lines.append(f"  {mark} {report.hive}: {report.outcome.value}")
        if report.desired:
            lines.append(f"    - HQ: {report.remote} → {report.desired} ({report.family})")
        if report.actual:
            lines.append(f"    - registered: {report.actual}")
        if report.mutation:
            verb = "would run" if report.outcome == Outcome.WOULD_ADD else "ran"
            lines.append(f"    - {verb}: {report.mutation}")
        if report.detail:
            lines.append(f"    - {report.detail}")
        if report.credential_source:
            lines.append(f"    - credentials: {report.credential_source}")
        lines.extend(f"    - note: {caveat}" for caveat in report.caveats)
    failing = [r for r in reports if r.failing]
    lines.append(f"\n# {len(reports)} hive(s) compared, {len(failing)} need attention")
    return lines


def to_json(reports: list[HiveRemoteReport], *, dry_run: bool) -> dict:
    return {
        "schema": SCHEMA,
        "dry_run": dry_run,
        "ok": not any(r.failing for r in reports),
        "hives": [r.to_json() for r in reports],
    }
