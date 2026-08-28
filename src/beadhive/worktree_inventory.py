"""Managed-worktree inventory, classification, and status presentation.

The pure lifecycle classifier stays in :mod:`beadhive.wt_status`. Implementations live here;
:mod:`beadhive.worktree` remains the stable facade and collaborator patch boundary.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer

from . import bd, config, jsonout, registry, wt_status
from .identity import workspace_identity


def _facade():
    from . import worktree

    return worktree


def _call_facade(name, *args, **kwargs):
    return getattr(_facade(), name)(*args, **kwargs)


_CLASSIFY_MAX_WORKERS = _facade()._CLASSIFY_MAX_WORKERS
_STORE_PROBE_CACHE = _facade()._STORE_PROBE_CACHE
_BOX_PIPE = _facade()._BOX_PIPE
_BOX_BRANCH = _facade()._BOX_BRANCH
_BOX_LAST = _facade()._BOX_LAST
_BOX_SPACE = _facade()._BOX_SPACE

_INVENTORY_SCHEMA_VERSION = 1
_INVENTORY_DEFAULT_LIMIT = 50
_INVENTORY_MAX_LIMIT = 200
_INVENTORY_STATES = frozenset(str(state) for state in wt_status.WtClassification)


def _managed_for_entry(*args, **kwargs):
    return _call_facade("_managed_for_entry", *args, **kwargs)


def managed(*args, **kwargs):
    return _call_facade("managed", *args, **kwargs)


def _emit(*args, **kwargs):
    return _call_facade("_emit", *args, **kwargs)


def _worktree_branch(*args, **kwargs):
    return _call_facade("_worktree_branch", *args, **kwargs)


def unregistered_worktrees(*args, **kwargs):
    return _call_facade("unregistered_worktrees", *args, **kwargs)


def list_cmd(*args, **kwargs):
    return _call_facade("list_cmd", *args, **kwargs)


def _classify_entries(*args, **kwargs):
    return _call_facade("_classify_entries", *args, **kwargs)


def _wt_dirty(*args, **kwargs):
    return _call_facade("_wt_dirty", *args, **kwargs)


def store_probe_cache(*args, **kwargs):
    return _call_facade("store_probe_cache", *args, **kwargs)


def _store_readable(*args, **kwargs):
    return _call_facade("_store_readable", *args, **kwargs)


def _probe_store(*args, **kwargs):
    return _call_facade("_probe_store", *args, **kwargs)


def _bead_statuses_for_entry(*args, **kwargs):
    return _call_facade("_bead_statuses_for_entry", *args, **kwargs)


def _classify_entry(*args, **kwargs):
    return _call_facade("_classify_entry", *args, **kwargs)


def _status_tags(*args, **kwargs):
    return _call_facade("_status_tags", *args, **kwargs)


def _render_status(*args, **kwargs):
    return _call_facade("_render_status", *args, **kwargs)


def _warn_untrustworthy(*args, **kwargs):
    return _call_facade("_warn_untrustworthy", *args, **kwargs)


def _status_scope(*args, **kwargs):
    return _call_facade("_status_scope", *args, **kwargs)


def _status_classifications(*args, **kwargs):
    return _call_facade("_status_classifications", *args, **kwargs)


def _ordered_statuses(*args, **kwargs):
    return _call_facade("_ordered_statuses", *args, **kwargs)


def status_rows(*args, **kwargs):
    return _call_facade("status_rows", *args, **kwargs)


def _warn_unregistered(*args, **kwargs):
    return _call_facade("_warn_unregistered", *args, **kwargs)


def _render_status_multi(*args, **kwargs):
    return _call_facade("_render_status_multi", *args, **kwargs)


def status_cmd(*args, **kwargs):
    return _call_facade("status_cmd", *args, **kwargs)


def _run_git(*args, **kwargs):
    return _call_facade("_run_git", *args, **kwargs)


def _bead_id_from_branch(*args, **kwargs):
    return _call_facade("_bead_id_from_branch", *args, **kwargs)


def bead_and_parent(*args, **kwargs):
    return _call_facade("bead_and_parent", *args, **kwargs)


def is_landed(*args, **kwargs):
    return _call_facade("is_landed", *args, **kwargs)


def is_merged(*args, **kwargs):
    return _call_facade("is_merged", *args, **kwargs)


def _entry_for_path(*args, **kwargs):
    return _call_facade("_entry_for_path", *args, **kwargs)


def _resolve_entry(*args, **kwargs):
    return _call_facade("_resolve_entry", *args, **kwargs)


def impl__managed_for_entry(e, root: str) -> list:
    """[(prefix, path, branch), ...] visible under `root` for ONE managed_repos entry, parsed
    from `git worktree list --porcelain`. [] when the entry has no local clone or the git call
    fails."""
    main = registry.hive_dir(e)
    if not (main / ".git").exists():
        return []
    res = _run_git(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return []
    out: list = []
    path = brref = None
    for line in (res.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            brref = None
        elif line.startswith("branch "):
            brref = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line.strip() and path:
            _emit(out, e, root, path, brref)
            path = brref = None
    if path:
        _emit(out, e, root, path, brref)
    return out


def impl_managed(cfg):
    """[(prefix, path, branch)] for every linked worktree under the shadow root."""
    root = str(config.worktrees_root().resolve())
    out = []
    for e in cfg.get("managed_repos", []) or []:
        out.extend(_managed_for_entry(e, root))
    return out


def impl__emit(out, entry, root, path, brref):
    try:
        under = Path(path).resolve().is_relative_to(root)
    except OSError:
        under = path.startswith(root + os.sep)
    if under:
        out.append((str(entry["prefix"]), path, brref or "(detached)"))


def impl__worktree_branch(path) -> str:
    """The current branch of the worktree at `path` ('(detached)' when HEAD isn't on a branch)."""
    res = _run_git(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"], check=False, capture=True
    )
    branch = (res.stdout or "").strip() if res.returncode == 0 else ""
    return branch if branch and branch != "HEAD" else "(detached)"


def impl_unregistered_worktrees(cfg):
    """[(slug, leaf, path, branch)] for git worktrees under the shadow root whose repo is NOT in
    managed_repos (bh-ea1i). The status/list sweep otherwise iterates only the hive registry, so a
    repo with worktrees on disk but no registration is silently omitted. This walks the wt root
    itself (``<root>/<provider>/<org>/<repo>/<leaf>``) so such orphans are surfaced, not dropped."""
    root = config.worktrees_root().resolve()
    if not root.exists():
        return []
    registered = {
        (str(e.get("provider")), str(e.get("org")), str(e.get("repo")))
        for e in (cfg.get("managed_repos", []) or [])
    }
    out = []
    for leaf in sorted(root.glob("*/*/*/*")):
        if not leaf.is_dir():
            continue
        parts = leaf.relative_to(root).parts
        if len(parts) != 4:
            continue
        provider, org, repo, leaf_name = parts
        if (provider, org, repo) in registered:
            continue
        if not (leaf / ".git").exists():
            continue  # a plain dir, not a linked git worktree
        out.append((f"{provider}/{org}/{repo}", leaf_name, str(leaf), _worktree_branch(leaf)))
    return out


def _inventory_digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _inventory_cursor(revision: str, scope: dict, offset: int) -> str:
    value = {"v": 1, "revision": revision, "scope": scope, "offset": offset}
    return (
        base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _inventory_cursor_offset(cursor: str | None, revision: str, scope: dict) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the worktree inventory cursor is malformed") from exc
    if (
        not isinstance(value, dict)
        or value.get("v") != 1
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
    ):
        raise ValueError("the worktree inventory cursor is malformed")
    if value.get("scope") != scope:
        raise ValueError("the worktree inventory cursor belongs to different filters")
    if value.get("revision") != revision:
        raise ValueError("the worktree inventory changed; restart without a cursor")
    return value["offset"]


def _inventory_item(observation: dict, status) -> dict:
    hive_id = str(observation["hive_id"])
    classification = str(status.classification)
    return {
        "hive_id": hive_id,
        "hive_prefix": str(observation["hive_prefix"]),
        "bead_id": status.bead_id,
        "worktree_id": f"{hive_id}:{status.leaf}",
        "leaf": status.leaf,
        "branch": status.branch,
        "path": status.path,
        "state": classification,
        "retention": "reclaimable" if status.safe else "retained",
        "merged": status.merged,
        "dirty": status.dirty,
        "safe": status.safe,
        "underlying_state": str(status.underlying) if status.underlying else None,
        "unknown_reason": status.unknown_reason or None,
    }


def _inventory_coverage_state(observations: list[dict]) -> str:
    states = {str(observation.get("state", "unavailable")) for observation in observations}
    if not states or states == {"complete"}:
        return "complete"
    if states == {"unavailable"}:
        return "unavailable"
    if states == {"stale"}:
        return "stale"
    return "partial"


def impl_inventory_payload(
    observations: list[dict],
    *,
    hive: str = "",
    states: tuple[str, ...] = (),
    limit: int = _INVENTORY_DEFAULT_LIMIT,
    cursor: str | None = None,
    generated_at: int | None = None,
) -> dict:
    """Build the versioned, bounded managed-worktree contract from source observations.

    An observation has an exact ``hive_id``, its configured ``hive_prefix``, a coverage
    ``state`` (complete/partial/stale/unavailable), an optional ``reason`` and ``revision``, and
    zero or more classified ``statuses``.  Keeping this fold pure makes the important count
    rule explicit: totals are numbers only when every covered source is complete.  A partial
    page is safe because totals are computed over the complete filtered snapshot before paging.
    """
    if not 1 <= limit <= _INVENTORY_MAX_LIMIT:
        raise ValueError(f"limit must be from 1 through {_INVENTORY_MAX_LIMIT}")
    requested_states = tuple(sorted(set(states)))
    invalid_states = sorted(set(requested_states) - _INVENTORY_STATES)
    if invalid_states:
        raise ValueError(f"unknown worktree state: {invalid_states[0]}")

    normalized: list[dict] = []
    all_items: list[dict] = []
    warnings: list[dict] = []
    for raw in observations:
        observation = {
            "hive_id": str(raw["hive_id"]),
            "hive_prefix": str(raw["hive_prefix"]),
            "state": str(raw.get("state") or "unavailable"),
            "reason": str(raw.get("reason") or "") or None,
            "revision": raw.get("revision"),
            "statuses": list(raw.get("statuses") or []),
        }
        if observation["state"] == "complete" and any(
            str(status.classification) == "unknown" for status in observation["statuses"]
        ):
            observation["state"] = "partial"
            observation["reason"] = "one or more worktree states could not be resolved"
        normalized.append(observation)
        all_items.extend(_inventory_item(observation, status) for status in observation["statuses"])
        if observation["state"] != "complete":
            warnings.append(
                {
                    "code": f"worktree_source_{observation['state']}",
                    "hive_id": observation["hive_id"],
                    "detail": observation["reason"],
                }
            )

    all_items.sort(key=lambda item: (item["hive_id"], item["worktree_id"]))
    coverage_state = _inventory_coverage_state(normalized)
    coverage_complete = coverage_state == "complete"
    filtered = [
        item for item in all_items if not requested_states or item["state"] in requested_states
    ]
    source_revision = (
        None
        if coverage_state == "unavailable"
        else _inventory_digest(
            [
                {
                    "hive_id": observation["hive_id"],
                    "state": observation["state"],
                    "revision": observation["revision"],
                    "items": [
                        item for item in all_items if item["hive_id"] == observation["hive_id"]
                    ],
                }
                for observation in normalized
            ]
        )
    )
    scope = {"hive": hive or None, "states": list(requested_states)}
    offset = _inventory_cursor_offset(cursor, source_revision or "unavailable", scope)
    if offset > len(filtered):
        raise ValueError("the worktree inventory cursor is outside the collection")
    page = filtered[offset : offset + limit]
    next_offset = offset + len(page)
    truncated = next_offset < len(filtered)

    counts = None
    if coverage_complete:
        counts = []
        for observation in normalized:
            hive_items = [item for item in all_items if item["hive_id"] == observation["hive_id"]]
            by_state = {
                state: sum(item["state"] == state for item in hive_items)
                for state in sorted({item["state"] for item in hive_items})
            }
            counts.append(
                {
                    "hive_id": observation["hive_id"],
                    "hive_prefix": observation["hive_prefix"],
                    "total": len(hive_items),
                    "by_state": by_state,
                }
            )

    now = generated_at if generated_at is not None else time.time_ns() // 1_000_000
    freshness_state = (
        "stale"
        if any(observation["state"] == "stale" for observation in normalized)
        else "unknown"
        if coverage_state == "unavailable"
        else "fresh"
    )
    reason = (
        "; ".join(
            sorted(
                {str(observation["reason"]) for observation in normalized if observation["reason"]}
            )
        )
        or None
    )
    return jsonout.envelope(
        "worktree list",
        _INVENTORY_SCHEMA_VERSION,
        {
            "source_revision": source_revision,
            "generated_at": now,
            "freshness": {
                "state": freshness_state,
                "as_of": now if freshness_state != "unknown" else None,
            },
            "coverage": {
                "state": coverage_state,
                "reason": reason,
                "sources": [
                    {
                        "hive_id": observation["hive_id"],
                        "state": observation["state"],
                        "reason": observation["reason"],
                        "revision": observation["revision"],
                    }
                    for observation in normalized
                ],
            },
            "filters": scope,
            "worktrees": page,
            "returned": len(page),
            "total": len(filtered) if coverage_complete else None,
            "counts": counts,
            "limit": limit,
            "truncated": truncated,
            "next_cursor": (
                _inventory_cursor(source_revision or "unavailable", scope, next_offset)
                if truncated
                else None
            ),
            "warnings": warnings,
        },
    )


def _inventory_observations(hive: str = "") -> list[dict]:
    """Read each scoped registered hive independently so one failed source stays partial."""
    cfg = config.load()
    entries, _unused = _status_scope(cfg, hive, [])
    root = str(config.worktrees_root().resolve())
    observations: list[dict] = []
    for entry in entries:
        hive_id = registry.hive_key(entry)
        prefix = str(entry.get("prefix", ""))
        main = registry.hive_dir(entry)
        try:
            result = _run_git(
                ["git", "-C", str(main), "worktree", "list", "--porcelain"],
                check=False,
                capture=True,
            )
        except Exception as exc:
            observations.append(
                {
                    "hive_id": hive_id,
                    "hive_prefix": prefix,
                    "state": "unavailable",
                    "reason": f"git could not enumerate the registered hive's worktrees: {exc}",
                    "revision": None,
                    "statuses": [],
                }
            )
            continue
        if result.returncode != 0:
            observations.append(
                {
                    "hive_id": hive_id,
                    "hive_prefix": prefix,
                    "state": "unavailable",
                    "reason": "git could not enumerate the registered hive's worktrees",
                    "revision": None,
                    "statuses": [],
                }
            )
            continue
        rows: list = []
        path = branch_ref = None
        for line in (result.stdout or "").splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :]
                branch_ref = None
            elif line.startswith("branch "):
                branch_ref = line[len("branch ") :].removeprefix("refs/heads/")
            elif not line.strip() and path:
                _emit(rows, entry, root, path, branch_ref)
                path = branch_ref = None
        if path:
            _emit(rows, entry, root, path, branch_ref)
        try:
            with store_probe_cache():
                statuses = _classify_entry(entry, rows, cfg) if rows else []
        except Exception as exc:
            observations.append(
                {
                    "hive_id": hive_id,
                    "hive_prefix": prefix,
                    "state": "unavailable",
                    "reason": f"worktree classification failed: {exc}",
                    "revision": _inventory_digest(result.stdout or ""),
                    "statuses": [],
                }
            )
            continue
        observations.append(
            {
                "hive_id": hive_id,
                "hive_prefix": prefix,
                "state": "complete",
                "reason": None,
                "revision": _inventory_digest(
                    {"porcelain": result.stdout or "", "statuses": [s.as_dict() for s in statuses]}
                ),
                "statuses": statuses,
            }
        )

    # Hub inventory must not claim exact fleet counts while managed-shaped worktrees exist for
    # an unregistered repository.  The human command already warns about these rows; the machine
    # contract represents the same gap as partial coverage rather than silently returning a
    # plausible smaller total.  A hive-scoped read intentionally excludes unrelated orphans.
    if not hive:
        ident = workspace_identity()
        cwd = Path.cwd()
        try:
            under_worktrees = cwd.resolve().is_relative_to(config.worktrees_root().resolve())
        except OSError:
            under_worktrees = False
        registered_cwd = False
        if ident is not None:
            registered_cwd = any(
                (str(entry["provider"]), str(entry["org"]), str(entry["repo"])) == ident
                for entry in cfg.get("managed_repos", []) or []
            )
        elif under_worktrees:
            try:
                _entry_for_path(cfg, cwd)
            except SystemExit:
                pass
            else:
                registered_cwd = True
        if not registered_cwd:
            by_slug: dict[str, list] = {}
            for row in unregistered_worktrees(cfg):
                by_slug.setdefault(row[0], []).append(row)
            for slug, rows in sorted(by_slug.items()):
                observations.append(
                    {
                        "hive_id": slug,
                        "hive_prefix": slug,
                        "state": "partial",
                        "reason": "managed worktrees exist for an unregistered repository",
                        "revision": _inventory_digest(rows),
                        "statuses": [],
                    }
                )
    return observations


def impl_list_cmd(
    *,
    as_json: bool = False,
    hive: str = "",
    states: tuple[str, ...] = (),
    limit: int = _INVENTORY_DEFAULT_LIMIT,
    cursor: str | None = None,
):
    if as_json:
        try:
            payload = impl_inventory_payload(
                _inventory_observations(hive),
                hive=hive,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            typer.echo(f"\u2717 {exc}", err=True)
            raise typer.Exit(2) from exc
        jsonout.emit(payload)
        return
    cfg = config.load()
    rows = managed(cfg)
    unreg = unregistered_worktrees(cfg)
    if not rows and not unreg:
        typer.echo("no managed worktrees")
        return
    for prefix, path, br in rows:
        typer.echo(f"{prefix}\t{br}\t{path}")
    for slug, _leaf, path, br in unreg:
        typer.echo(f"{slug}\t{br}\t{path}")
    if unreg:
        _warn_unregistered(unreg)


def impl__classify_entries(
    cfg,
    entries: list,
    rows_by_prefix: dict[str, list],
    on_complete=None,
) -> dict[str, list]:
    """Classify populated hives concurrently, optionally reporting each completed hive.

    Every classification has independent git and bead-store subprocesses, so waiting for one
    hive before starting the next only delays the fleet view.  Each worker owns a
    :func:`store_probe_cache` context: cache entries are per main-clone path and one worker owns
    one hive, which preserves the one-probe-per-hive command contract without sharing a mutable
    ``ContextVar`` value between threads.

    Results are keyed rather than appended in completion order.  Callers that return structured
    data can then retain their established deterministic entry ordering, while the human status
    renderer uses ``on_complete`` to show a hive as soon as it is ready.
    """
    jobs = [
        (str(entry.get("prefix", "")), entry, rows_by_prefix.get(str(entry.get("prefix", "")), []))
        for entry in entries
        if rows_by_prefix.get(str(entry.get("prefix", "")), [])
    ]
    if not jobs:
        return {}

    def classify_one(entry, entry_rows):
        # A context is intentionally per worker rather than around the executor: ContextVars do
        # not propagate their values into new threads, and no two workers classify one hive.
        with store_probe_cache():
            return _classify_entry(entry, entry_rows, cfg)

    statuses_by_prefix: dict[str, list] = {}
    if len(jobs) == 1:
        prefix, entry, entry_rows = jobs[0]
        statuses = classify_one(entry, entry_rows)
        statuses_by_prefix[prefix] = statuses
        if on_complete is not None:
            on_complete(prefix, statuses)
        return statuses_by_prefix

    with ThreadPoolExecutor(
        max_workers=min(_CLASSIFY_MAX_WORKERS, len(jobs)),
        thread_name_prefix="bh-worktree-classify",
    ) as executor:
        futures = {
            executor.submit(classify_one, entry, entry_rows): prefix
            for prefix, entry, entry_rows in jobs
        }
        for future in as_completed(futures):
            prefix = futures[future]
            statuses = future.result()
            statuses_by_prefix[prefix] = statuses
            if on_complete is not None:
                on_complete(prefix, statuses)

    return statuses_by_prefix


def impl__wt_dirty(path: str) -> bool:
    """True iff the worktree at `path` has uncommitted changes.

    Runs ``git status --porcelain`` directly in the worktree directory — the only reliable
    approach for linked worktrees, since the main clone's ``RepoMetadata.branches`` dirty flag
    only reflects the main clone's checked-out branch.  Best-effort: if the path does not exist
    or git fails, treated as clean (not dirty) so a missing worktree is never blocked by I/O.
    """
    try:
        res = _run_git(["git", "-C", path, "status", "--porcelain"], check=False, capture=True)
        return res.returncode == 0 and bool((res.stdout or "").strip())
    except Exception:
        return False


@contextlib.contextmanager
def impl_store_probe_cache():
    """Resolve each hive's store readability ONCE for the duration of this block (bh-ioub2).

    A CONTEXT rather than a process-lifetime memo, and the distinction is the point.
    ``worktree status``'s own help promises it "repopulates fresh metadata before classifying —
    the pre-flight never uses stale data"; a memo that outlived the command would quietly break
    exactly that promise inside a long-lived process (`bh mcp serve` holds one for days). Scoping
    it to the caller keeps the guarantee — one probe per command, never across commands — the
    same way `bd.strict_reads` scopes strictness to a surface rather than a call site.

    THE COST IT REMOVES, measured shape: `retire.teardown_worktrees` calls `worktree.remove` once
    per worktree, and each removal's UNKNOWN preflight re-asked the SAME hive whether its store
    could be read. Retiring the 28-worktree agentguides/runtime hive — the hive that motivated
    bh-167s0 — therefore paid 28 store probes for one hive-level fact, against a store that is by
    that bead's own premise either slow or refusing. An unbounded `bd` call in a loop is the
    shape bh-toitp exists to eliminate; this is that shape, introduced by the fix next door.
    """
    token = _STORE_PROBE_CACHE.set({})
    try:
        yield
    finally:
        _STORE_PROBE_CACHE.reset(token)


def impl__store_readable(main: Path) -> str:
    """ "" iff this hive's bead store answers with real issues; otherwise WHY it does not.

    THE PRE-FLIGHT'S OWN PRE-FLIGHT (bh-167s0).  ``worktree status`` promises it "repopulates
    fresh metadata before classifying — the pre-flight never uses stale data", and then accepted
    an unreadable bead store without a word.  A per-bead miss cannot tell the two causes apart:
    on the hive that produced this bead, ``bd show <id>`` came back EMPTY WITH EXIT 0 for every
    id, because bd's schema-fork guard was refusing to open the database — identical on the wire
    to "no such bead".  So the store is asked once, up front, and every subsequent miss is read
    in that light.

    ``bd list`` rather than a bead lookup on purpose: the question is whether the store answers
    AT ALL, and a store holding zero issues is the measured signature of the schema-fork guard
    (``issues=0  schema_blocked=1`` on three of the four agentguides hives, while the fourth
    answered 102).  It is deliberately NOT fatal — an empty store is a legitimate state for a
    fresh hive, and the caller's job is to stop CLASSIFYING confidently, not to refuse to run.

    Memoized per main-clone path inside a :func:`store_probe_cache` block, and only there — see
    that function for why the scope is the command rather than the process.
    """
    cache = _STORE_PROBE_CACHE.get()
    if cache is not None and str(main) in cache:
        return cache[str(main)]
    reason = _probe_store(main)
    if cache is not None:
        cache[str(main)] = reason
    return reason


def impl__probe_store(main: Path) -> str:
    """The uncached probe itself — split out so the memo above is obviously a memo and nothing
    more, and so a test can count invocations of the thing that actually shells out."""
    issues = bd.json(["list"], str(main))
    if issues is None:
        return (
            f"the bead store at {main} could not be READ (bd exited non-zero or returned "
            "no JSON) — bd absent, a schema-fork guard refusing to open the database, or a "
            "store engine that is down; try `bh bd list` there to see bd's own error"
        )
    if isinstance(issues, list) and not issues:
        return (
            f"the bead store at {main} answered with ZERO issues — an empty hive, or a store "
            "bd is refusing to read (its schema-fork guard reports no issues rather than an "
            "error: `bd migrate schema --inspect` reports the real version skew)"
        )
    return ""


def impl__bead_statuses_for_entry(
    entry,
    rows: list[tuple[str, str, str]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str]:
    """Fetch bead statuses and close_reasons for every bead id in ``rows`` for this entry.

    Uses the same ``bd show`` seam as ``doctor._orphan_container_branches`` (bd.show).  The
    bead id is parsed from the real ``wt/bead/<type>/<id>`` branch ref in each row via
    :func:`_bead_id_from_branch` — this preserves dots that the sanitized directory leaf converts
    to dashes (the same fix as ``bead_and_parent``).  Non-bead worktrees are skipped.

    Returns ``(statuses, close_reasons, unknown_reasons, store_reason)``.  ``close_reasons``
    holds the AGF lifecycle close_reason (e.g. ``"merged"``, ``"molecule landed"``) — used by
    ``is_landed`` to confirm rebase/squash-landed branches.

    ``unknown_reasons`` / ``store_reason`` are bh-167s0: an id that does not resolve is reported
    WITH ITS REASON rather than silently becoming an empty status the classifier reads as "open".
    The reason has to be built HERE because this is the only layer that knows both what was
    asked and what came back; the classifier is pure and would have to guess.
    """

    main = registry.hive_dir(entry)
    store_reason = _store_readable(main)
    statuses: dict[str, str] = {}
    close_reasons: dict[str, str] = {}
    unknown_reasons: dict[str, str] = {}
    for _, _path, branch in rows:
        bead_id = _bead_id_from_branch(branch)
        if not bead_id or bead_id in statuses:
            continue
        bead = bd.show(bead_id, str(main))
        statuses[bead_id] = (bead or {}).get("status", "")
        close_reasons[bead_id] = (bead or {}).get("close_reason", "")
        if not statuses[bead_id] and not store_reason:
            # The store answers, and this ONE id is not in it.  Measured cause on the hive that
            # produced this bead: a retired bead PREFIX.  The store held 96 `ag-run-*` beads and
            # zero `ag-rt-*`, while 21 of 28 worktree branches were named `wt/bead/issue/ag-rt-*`
            # — the ids exist, under a name no longer derivable from the branch, and every one of
            # those beads was CLOSED.  `bh hive repair` reconciles registry<->database and stops,
            # so nothing renames the branches; the row is unclassifiable until something does.
            unknown_reasons[bead_id] = (
                f"the store answers, but bead {bead_id} is not in it — the branch names an id "
                "that no longer exists (a retired bead prefix leaves every worktree created "
                "under the old one unresolvable), or the bead was deleted"
            )
    return statuses, close_reasons, unknown_reasons, store_reason


def impl__classify_entry(
    entry,
    rows: list[tuple[str, str, str]],
    cfg,
) -> list:
    """Classify all managed worktrees for one hive entry.

    Repopulates fresh metadata (ttl=0) then runs the classifier.  Returns a list of
    ``WtStatus`` objects.
    """
    from . import metadata

    key = registry.hive_key(entry)
    meta_map = metadata.read_fleet(cfg, [key], ttl=0)
    meta = meta_map.get(key)
    meta_branches = meta.branches if meta else []

    integration = config.integration_branch(cfg, entry)
    bead_statuses, bead_close_reasons, unknown_reasons, store_reason = _bead_statuses_for_entry(
        entry, rows
    )
    dirty_by_path = {path: _wt_dirty(path) for _, path, _ in rows}

    # Closures capture the full entry so bead_and_parent / is_merged / is_landed receive
    # the correct provider/org/repo context; the classify signature's `entry` param is ignored.
    def _merged_fn(_e, branch, base):
        return is_merged(entry, branch, base)

    def _parent_fn(_e, path, integ, br=""):
        return bead_and_parent(entry, path, integ, br)

    def _landed_fn(_e, branch, base, close_reason):
        return is_landed(entry, branch, base, close_reason)

    return wt_status.classify(
        hive_prefix=str(entry.get("prefix", "")),
        managed_rows=rows,
        meta_branches=meta_branches,
        bead_statuses=bead_statuses,
        dirty_by_path=dirty_by_path,
        is_merged_fn=_merged_fn,
        parent_fn=_parent_fn,
        integration=integration,
        is_landed_fn=_landed_fn,
        bead_close_reasons=bead_close_reasons,
        bead_unknown_reasons=unknown_reasons,
        store_unreadable_reason=store_reason,
    )


def impl__status_tags(st) -> str:
    """The trailing tag run on one rendered row.

    ``? UNKNOWN`` is deliberately the loudest thing on the line and the only class carrying a
    glyph: it is the one classification that means "do not act on this row", and it has to be
    findable by eye in a tree of thirty (bh-167s0 — "visually distinct in the rendered tree").
    A ``DIRTY`` row also shows what it is masking, so a dirty-but-SAFE seat is distinguishable
    from a dirty-and-open one and a dirty row over an unresolvable bead cannot look ordinary.
    """
    tags = ""
    if st.merged:
        tags += "  merged"
    if st.dirty:
        tags += "  dirty"
    if getattr(st, "underlying", None):
        tags += f"  (under: {str(st.underlying).upper()})"
    if st.safe:
        tags += "  SAFE"
    return tags


def impl__render_status(statuses: list, header: str = "") -> None:
    """Render a list of WtStatus entries as a text tree to stdout.

    Format::

        <header>          (omitted when empty)
        ├─ <leaf>  [<branch>]  <CLASSIFICATION>  <merged>  SAFE
        └─ <leaf>  [<branch>]  <CLASSIFICATION>

    Box-drawing prefixes only; no rich / colour.
    """
    if header:
        typer.echo(header)
    for i, st in enumerate(statuses):
        prefix = _BOX_LAST if i == len(statuses) - 1 else _BOX_BRANCH
        mark = "? " if str(st.classification) == "unknown" else ""
        typer.echo(
            f"{prefix}{mark}{st.leaf}  [{st.branch}]  {st.classification.upper()}{_status_tags(st)}"
        )


def impl__warn_untrustworthy(statuses: list) -> None:
    """Say plainly, per hive, that these classifications cannot back a removal decision.

    bh-167s0's third acceptance criterion, and the reason the bead is P1 rather than cosmetic:
    an operator asking "can I archive this?" was told 16 worktrees were ACTIVE and reasonably
    concluded there was live work.  The rows are not merely wrong — they are UNKNOWABLE from
    here, and one bad row poisons the hive's whole answer, because whatever stopped that bead
    resolving stopped nothing else being confirmed either.
    """
    by_hive: dict[str, list] = {}
    for s in wt_status.untrustworthy(statuses):
        by_hive.setdefault(s.hive, []).append(s)
    for hive, rows in by_hive.items():
        typer.echo(
            f"\n⚠ {hive}: {len(rows)} worktree(s) UNKNOWN — the bead could not be resolved, so "
            f"this hive's classifications are NOT a basis for a removal decision.",
            err=True,
        )
        for reason in sorted({r.unknown_reason for r in rows if r.unknown_reason}):
            typer.echo(f"    {reason}", err=True)
        typer.echo(
            "  `prune` will refuse this hive and `rm` will refuse these rows until it resolves.",
            err=True,
        )


def impl__status_scope(cfg, hive: str, all_rows: list) -> tuple:
    """Resolve which managed_repos entries — and their pre-grouped rows — are in scope for
    `status_rows`'s `hive` argument. See `status_rows`'s docstring for the scoping rules.
    Returns `(entries, rows_by_prefix)`."""
    if hive:
        entry = _resolve_entry(cfg, hive)
        target_prefix = str(entry.get("prefix", ""))
        return [entry], {target_prefix: [r for r in all_rows if r[0] == target_prefix]}

    # Try to resolve from cwd; fall through to all-hives on failure
    ident = workspace_identity()
    cwd = Path.cwd()
    root = config.worktrees_root()
    try:
        under_wts = cwd.resolve().is_relative_to(root.resolve())
    except OSError:
        under_wts = False

    entry_from_cwd = None
    if ident is not None:
        provider, org, repo = ident
        for e in cfg.get("managed_repos", []) or []:
            if (str(e["provider"]), str(e["org"]), str(e["repo"])) == (provider, org, repo):
                entry_from_cwd = e
                break
    elif under_wts:
        try:
            entry_from_cwd = _entry_for_path(cfg, cwd)
        except SystemExit:
            entry_from_cwd = None

    if entry_from_cwd is not None:
        target_prefix = str(entry_from_cwd.get("prefix", ""))
        return [entry_from_cwd], {target_prefix: [r for r in all_rows if r[0] == target_prefix]}

    # Hub scope: all managed hives
    entries = list(cfg.get("managed_repos", []) or [])
    rows_by_prefix: dict = {}
    for r in all_rows:
        rows_by_prefix.setdefault(r[0], []).append(r)
    return entries, rows_by_prefix


def impl__status_classifications(hive: str = "", on_complete=None) -> tuple:
    """Load status inputs and classify their populated hives.

    ``on_complete`` receives ``(prefix, statuses)`` in completion order.  It is used only by the
    human multi-hive CLI path; structured callers collect the returned mapping and retain entry
    order.
    """
    cfg = config.load()
    all_rows = managed(cfg)  # [(prefix, path, branch), ...]
    entries, rows_by_prefix = _status_scope(cfg, hive, all_rows)
    return cfg, entries, _classify_entries(cfg, entries, rows_by_prefix, on_complete=on_complete)


def impl__ordered_statuses(entries: list, statuses_by_prefix: dict[str, list]) -> list:
    """Flatten completed classifications in managed-repository order, never finish order."""
    return [
        status
        for entry in entries
        for status in statuses_by_prefix.get(str(entry.get("prefix", "")), [])
    ]


def impl_status_rows(hive: str = "") -> list:
    """Return the ``WtStatus`` list for managed worktrees — Typer-free core.

    Repopulates fresh metadata before classifying — never uses stale data.
    Scoping mirrors ``status_cmd``:
      - ``hive`` → that hive only.
      - No ``hive`` and cwd is inside a hive → that hive.
      - No ``hive`` and not in a hive (hub) → all managed hives.

    Called by both ``status_cmd`` (the Typer command) and the MCP
    ``beadhive://worktree/list`` resource.
    """
    _cfg, entries, statuses_by_prefix = _status_classifications(hive)
    return _ordered_statuses(entries, statuses_by_prefix)


def impl__warn_unregistered(unreg) -> None:
    """Surface unregistered repos that have on-disk managed worktrees (bh-ea1i) — a warning so they
    are never silently omitted from status/list. Lists the repo slug + each orphaned worktree."""
    repos = sorted({slug for slug, *_ in unreg})
    typer.echo(
        f"⚠ {len(unreg)} managed worktree(s) under unregistered repo(s) {', '.join(repos)} — "
        f"register with `{config.BINARY_ALIAS} hive add` to include them fully",
        err=True,
    )
    for slug, leaf, path, br in unreg:
        typer.echo(f"    {slug}  {leaf}  [{br}]  {path}", err=True)


def impl__render_status_multi(by_hive: dict) -> None:
    """Nested tree-with-hive-headers rendering `status_cmd` uses when statuses span >1 hive."""
    hive_keys = list(by_hive)
    for ri, hive_label in enumerate(hive_keys):
        statuses = by_hive[hive_label]
        is_last_hive = ri == len(hive_keys) - 1
        hive_prefix = _BOX_LAST if is_last_hive else _BOX_BRANCH
        typer.echo(f"{hive_prefix}{hive_label}")
        for i, st in enumerate(statuses):
            indent = _BOX_SPACE if is_last_hive else _BOX_PIPE
            node = _BOX_LAST if i == len(statuses) - 1 else _BOX_BRANCH
            mark = "? " if str(st.classification) == "unknown" else ""
            typer.echo(
                f"{indent}{node}{mark}{st.leaf}  [{st.branch}]  {st.classification.upper()}"
                f"{_status_tags(st)}"
            )


def impl_status_cmd(hive: str = "", as_json: bool = False) -> None:
    """Render per-worktree status for one hive (--hive/-r) or all managed hives.

    Repopulates fresh metadata before classifying — the pre-flight never uses stale data.
    Scoping:
      - ``--hive <id>`` → that hive only.
      - No ``--hive`` and cwd is inside a hive → that hive.
      - No ``--hive`` and not in a hive (hub) → all managed hives.
    """
    import json as _json

    # Streaming is only for the all-hive human view.  JSON is intentionally collected first so
    # consumers retain its deterministic managed-repository ordering, and the established
    # single-hive rendering remains byte-for-byte the same.
    cfg = config.load()
    all_rows = managed(cfg)
    entries, rows_by_prefix = _status_scope(cfg, hive, all_rows)
    populated_entries = [
        entry for entry in entries if rows_by_prefix.get(str(entry.get("prefix", "")), [])
    ]
    stream_multi = not as_json and len(populated_entries) > 1

    def render_completed_hive(prefix: str, statuses: list) -> None:
        # Render a complete one-hive tree immediately.  The standalone tree deliberately uses
        # the same renderer and therefore keeps its hierarchy, UNKNOWN marking, and SAFE tags;
        # only section order now follows completion order.
        _render_status_multi({prefix: statuses})

    statuses_by_prefix = _classify_entries(
        cfg,
        entries,
        rows_by_prefix,
        on_complete=render_completed_hive if stream_multi else None,
    )
    all_statuses = _ordered_statuses(entries, statuses_by_prefix)
    unreg = unregistered_worktrees(cfg) if not hive else []

    if as_json:
        typer.echo(_json.dumps([s.as_dict() for s in all_statuses], indent=2))
        # …and on stderr for the JSON reader too. The payload carries `classification:
        # "unknown"` and its `unknown_reason`, but a consumer that only counts `active` sees a
        # plausible answer either way — the same shape bh-fzh4h closed for the MCP surface.
        _warn_untrustworthy(all_statuses)
        if unreg:
            _warn_unregistered(unreg)
        return

    if not all_statuses:
        if unreg:
            _warn_unregistered(unreg)
        else:
            typer.echo("no managed worktrees")
        return

    if not stream_multi:
        # Group by hive for the tree header when covering multiple hives.  In streaming mode,
        # each completed section has already been rendered by the callback above.
        by_hive: dict[str, list] = {}
        for s in all_statuses:
            by_hive.setdefault(s.hive, []).append(s)

        if len(by_hive) == 1:
            # Single-hive: show a flat tree with no hive header
            hive_label, statuses = next(iter(by_hive.items()))
            typer.echo(f"worktrees: {hive_label}")
            _render_status(statuses)
        else:
            # Multi-hive: nest under a hive header line
            _render_status_multi(by_hive)

    _warn_untrustworthy(all_statuses)

    if unreg:
        _warn_unregistered(unreg)
