"""Frame release policy: the PEP 440 spec parser and the shorthand normaliser.

``desired.release_policy = {format, spec, catalog}`` is an optional, operator-signed range on a
frame grant (docs/design/frame-release-range-adr.md, section 1).  The signed record holds only
the canonical PEP 440 specifier set, so no verifier ever parses shorthand; the operator CLI
accepts npm-style shorthand and normalises it here before anything is signed.

Prereleases are admitted only when the spec names one (the ``packaging`` default).

Eligibility is deliberately not decided here: the read-time ``admits()`` predicate that judges an
evidence row against the policy is bh-vfrem.6.  :func:`satisfies` only answers "does this version
match this spec", which validation needs for ``release.id``.
"""

from __future__ import annotations

import re

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

    The read-time eligibility predicate ``admits(policy, evidence)`` (catalog membership plus
    range) belongs here and lands with bh-vfrem.6; it must not be inferred from this helper.
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
