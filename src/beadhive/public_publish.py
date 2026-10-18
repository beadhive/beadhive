"""Publish a PUBLIC projection store to its Git Dolt ref — explicit and public-only (bh-lniwf.8).

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

Nothing here runs implicitly: no verb, sync or hook calls it, so a hive that never asks to
publish never publishes. It never reads or writes the internal store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dolt_remote, store_bootstrap, store_target
from .modules.config.contracts import GIT_ORIGIN_REMOTE
from .run import run
from .store_target import SCOPE_PUBLIC, StoreTarget


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


@dataclass(frozen=True)
class PublishReport:
    hive: str
    remote: str
    destination: str  # redacted
    dry_run: bool
    pushed: bool


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
) -> PublishReport:
    """Push ``entry``'s PUBLIC store to its Git Dolt ref (or only plan it with ``dry_run``).

    Raises :class:`PublishError` / ``StoreResolutionError`` for a refusal or a failed push. A
    refusal happens before anything is registered or pushed."""
    plan = plan_publication(entry, origin_url=origin_url)
    target = plan.target.require_present()
    store_env = target.env({})  # BEADS_DIR for the detached store
    target.env()  # refuse an inherited BEADS_DIR naming another store
    report = PublishReport(
        hive=target.hive,
        remote=plan.remote,
        destination=plan.redacted,
        dry_run=dry_run,
        pushed=False,
    )
    if dry_run:
        return report
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


def _tail(res: Any, plan: PublishPlan) -> str:
    for name in ("stderr", "stdout"):
        raw = str(getattr(res, name, "") or "").replace(plan.url, plan.redacted)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return f"exit {getattr(res, 'returncode', '?')}"


__all__ = [
    "PublishError",
    "PublishPlan",
    "PublishReport",
    "dolt_git_url",
    "plan_publication",
    "publish_public",
]
