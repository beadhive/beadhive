"""Publish a PUBLIC tracker store to its Git Dolt replica — explicit and public-only (bh-lniwf.8).

docs/design/two-store-scope-adr.md section 8: once a repository is partitioned only an
explicitly selected public store may be published; the internal store is private even when the
code is public. :func:`publish_public` is the one sanctioned push of a public store's Dolt data,
and it is deliberately narrow:

* it resolves the PUBLIC store and nothing else (:func:`store_target.resolve_store` with scope
  ``public``): a legacy or internal-only hive raises ``StoreNotConfigured``, never a fallback;
* the destination is the store's own ``remote`` — ``git-origin`` means the code checkout's Git
  origin (its ``refs/dolt/data``), any other name its nested ``dolt-remote-descriptor/v1`` — and
  it must be a Git Dolt ref (the "public on a Git Dolt ref" contract) that does not address the
  internal store's descriptor;
* the destination is registered on the public store under its name through
  ``Engine.add_remote`` (idempotent; never re-points an existing name) and pushed through
  ``Engine.push_state`` with the store's ``BEADS_DIR`` — **never forced**. A destination that
  already carries another history (an unmigrated origin whose ``refs/dolt/data`` still holds the
  internal corpus) rejects the push as a non-fast-forward and keeps its data.

One guarded exception exists (bh-31rf7): :func:`publish_public` with ``lease=`` performs the
one-time REPLACEMENT of a destination ref that holds an unrelated (non-fast-forward) history.
It is off by default and compare-and-swap: it reads the destination's ``refs/dolt/data`` with
``git ls-remote`` and proceeds only when that SHA equals the operator-supplied expected old SHA
(force-with-lease semantics — never a bare ``--force``), the confirmation was given, and the
internal store no longer uses the code origin as its remote. It moves ONLY ``refs/dolt/data``:
branches and tags (Dolt's own ``refs/heads/__dolt_remote_info__`` marker aside, which Dolt
rewrites with the data) are read before and after and the replacement reports a failure if any
moved, and the old data is never parked under another ref.

Nothing here runs implicitly: only ``bh hive remote sync --store <tracker>`` (tracker_sync)
calls it, so a hive that never asks to sync never publishes. It never reads or writes the internal
store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import dolt_remote, store_bootstrap, store_target
from .modules.config.contracts import GIT_ORIGIN_REMOTE
from .run import run
from .store_target import SCOPE_PUBLIC, StoreTarget

#: ``--json`` contract version of the push leg of ``bh hive remote sync`` (see ``jsonout``).
PUBLISH_PUBLIC_SCHEMA = 1


class PublishError(RuntimeError):
    """The publication refused, or the push failed; the internal store was not touched."""


@dataclass(frozen=True)
class PublishPlan:
    target: StoreTarget
    remote: str
    url: str

    @property
    def redacted(self) -> str:
        return dolt_remote.redact_url(self.url)


DOLT_DATA_REF = "refs/dolt/data"
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ROLLBACK_RULE = (
    "rollback: NEVER restore the old SHA -- {ref} held the private corpus, and re-pushing it "
    "(even with a lease) republishes it. Recover from the verified private archive instead "
    "(re-run the replace from the audited tracker store), or remove the ref; nothing is parked "
    "under another ref"
)


@dataclass(frozen=True)
class ReplacePlan:
    """What a guarded replacement will do (and did), named exactly for the operator."""

    ref: str
    old_sha: str  # the destination's current value
    lease_sha: str  # the operator-supplied compare-and-swap value ("" = not supplied)
    new_store: str  # the public store identity that becomes the ref's content
    code_refs: tuple[tuple[str, str], ...]  # (ref, sha) of every branch/tag; must not change
    rollback: str

    @property
    def matches(self) -> bool:
        return bool(self.lease_sha) and self.lease_sha == self.old_sha


@dataclass(frozen=True)
class PublishReport:
    hive: str
    remote: str
    destination: str  # redacted
    dry_run: bool
    pushed: bool
    replace: ReplacePlan | None = None
    new_sha: str = ""
    remote_sha: str = ""  # dry run only: the destination's current refs/dolt/data


def dolt_git_url(git_url: str) -> str:
    """The Dolt Git-remote URL (``git+<transport>://…``) for a Git remote URL or path."""
    raw = (git_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("git+"):
        return raw
    if "://" in raw:
        return f"git+{raw}"
    if ":" in raw and not raw.startswith(("/", ".")):  # scp form: user@host:owner/repo.git
        user_host, _, path = raw.partition(":")
        return f"git+ssh://{user_host}/{path.lstrip('/')}"
    return f"git+file://{Path(raw).expanduser().resolve()}"


def _origin_url(target: StoreTarget) -> str:
    res = run(
        ["git", "-C", str(target.repo_dir), "remote", "get-url", "origin"],
        check=False,
        capture=True,
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def plan_publication(entry: Mapping[str, Any], *, origin_url: str | None = None) -> PublishPlan:
    """The public store and the Git Dolt ref it publishes to, or a refusal naming why not."""
    target = store_target.resolve_store(entry, SCOPE_PUBLIC)
    if target.scope != SCOPE_PUBLIC or target.operational:  # defence in depth
        raise PublishError(f"{target.hive}: refusing to publish the {target.scope} store")
    if not target.remote:
        raise PublishError(f"{target.hive}: the public store names no remote; nothing to publish")
    if target.remote == GIT_ORIGIN_REMOTE:
        raw = _origin_url(target) if origin_url is None else origin_url
        if not raw:
            raise PublishError(
                f"{target.hive}: the public store publishes to git-origin, but the checkout "
                f"{target.repo_dir} has no Git origin"
            )
        url = dolt_git_url(raw)
    else:
        try:
            url = store_bootstrap.entry_remote_url(entry, target.remote)
        except store_bootstrap.RemoteUnresolved as exc:
            raise PublishError(f"{target.hive}: {str(exc).split('] ', 1)[-1]}") from None
    plan = PublishPlan(target=target, remote=target.remote, url=url)
    classified = dolt_remote.classify(url)
    if classified.family != dolt_remote.RemoteFamily.GIT or not classified.supported:
        raise PublishError(
            f"{target.hive}: the public store publishes only to a Git Dolt ref; "
            f"{plan.redacted} is {classified.family.value}"
        )
    internal = store_target.operational_store(entry)
    if internal.remote:
        try:
            internal_url = store_bootstrap.entry_remote_url(entry, internal.remote)
        except store_bootstrap.RemoteUnresolved:
            internal_url = ""
        if internal_url and dolt_remote.same_remote(internal_url, url):
            raise PublishError(
                f"{target.hive}: {plan.redacted} is the internal store's remote; the public "
                "store never publishes into it"
            )
    return plan


def publish_public(
    entry: Mapping[str, Any],
    *,
    dry_run: bool = False,
    eng=None,
    origin_url: str | None = None,
    lease: str = "",
    confirmed: bool = False,
) -> PublishReport:
    """Push ``entry``'s PUBLIC store to its Git Dolt ref (or only plan it with ``dry_run``).

    Raises :class:`PublishError` / ``StoreResolutionError`` for a refusal or a failed push. A
    refusal happens before anything is registered or pushed.

    A non-empty ``lease`` (the expected old SHA) selects the guarded compare-and-swap
    replacement of the destination's ``refs/dolt/data`` (see the module docstring); a real
    replacement also needs ``confirmed``. A plain ``dry_run`` additionally reports the
    destination ref's current SHA (best effort) so the operator can supply the lease."""
    plan = plan_publication(entry, origin_url=origin_url)
    target = plan.target.require_present()
    store_env = target.env({})  # BEADS_DIR for the detached store
    target.env()  # refuse an inherited BEADS_DIR naming another store
    if lease:
        return _replace_ref(
            entry,
            plan,
            store_env,
            dry_run=dry_run,
            eng=eng,
            lease=lease,
            confirmed=confirmed,
        )
    if confirmed:
        raise PublishError(
            f"{target.hive}: --confirm only applies with --force-with-lease; the default "
            "push is fast-forward-only"
        )
    report = PublishReport(
        hive=target.hive,
        remote=plan.remote,
        destination=plan.redacted,
        dry_run=dry_run,
        pushed=False,
    )
    if dry_run:
        try:
            current = _ls_remote(plan).get(DOLT_DATA_REF, "")
        except PublishError:
            current = ""
        return replace(report, remote_sha=current)
    from . import engine  # lazy: keep this module import-light

    eng = eng or engine.get_engine()
    root = target.store_root
    added = eng.add_remote(root, plan.remote, plan.url, extra_env=store_env)
    if getattr(added, "returncode", 1) != 0:
        raise PublishError(
            f"{target.hive}: could not register {plan.remote} ({plan.redacted}) on the public "
            f"store: {_tail(added, plan)}"
        )
    pushed = eng.push_state(
        root,
        message="publish public projection",
        remote=plan.remote,
        extra_env=store_env,
    )
    if getattr(pushed, "returncode", 1) != 0:
        raise PublishError(
            f"{target.hive}: publishing the public store to {plan.remote} ({plan.redacted}) "
            f"failed ({_tail(pushed, plan)}); nothing was forced and the internal store was "
            "not touched"
        )
    return PublishReport(
        hive=target.hive,
        remote=plan.remote,
        destination=plan.redacted,
        dry_run=False,
        pushed=True,
    )


def _git_url(plan: PublishPlan) -> str:
    return plan.url[len("git+") :] if plan.url.startswith("git+") else plan.url


def _ls_remote(plan: PublishPlan) -> dict[str, str]:
    """Every ref the destination advertises, ``{ref: sha}`` (read-only)."""
    res = run(["git", "ls-remote", _git_url(plan)], check=False, capture=True)
    if res.returncode != 0:
        raise PublishError(
            f"{plan.target.hive}: could not read the refs of {plan.redacted}: {_tail(res, plan)}"
        )
    refs: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        sha, _, ref = line.strip().partition("\t")
        if sha and ref and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


#: The branch Dolt's own Git-remote blobstore keeps beside ``refs/dolt/data`` (one commit holding
#: ``DOLT_REMOTE.md``). Dolt rewrites it on a forced push (a new commit with the same content,
#: found by the real-bd proof bh-rgd4a), so it is Dolt's data, not a code branch to protect.
DOLT_OWNED_REFS = frozenset({"refs/heads/__dolt_remote_info__"})


def _code_refs(refs: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (r, s)
            for r, s in refs.items()
            if r.startswith(("refs/heads/", "refs/tags/")) and r not in DOLT_OWNED_REFS
        )
    )


def _internal_uses_code_origin(entry: Mapping[str, Any], plan: PublishPlan) -> str:
    """Why the internal store still depends on the code origin ('' when it does not)."""
    internal = store_target.operational_store(entry)
    if internal.remote == GIT_ORIGIN_REMOTE:
        return f"the internal store's remote is {GIT_ORIGIN_REMOTE} (the code origin)"
    if internal.remote:
        try:
            url = store_bootstrap.entry_remote_url(entry, internal.remote)
        except store_bootstrap.RemoteUnresolved:
            return ""
        if url and dolt_remote.same_remote(url, plan.url):
            return f"the internal store's remote {internal.remote} is {plan.redacted}"
    return ""


def _replace_ref(
    entry: Mapping[str, Any],
    plan: PublishPlan,
    store_env: Mapping[str, str],
    *,
    dry_run: bool,
    eng,
    lease: str,
    confirmed: bool,
) -> PublishReport:
    target = plan.target
    lease = (lease or "").strip().lower()
    if lease and not _SHA_RE.match(lease):
        raise PublishError(
            f"{target.hive}: --force-with-lease must be the full 40- or 64-hex SHA "
            f"of {DOLT_DATA_REF}"
        )
    why = _internal_uses_code_origin(entry, plan)
    if why:
        raise PublishError(
            f"{target.hive}: refusing to replace {DOLT_DATA_REF}: {why}; move the internal store "
            "to its private remote first so the replacement cannot strand it"
        )
    before = _ls_remote(plan)
    old = before.get(DOLT_DATA_REF, "")
    if not old:
        raise PublishError(
            f"{target.hive}: {DOLT_DATA_REF} does not exist on {plan.redacted}; there is nothing "
            "to replace -- sync without --force-with-lease"
        )
    replacement = ReplacePlan(
        ref=DOLT_DATA_REF,
        old_sha=old,
        lease_sha=lease,
        new_store=f"{target.hive} public store {target.prefix} at {target.store_root}",
        code_refs=_code_refs(before),
        rollback=ROLLBACK_RULE.format(ref=DOLT_DATA_REF),
    )
    report = PublishReport(
        hive=target.hive,
        remote=plan.remote,
        destination=plan.redacted,
        dry_run=dry_run,
        pushed=False,
        replace=replacement,
    )
    if dry_run:
        return report
    if not lease:
        raise PublishError(
            f"{target.hive}: replacing needs --force-with-lease <sha>; {DOLT_DATA_REF} on "
            f"{plan.redacted} is {old} (run with --dry-run to review)"
        )
    if not replacement.matches:
        raise PublishError(
            f"{target.hive}: compare-and-swap refused: {DOLT_DATA_REF} on {plan.redacted} is "
            f"{old}, not the expected {lease}; nothing was changed"
        )
    if not confirmed:
        raise PublishError(
            f"{target.hive}: replacing {DOLT_DATA_REF} is destructive; re-run with --confirm "
            "after reviewing --dry-run"
        )
    from . import engine  # lazy: keep this module import-light

    eng = eng or engine.get_engine()
    root = target.store_root
    added = eng.add_remote(root, plan.remote, plan.url, extra_env=store_env)
    if getattr(added, "returncode", 1) != 0:
        raise PublishError(
            f"{target.hive}: could not register {plan.remote} ({plan.redacted}) on the public "
            f"store: {_tail(added, plan)}"
        )
    # Lease check as late as possible: re-read immediately before the one forced push.
    current = _ls_remote(plan).get(DOLT_DATA_REF, "")
    if current != lease:
        raise PublishError(
            f"{target.hive}: compare-and-swap refused: {DOLT_DATA_REF} moved to {current or '-'} "
            f"(expected {lease}); nothing was changed"
        )
    pushed = eng.push_state(
        root,
        message="replace public projection",
        remote=plan.remote,
        force=True,
        extra_env=store_env,
    )
    if getattr(pushed, "returncode", 1) != 0:
        raise PublishError(
            f"{target.hive}: replacing {DOLT_DATA_REF} on {plan.redacted} failed "
            f"({_tail(pushed, plan)}); the internal store was not touched"
        )
    after = _ls_remote(plan)
    if _code_refs(after) != replacement.code_refs:
        raise PublishError(
            f"{target.hive}: UNEXPECTED: branches or tags on {plan.redacted} changed during the "
            f"replacement; inspect immediately. {replacement.rollback}"
        )
    return PublishReport(
        hive=target.hive,
        remote=plan.remote,
        destination=plan.redacted,
        dry_run=False,
        pushed=True,
        replace=replacement,
        new_sha=after.get(DOLT_DATA_REF, ""),
    )


def render_report(report: PublishReport) -> list[str]:
    """Human lines for the push leg of ``bh hive remote sync``."""
    rep = report.replace
    head = f"{report.hive}: public store -> {report.destination} ({report.remote})"
    if rep is None:
        if report.dry_run:
            lines = [head + " (dry run -- nothing pushed; fast-forward only)"]
            if report.remote_sha:
                lines.append(f"  {DOLT_DATA_REF} currently {report.remote_sha}")
            return lines
        return [head + " published (fast-forward only, never forced)"]
    lines = [
        head + (" REPLACE (dry run -- nothing written)" if report.dry_run else " REPLACED"),
        f"  ref:          {rep.ref}",
        f"  old SHA:      {rep.old_sha}"
        + (
            ""
            if not rep.lease_sha
            else (
                " (matches --force-with-lease)"
                if rep.matches
                else " (DIFFERS from --force-with-lease)"
            )
        ),
        f"  new content:  {rep.new_store}",
        f"  must not change: {len(rep.code_refs)} branch/tag ref(s)",
    ]
    lines += [f"    {ref} {sha}" for ref, sha in rep.code_refs]
    lines.append(f"  {rep.rollback}")
    if report.new_sha:
        lines.append(f"  new SHA:      {report.new_sha}")
    return lines


def payload(report: PublishReport) -> dict[str, Any]:
    """The versioned ``--json`` document (``jsonout`` envelope)."""
    from . import jsonout

    body: dict[str, Any] = {
        "hive": report.hive,
        "remote": report.remote,
        "destination": report.destination,
        "dry_run": report.dry_run,
        "pushed": report.pushed,
        "mode": "replace" if report.replace else "fast-forward",
        "remote_sha": report.remote_sha,
    }
    if report.replace:
        rep = report.replace
        body["replace"] = {
            "ref": rep.ref,
            "old_sha": rep.old_sha,
            "lease_sha": rep.lease_sha,
            "cas_match": rep.matches,
            "new_store": rep.new_store,
            "code_refs": [{"ref": r, "sha": s} for r, s in rep.code_refs],
            "rollback": rep.rollback,
            "new_sha": report.new_sha,
        }
    return jsonout.envelope("hive remote sync", PUBLISH_PUBLIC_SCHEMA, body)


def _tail(res: Any, plan: PublishPlan) -> str:
    for name in ("stderr", "stdout"):
        raw = str(getattr(res, name, "") or "").replace(plan.url, plan.redacted)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"exit {getattr(res, 'returncode', '?')}"


__all__ = [
    "DOLT_DATA_REF",
    "DOLT_OWNED_REFS",
    "PUBLISH_PUBLIC_SCHEMA",
    "ROLLBACK_RULE",
    "PublishError",
    "ReplacePlan",
    "payload",
    "render_report",
    "PublishPlan",
    "PublishReport",
    "dolt_git_url",
    "plan_publication",
    "publish_public",
]
