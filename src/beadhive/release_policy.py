"""Frame release policy: the PEP 440 spec parser and the shorthand normaliser.

``desired.release_policy = {format, spec, catalog}`` is an optional, operator-signed range on a
frame grant (docs/design/frame-release-range-adr.md, section 1).  The signed record holds only
the canonical PEP 440 specifier set, so no verifier ever parses shorthand; the operator CLI
accepts npm-style shorthand and normalises it here before anything is signed.

Prereleases are admitted only when the spec names one (the ``packaging`` default).

:func:`admits` is the read-time eligibility predicate both liveness carriers share (ADR section
4): it judges a measured release against the grant and the catalog row for its digest.
:func:`satisfies` only answers "does this version match this spec", which validation needs for
``release.id``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

POLICY_FORMAT = 1
POLICY_CATALOG = "beadhive"
POLICY_KEYS = frozenset({"format", "spec", "catalog"})

_ORDER = {">=": 0, ">": 1, "~=": 2, "==": 3, "===": 4, "!=": 5, "<=": 6, "<": 7}
_NUM = r"\d+"
_PARTIAL = rf"{_NUM}(?:\.{_NUM}){{0,2}}"
_WILD = re.compile(rf"^({_NUM}(?:\.{_NUM}){{0,1}})\.(?:x|X|\*)$")
_MAJOR_WILD = re.compile(rf"^({_NUM})\.(?:x|X|\*)$")


class ReleasePolicyError(ValueError):
    """An unparseable or unacceptable release policy."""


def canonicalize(spec: str) -> str:
    """Return the canonical comma-joined form of an already-PEP 440 spec.

    Clauses are de-duplicated, whitespace-free, and ordered lower bound, equality, then upper
    bound (so ``>=0.28.2,==0.28.*``), which is what the ADR table stores.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ReleasePolicyError("release policy spec is empty")
    try:
        parsed = SpecifierSet(spec)
    except InvalidSpecifier:
        raise ReleasePolicyError(
            f"release policy spec is not a PEP 440 specifier set: {spec!r}"
        ) from None
    clauses = {(s.operator, s.version) for s in parsed}
    if not clauses:
        raise ReleasePolicyError("release policy spec is empty")
    ordered = sorted(clauses, key=lambda c: (_ORDER[c[0]], c[1]))
    return ",".join(f"{op}{ver}" for op, ver in ordered)


def is_canonical(spec: str) -> bool:
    try:
        return canonicalize(spec) == spec
    except ReleasePolicyError:
        return False


def _bump(partial: str) -> str:
    parts = [int(p) for p in partial.split(".")]
    parts[-1] += 1
    return ".".join(str(p) for p in parts)


def _parts(partial: str) -> list[int]:
    return [int(p) for p in partial.split(".")]


def _caret(partial: str) -> str:
    parts = _parts(partial)
    if parts[0] == 0:
        if len(parts) == 1:
            raise ReleasePolicyError("'^0' is ambiguous; name the minor (^0.28.2) or use 0.x")
        if parts[1] == 0:
            raise ReleasePolicyError(f"'^{partial}' pins a patch; use an exact == pin instead")
        return f">={partial},=={parts[0]}.{parts[1]}.*"
    floor = f">={partial}," if len(parts) > 1 else ""
    return f"{floor}=={parts[0]}.*"


def _tilde(partial: str) -> str:
    parts = _parts(partial)
    if len(parts) == 1:
        return f"=={parts[0]}.*"
    if len(parts) == 2:
        return f"=={parts[0]}.{parts[1]}.*"
    return f">={partial},=={parts[0]}.{parts[1]}.*"


def normalize(text: str) -> str:
    """Normalise operator input (shorthand or PEP 440) to the canonical PEP 440 spec."""
    if not isinstance(text, str) or not text.strip():
        raise ReleasePolicyError("release policy spec is empty")
    s = text.strip()
    range_match = re.fullmatch(rf"({_PARTIAL})\s+-\s+({_PARTIAL})", s)
    if range_match:
        low, high = range_match.groups()
        upper = f"<={high}" if len(_parts(high)) == 3 else f"<{_bump(high)}"
        return canonicalize(f">={low},{upper}")
    if re.fullmatch(rf"\^{_PARTIAL}", s):
        return canonicalize(_caret(s[1:]))
    if re.fullmatch(rf"~{_PARTIAL}", s):
        return canonicalize(_tilde(s[1:]))
    major = _MAJOR_WILD.fullmatch(s)
    if major:
        return canonicalize(f"=={major.group(1)}.*")
    wild = _WILD.fullmatch(s)
    if wild:
        return canonicalize(f"=={wild.group(1)}.*")
    if re.fullmatch(r"[xX*]", s):
        raise ReleasePolicyError("an unbounded release policy is refused; name a major or minor")
    return canonicalize(s)


