"""fence_audit's pure parts (bh-uz46l): the I3-style history check and the report.

The engine-backed audit (stale_marks / epoch_regressed / placement_ahead / late writes on a real
Dolt remote, including the bh-uhx2r E2 re-stamp) is in ``tests/test_fence_data_int.py``.
"""

from __future__ import annotations

from beadhive import fence_audit as fa

H = {name: (name * 32)[:32] for name in "abcdefghijklmnopqrstuv"}


def c(name: str, *parents: str, message: str = "") -> fa.Commit:
    return fa.Commit(H[name], tuple(H[p] for p in parents), message or name)


def window(*commits: fa.Commit) -> dict[str, fa.Commit]:
    return {x.hash: x for x in commits}


def test_writes_after_the_adopt_at_the_live_epoch_are_not_late():
    # anchor A (adopt @2) -- b -- c, all stamped 2
    w = window(c("b", "a"), c("c", "b"))
    epochs = {H["a"]: 2, H["b"]: 2, H["c"]: 2}
    assert fa.find_late_writes(w, epochs, {H["a"]: 2}) == []


def test_bh_uhx2r_e2_restamped_stale_commit_is_a_late_write():
    """E2: a stale commit S (epoch 1, a sibling of the adopt A @2) is force-merged into main by
    M, its marks re-stamped; current-state audit is clean, history is not."""
    w = window(
        c("s", "g", message="stale forwarded write"),
        c("m", "s", "a", message="routine"),
        c("d", "m"),
    )
    epochs = {H["g"]: 1, H["s"]: 1, H["a"]: 2, H["m"]: 2, H["d"]: 2}
    late = fa.find_late_writes(w, epochs, {H["a"]: 2})
    assert [(x.commit, x.epoch, x.adopt_commit, x.adopt_epoch) for x in late] == [
        (H["s"], 1, H["a"], 2)
    ]
    assert late[0].merged_by == ""  # S is M's FIRST parent: main itself was the stale line
    assert "entered main after the epoch-2 adopt" in late[0].describe()


def test_a_stale_side_branch_merged_in_names_its_merge():
    w = window(c("s", "g"), c("m", "a", "s", message="merge it"))
    epochs = {H["g"]: 1, H["s"]: 1, H["a"]: 2, H["m"]: 2}
    late = fa.find_late_writes(w, epochs, {H["a"]: 2})
    assert [(x.commit, x.merged_by) for x in late] == [(H["s"], H["m"])]


def test_the_writers_deliberate_orphan_merge_is_sanctioned():
    w = window(
        c("s", "g", message="orphaned write"),
        c("m", "a", "s", message=f"{fa.MERGE_PREFIX}p/orphan-1-abc"),
    )
    epochs = {H["g"]: 1, H["s"]: 1, H["a"]: 2, H["m"]: 2}
    assert fa.find_late_writes(w, epochs, {H["a"]: 2}) == []


def test_an_unfenced_commit_merged_after_the_adopt_is_late():
    w = window(c("s", "g"), c("m", "a", "s"))
    epochs = {H["g"]: None, H["s"]: None, H["a"]: 2, H["m"]: 2}
    late = fa.find_late_writes(w, epochs, {H["a"]: 2})
    assert [(x.commit, x.epoch) for x in late] == [(H["s"], None)]
    assert "unfenced" in late[0].describe()


def test_a_wider_window_judges_every_later_bump():
    """since_epoch below live: commits at epoch 1 below the in-window bump to 2 are fine; one
    beside it is late against that bump."""
    w = window(
        c("b", "a"),  # epoch 1 write, before the bump
        c("e", "b", message="bh: adopt q@2"),  # bump to 2 inside the window
        c("s", "b"),  # epoch 1 write beside the bump
        c("m", "e", "s"),
    )
    epochs = {H["a"]: 1, H["b"]: 1, H["e"]: 2, H["s"]: 1, H["m"]: 2}
    late = fa.find_late_writes(w, epochs, {H["a"]: 1})
    assert [(x.commit, x.adopt_commit) for x in late] == [(H["s"], H["e"])]


def test_report_findings_and_ok():
    clean = fa.FenceAudit(
        ref="origin/main",
        head=H["a"],
        cut_over=True,
        writer_frame="b",
        writer_epoch=2,
        live_epoch=2,
        history_max_epoch=2,
        placement_frame="b",
        placement_epoch=2,
        adopt_commits=(H["a"],),
    )
    assert clean.ok and clean.findings() == []
    dirty = fa.FenceAudit(
        ref="origin/main",
        head=H["a"],
        cut_over=True,
        writer_frame="b",
        writer_epoch=2,
        live_epoch=2,
        stale_mark_count=1,
        stale_marks=(fa.StaleMark("x", 1, "issues"),),
        history_max_epoch=3,
        history_max_commit=H["c"],
        placement_frame="c",
        placement_epoch=4,
        adopt_commits=(),
    )
    kinds = [f.split(":")[0] for f in dirty.findings()]
    assert kinds == ["stale_marks", "epoch_regressed", "placement_ahead", "history_unanchored"]
    assert dirty.as_dict()["epoch_regressed"] and not dirty.ok
    legacy = fa.FenceAudit(ref="origin/main", head=H["a"], cut_over=False, placement_epoch=9)
    assert legacy.ok and not legacy.placement_ahead


def test_history_window_finds_the_adopt_and_excludes_its_ancestry():
    """fenced = {commit: (epoch, parents)}; ``g`` is the cutover (parent ``a`` unfenced)."""
    fenced = {
        H["g"]: (1, (H["a"],)),
        H["b"]: (1, (H["g"],)),
        H["s"]: (1, (H["b"],)),  # stale, beside the adopt
        H["e"]: (2, (H["b"],)),  # the epoch-2 adopt
        H["m"]: (2, (H["s"], H["e"])),
        H["n"]: (2, (H["m"], H["u"])),  # merges in unfenced history u
    }
    window, anchors, unfenced = fa.history_window(fenced, H["n"], 2)
    assert anchors == {H["e"]: 2}
    assert set(window) == {H["s"], H["m"], H["n"]}
    assert unfenced == {H["u"]: H["n"]}
    _, cutover, _ = fa.history_window(fenced, H["n"], 1)
    assert cutover == {H["g"]: 1}  # first parent unfenced: the cutover anchors epoch 1
