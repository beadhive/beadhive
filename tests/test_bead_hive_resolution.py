"""Read-only exact bead-to-hive discovery for role launches."""

from __future__ import annotations


def _entry(prefix: str, repo: str | None = None) -> dict[str, str]:
    return {"provider": "github", "org": "acme", "repo": repo or prefix, "prefix": prefix}


def _cfg(*entries: dict[str, str]) -> dict:
    return {"managed_repos": list(entries)}


def test_resolve_bead_hive_finds_a_uniquely_registered_prefix(monkeypatch):
    from beadhive import bd, registry

    nvhack = _entry("nvhack")
    monkeypatch.setattr(bd, "show", lambda bead, _cwd: {"id": bead})

    result = registry.resolve_bead_hive(_cfg(nvhack), "nvhack-lvxi")

    assert result.entry == nvhack


def test_resolve_bead_hive_uses_the_full_registered_multi_hyphen_prefix(monkeypatch):
    from beadhive import bd, registry

    nvhack = _entry("nv-hack-west", "nvhack")
    monkeypatch.setattr(
        bd, "show", lambda bead, cwd: {"id": bead} if cwd.name == "nvhack" else None
    )

    result = registry.resolve_bead_hive(_cfg(nvhack), "nv-hack-west-lvxi")

    assert result.found
    assert result.entry == nvhack


def test_explicit_hive_requires_the_exact_bead(monkeypatch):
    from beadhive import bd, registry

    target = _entry("target")
    seen = []
    monkeypatch.setattr(bd, "show", lambda bead, cwd: seen.append((bead, cwd.name)) or None)

    result = registry.resolve_bead_hive(_cfg(target), "target-1", hive="target")

    assert not result.found
    assert not result.ambiguous
    assert seen == [("target-1", "target")]


def test_prefix_miss_falls_back_to_an_exact_read_across_registered_hives(monkeypatch):
    from beadhive import bd, registry

    first, nvhack = _entry("first"), _entry("nv-hack", "nvhack")
    calls = []

    def show(bead, cwd):
        calls.append(cwd.name)
        return {"id": bead} if cwd.name == "nvhack" else None

    monkeypatch.setattr(bd, "show", show)
    result = registry.resolve_bead_hive(_cfg(first, nvhack), "unrelated-lvxi")

    assert result.entry == nvhack
    assert calls == ["first", "nvhack"]


def test_ambiguous_exact_lookup_names_every_candidate_and_tolerates_unavailable_store(monkeypatch):
    from beadhive import bd, registry

    first, second, broken = _entry("one"), _entry("two"), _entry("broken")

    def show(bead, cwd):
        if cwd.name == "broken":
            raise OSError("store unavailable")
        return {"id": bead} if cwd.name in {"one", "two"} else None

    monkeypatch.setattr(bd, "show", show)
    result = registry.resolve_bead_hive(_cfg(first, second, broken), "missing-prefix-1")

    assert not result.found
    assert result.ambiguous
    assert result.candidates == (first, second)


def test_missing_exact_lookup_is_not_found_when_all_stores_are_missing_or_unavailable(monkeypatch):
    from beadhive import bd, registry

    missing, broken = _entry("missing"), _entry("broken")

    def show(_bead, cwd):
        if cwd.name == "broken":
            raise RuntimeError("dolt unavailable")
        return None

    monkeypatch.setattr(bd, "show", show)
    result = registry.resolve_bead_hive(_cfg(missing, broken), "no-prefix-1")

    assert not result.found
    assert result.candidates == ()