def satisfies(spec: str, version: str) -> bool:
    """Whether ``version`` matches ``spec`` (prereleases only if the spec names one).

    Range only: catalog membership is :func:`admits`, which must not be inferred from this.
    """
    try:
        parsed = SpecifierSet(spec)
        # Explicit, not the library default, which differs across packaging releases.
        names_prerelease = any(_is_prerelease(c.version) for c in parsed)
        return parsed.contains(Version(version), prereleases=names_prerelease)
    except (InvalidSpecifier, InvalidVersion):
        return False


def _is_prerelease(literal: str) -> bool:
    try:
        return Version(literal.removesuffix(".*")).is_prerelease
    except InvalidVersion:
        return False


def policy_for(spec: str) -> dict:
    """The record value for an already-normalised spec."""
    return {"format": POLICY_FORMAT, "spec": canonicalize(spec), "catalog": POLICY_CATALOG}


def validate_policy(policy, release_id) -> None:
    """Reject anything but a canonical, format-1 policy that admits the reference release."""
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        raise ValueError("invalid release policy fields")
    if type(policy["format"]) is not int or policy["format"] != POLICY_FORMAT:
        raise ValueError("unsupported release policy format")
    if policy["catalog"] != POLICY_CATALOG:
        raise ValueError("unknown release policy catalog")
    spec = policy["spec"]
    if not isinstance(spec, str) or not is_canonical(spec):
        raise ValueError("release policy spec is not a canonical PEP 440 specifier set")
    if not isinstance(release_id, str) or not satisfies(spec, release_id):
        raise ValueError("reference release does not satisfy release policy")


def _probes(spec: str) -> list[Version]:
    probes = {Version("0")}
    for clause in SpecifierSet(spec):
        literal = clause.version.removesuffix(".*")
        try:
            base = Version(literal)
        except InvalidVersion:
            continue
        probes.add(base)
        release = list(base.release)
        release[-1] += 1
        probes.add(Version(".".join(str(p) for p in release)))
    return sorted(probes)


def downgrades(spec: str, attested: str | None) -> bool:
    """True when ``spec`` excludes the attested version yet still admits a lower one."""
    if not attested:
        return False
    try:
        current = Version(attested)
    except InvalidVersion:
        return False
    if satisfies(spec, attested):
        return False
    return any(p < current and satisfies(spec, str(p)) for p in _probes(spec))


def catalog_row_for(
    rows: Iterable[Sequence], catalog: str, measured: Mapping[str, str]
) -> dict | None:
    """Fold raw catalog rows into the one row :func:`admits` judges, or ``None`` when absent.

    ``rows`` are ``(catalog, version, measured_digest, status, algorithm)`` tuples: every row of
    each version that holds ``measured['digest']``, read ``AS OF`` the verified runtime head.
    Revocation is a tombstone (a ``revoked`` row for the same digest), so a digest is active only
    with an ``active`` row and no tombstone.  ``active_digests`` is how many digests of that
    version are effectively active; the catalog allows one, and :func:`admits` refuses more.
    A row whose version differs from ``measured['id']`` is returned unchanged so ``admits``
    refuses the disagreement rather than this lookup hiding it.
    """
    mine = [tuple(r) for r in rows if r[0] == catalog]
    digest, version = measured.get("digest"), measured.get("id")
    versions = sorted({r[1] for r in mine if r[2] == digest})
    if not versions:
        return None
    chosen = version if version in versions else versions[0]
    scoped = [r for r in mine if r[1] == chosen]
    tombstoned = {r[2] for r in scoped if r[3] == "revoked"}
    active = {r[2] for r in scoped if r[3] == "active"} - tombstoned
    algorithm = next((r[4] for r in scoped if r[2] == digest and r[3] == "active"), "")
    return {
        "catalog": catalog,
        "version": chosen,
        "measured_digest": digest,
        "status": "active" if digest in active else "revoked",
        "algorithm": algorithm,
        "active_digests": len(active),
    }


def admits(measured, desired, catalog_row) -> bool:
    """Whether ``measured`` (``{id, digest}``) satisfies the release the grant ``desired`` names.

    Without ``desired['release_policy']`` this is today's exact pin and ``catalog_row`` is
    ignored.  With one, all of these hold (ADR section 4; the epoch conjunct stays with the
    carrier): the row is the catalog's own, ``active`` and for this digest; the measurement
    algorithm is the one the catalog row names; the self-reported id equals the row's version;
    that version has exactly one active digest; and the version satisfies the canonical spec.
    """
    if not isinstance(measured, Mapping) or not isinstance(desired, Mapping):
        return False
    policy = desired.get("release_policy")
    if policy is None:
        return dict(measured) == desired.get("release")
    row = catalog_row
    try:
        from .release_measurement import ALGORITHM

        return (
            isinstance(row, Mapping)
            and isinstance(policy, Mapping)
            and row.get("catalog") == policy.get("catalog")
            and row.get("status") == "active"
            and row.get("algorithm") == ALGORITHM
            and row.get("active_digests") == 1
            and row.get("measured_digest") == measured.get("digest")
            and row.get("version") == measured.get("id")
            and isinstance(policy.get("spec"), str)
            and satisfies(policy["spec"], str(row.get("version")))
        )
    except (TypeError, ValueError):
        return False
