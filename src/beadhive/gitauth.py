"""Read-only per-repo-group auth introspection (bh-4y0r.3).

Each repo group (see `gitworkspace.RepoGroup`) may auth differently: an SSH host alias via
`url.<alias>.insteadOf`, a scoped identity/signing key via `includeIf "gitdir:<dir>/"`
blocks, or just the plain global identity. This module only ever READS global git config
(`git config --global ...` / `git config --file <included-config> ...`) to report which of
those applies to each group — it NEVER writes global git config; that stays with
custodian/homelab provisioning.
"""

from __future__ import annotations

import re
from pathlib import Path

from .identity import workspace_root
from .run import run


def _global_get(key: str) -> str:
    res = run(["git", "config", "--global", "--get", key], check=False, capture=True)
    return res.stdout.strip() if res.returncode == 0 else ""


def global_identity() -> dict:
    """The fallback identity: global user.name/email/signingkey ('' if unset)."""
    return {
        "name": _global_get("user.name"),
        "email": _global_get("user.email"),
        "signingkey": _global_get("user.signingkey"),
    }


def _get_regexp(pattern: str) -> list[tuple[str, str]]:
    """[(key, value), ...] from `git config --global --get-regexp <pattern>` — [] on any
    failure (no match, git missing, malformed regex). Never raises."""
    try:
        res = run(["git", "config", "--global", "--get-regexp", pattern], check=False, capture=True)
    except Exception:  # noqa: BLE001 - read-only introspection: any failure means "nothing found"
        return []
    if res.returncode != 0:
        return []
    out = []
    for line in res.stdout.splitlines():
        if " " in line:
            key, _, value = line.partition(" ")
            out.append((key, value))
    return out


def insteadof_aliases() -> list[tuple[str, str]]:
    """[(alias, original), ...] from every `url.<alias>.insteadOf <original>` entry."""
    out = []
    for key, value in _get_regexp(r"^url\..*\.insteadof$"):
        m = re.match(r"^url\.(.+)\.insteadof$", key)
        if m:
            out.append((m.group(1), value))
    return out


def includeif_blocks() -> list[tuple[str, str]]:
    """[(gitdir_pattern, included_config_path), ...] from every
    `includeIf.gitdir[/i]:<pattern>.path <config>` entry."""
    out = []
    for key, value in _get_regexp(r"^includeif\.gitdir.*\.path$"):
        m = re.match(r"^includeif\.(gitdir/?i?:.+)\.path$", key, re.IGNORECASE)
        if m:
            out.append((m.group(1), value))
    return out


def _pattern_dir(pattern: str) -> str:
    """The directory prefix an includeIf `gitdir:`/`gitdir/i:` pattern names, trailing
    `**`/`*`/`/` glob noise stripped, `~` expanded. A best-effort prefix match — not a full
    gitignore-style glob engine, which is out of scope for a read-only report."""
    raw = re.sub(r"^gitdir/i:", "", pattern, flags=re.IGNORECASE)
    raw = re.sub(r"^gitdir:", "", raw, flags=re.IGNORECASE)
    raw = raw.rstrip("*").rstrip("/")
    return str(Path(raw).expanduser())


def scoped_identity_for(group_dir: Path) -> dict | None:
    """The identity (name/email/signingkey/pattern) from the FIRST includeIf block whose
    gitdir pattern prefixes `group_dir`, read from its included config file via
    `git config --file <path> --list` (read-only). None when no block scopes to this dir or
    the included file can't be read."""
    target = str(group_dir)
    for pattern, path in includeif_blocks():
        prefix = _pattern_dir(pattern)
        if not prefix or not target.startswith(prefix):
            continue
        included = Path(path).expanduser()
        if not included.is_file():
            continue
        res = run(["git", "config", "--file", str(included), "--list"], check=False, capture=True)
        if res.returncode != 0:
            continue
        values = {}
        for line in res.stdout.splitlines():
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
        return {
            "pattern": pattern,
            "name": values.get("user.name", ""),
            "email": values.get("user.email", ""),
            "signingkey": values.get("user.signingkey", ""),
        }
    return None


def insteadof_for_urls(urls: list[str]) -> str | None:
    """The alias of the first insteadOf rule whose `original` prefixes any of `urls`, else
    None (no positive evidence of a rewrite covering this group's repos)."""
    aliases = insteadof_aliases()
    for url in urls:
        for alias, original in aliases:
            if original and url.startswith(original):
                return alias
    return None


def group_auth_table(cfg) -> list[dict]:
    """Per-group auth row: {path, account, name, email, signingkey, scoped, insteadof_alias}.
    `scoped` is True iff an includeIf gitdir: block covers the group's on-disk dir; otherwise
    the row reports the plain global identity as the fallback bh would actually use there."""
    from . import gitworkspace  # lazy: avoid a load-time import cycle

    root = Path(workspace_root())
    fallback = global_identity()
    urls_by_group: dict[str, list[str]] = {}
    for key, url in gitworkspace.repo_urls(cfg).items():
        group_path = key.split("/", 1)[0]
        urls_by_group.setdefault(group_path, []).append(url)

    rows = []
    for g in gitworkspace.groups(cfg):
        scoped = scoped_identity_for(root / g.path / g.account)
        identity = scoped or fallback
        rows.append(
            {
                "path": g.path,
                "account": g.account,
                "name": identity.get("name", ""),
                "email": identity.get("email", ""),
                "signingkey": identity.get("signingkey", ""),
                "scoped": scoped is not None,
                "insteadof_alias": insteadof_for_urls(urls_by_group.get(g.path, [])),
            }
        )
    return rows


def group_auth_warnings(rows: list[dict]) -> list[str]:
    """Warn (never error) on a group with no scoped identity, and on two-or-more groups
    silently sharing the exact same resolved identity."""
    warns: list[str] = []
    by_identity: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        if not r["scoped"]:
            warns.append(
                f"repo group '{r['path']}' has no scoped identity (no includeIf gitdir: block) "
                "— falling back to the global user.name/email"
            )
        key = (r["name"], r["email"])
        if key != ("", ""):
            by_identity.setdefault(key, []).append(r["path"])
    for (name, email), paths in sorted(by_identity.items()):
        if len(paths) > 1:
            warns.append(
                f"repo groups share auth ({name} <{email}>): {', '.join(sorted(set(paths)))}"
            )
    return warns
