"""Turn the bh export + bd `events` table into (1) a bitemporal op stream for XTDB and
(2) an independent plain-Python replay of bead state at the valid-time cuts. No XTDB needed.

Outputs (outside the repo, on disk):
    work/ops.pkl        [(recorded_at, [op, ...]), ...]  one entry per distinct recorded_at
    work/expected.json  replay projection + bd pre-image witnesses per cut, final state, sample

Mapping (layout (b) from bh-w56ts):
    created / updated / claimed / status_changed / closed / reopened / lease_reclaimed
        -> INSERT a full `beads` version with _valid_from = event created_at
    label_added / label_removed     -> INSERT / DELETE ... FOR PORTION OF VALID_TIME FROM t TO NULL
    dependency_added / _removed     -> INSERT / DELETE on `bead_edges`
    commented                       -> INSERT `bead_comments` (source 'event')
    recorded_at (SYSTEM_TIME) = event created_at; snapshot-derived rows use their occurred_at.
"""

from __future__ import annotations

import collections
import hashlib
import itertools
import json
import pickle
import random
import re

from xt import WORK, esc, load_events, load_export, to_transit, tts

SCALAR = {"updated", "claimed", "status_changed", "closed", "reopened", "lease_reclaimed"}
LABEL = {"label_added", "label_removed"}
EDGE = {"dependency_added", "dependency_removed"}
TS_FIELDS = {
    "created_at",
    "updated_at",
    "closed_at",
    "started_at",
    "lease_expires_at",
    "heartbeat_at",
    "defer_until",
}
NOT_IN_VERSION = {
    "labels",
    "dependencies",
    "comments",
    "dependency_count",
    "dependent_count",
    "comment_count",
    "_type",
}
TEXT = ["notes", "description", "design", "acceptance_criteria"]
SCALAR_CMP = ["status", "assignee", "priority", "title", "issue_type", "closed_at", "close_reason"]
CUTS = ["2026-08-21T00:00:00Z", "2026-09-18T00:00:00Z", "2026-10-16T00:00:00Z"]
PRE_WINDOW_CUT = "2026-07-20T00:00:00Z"
DEP_ADDED = re.compile(r"Added dependency: (\S+) (\S+) (\S+)$")
DEP_REMOVED = re.compile(r"Removed dependency on (\S+)$")


def pj(s):
    if not s or not s.startswith("{"):
        return None
    try:
        v = json.loads(s)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def clean(state: dict) -> dict:
    out = {}
    for k, v in state.items():
        if k in NOT_IN_VERSION or v in ("", None):
            continue
        if k == "metadata":
            if isinstance(v, str):
                v = json.loads(v) if v.strip() else {}
            if not v:
                continue
        out[k] = v
    return out


def pre_of(e):
    return clean(pj(e["old_value"])) if pj(e["old_value"]) is not None else None


def delta(e: dict, s: dict) -> dict:
    """State after event e, given the state before it."""
    s, t, typ = dict(s), e["created_at"], e["event_type"]
    if "_post" in e:
        return {**e["_post"], "updated_at": t}
    nv = pj(e["new_value"])
    if nv is not None:
        for k, v in nv.items():
            v = clean({k: v}).get(k)
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        if typ == "reopened":
            s.pop("closed_at", None)
            s.pop("close_reason", None)
        if typ == "closed":
            s["closed_at"] = e.get("_closed_at", t)
    elif typ == "closed":
        s["status"], s["closed_at"] = "closed", e.get("_closed_at", t)
        if e["new_value"]:
            s["close_reason"] = e["new_value"]
        else:
            s.pop("close_reason", None)
    elif typ == "reopened":
        s["status"] = "open"
        s.pop("closed_at", None)
        s.pop("close_reason", None)
    elif typ == "lease_reclaimed":
        s["status"] = "open"
        s.pop("assignee", None)
    elif typ == "status_changed" and e["new_value"]:
        s["status"] = e["new_value"]
    if s.get("status") == "in_progress" and "started_at" not in s:
        s["started_at"] = t
    s["updated_at"] = t
    return s


def invert(e: dict, after: dict, prior_close: str | None) -> dict:
    """State before e when e carries no pre-image (inference; declared in the spike doc)."""
    s, typ = dict(after), e["event_type"]
    if typ == "closed":
        s["status"] = "open"
        s.pop("closed_at", None)
        s.pop("close_reason", None)
    elif typ == "reopened":
        s["status"] = "closed"
        if prior_close:
            s["closed_at"] = prior_close
    elif typ == "lease_reclaimed":
        s["status"] = "in_progress"
        if e["old_value"]:
            s["assignee"] = e["old_value"]
    elif typ == "status_changed" and e["old_value"]:
        s["status"] = e["old_value"]
    return s


def closes(e) -> bool:
    nv = pj(e["new_value"])
    return e["event_type"] == "closed" or (nv is not None and nv.get("status") == "closed")


def label_of(e):
    c = e.get("comment") or ""
    for p in ("Added label: ", "Removed label: "):
        if c.startswith(p):
            return c[len(p) :]
    return (e["new_value"] if e["event_type"] == "label_added" else e["old_value"]) or None


def h(v):
    return None if v is None else hashlib.sha1(str(v).encode()).hexdigest()[:12]


def project(s: dict | None, labels, edges):
    if s is None:
        return None
    p = {k: s.get(k) for k in SCALAR_CMP}
    p.update({k: h(s.get(k)) for k in TEXT})
    p["labels"] = sorted(labels)
    p["edges"] = sorted(edges)
    return p


def order_ties(evs: list[dict]) -> tuple[list[dict], int, int]:
    """Same-bead, same-second scalar events: the file order is by event id, not causal.
    Pick the permutation whose pre-images chain best (post(a) == pre(b))."""
    out, groups, resolved = [], 0, 0
    for _, grp in itertools.groupby(evs, key=lambda e: e["created_at"]):
        grp = list(grp)
        if len(grp) == 1:
            out += grp
            continue
        groups += 1
        best, best_score = grp, None
        for perm in itertools.permutations(grp):
            score, st = 0, None
            for e in perm:  # penalise every field a later pre-image contradicts
                p = pre_of(e)
                if p is not None and st is not None:
                    score -= sum(st.get(k) != p.get(k) for k in SCALAR_CMP + TEXT)
                st = delta(e, p if p is not None else (st or {}))
            if best_score is None or score > best_score:
                best, best_score = list(perm), score
        resolved += best != grp
        out += best
    return out, groups, resolved


def bead_doc(bid: str, s: dict, vf: str, src: str) -> dict:
    doc = {"_id": esc(bid), "bd_record_type": "issue", "src": src, "_valid_from": tts(vf)}
    for k, v in s.items():
        doc[k] = tts(v) if k in TS_FIELDS else to_transit(v)
    return doc


def main() -> None:
    beads = load_export()
    events = load_events()
    ex = {b["id"]: b for b in beads}
    window_start = min(e["created_at"] for e in events)
    stats: dict = collections.Counter()
    by_bead = collections.defaultdict(list)
    for e in events:
        if e["issue_id"] in ex:
            by_bead[e["issue_id"]].append(e)
            stats["events_used_" + e["event_type"]] += 1
        else:
            stats["events_skipped_bead_not_in_export"] += 1
    ops = collections.defaultdict(list)  # recorded_at -> [op]
    seq = itertools.count()
    expected = {c: {} for c in [*CUTS, PRE_WINDOW_CUT]}
    witness = {c: {} for c in CUTS}
    final = {}
    drift_fields = collections.Counter()
    drift_examples = []  # (bead, event, field, len replay, len bd, equal ignoring whitespace)

    def emit(ts, op):
        ops[ts].append((next(seq), op))

    for bid, b in ex.items():
        evs = sorted(by_bead[bid], key=lambda e: e["created_at"])  # stable: keeps id order in ties
        t0 = min([b["created_at"], *(e["created_at"] for e in evs)])
        stats[
            "beads_created_before_window" if t0 < window_start else "beads_created_in_window"
        ] += 1
        if any(e["event_type"] == "created" for e in evs):
            stats["beads_with_created_event"] += 1
        scal, g, r = order_ties([e for e in evs if e["event_type"] in SCALAR])
        # payload-less `updated` events (old = new = ''): bd recorded THAT something changed, not
        # what. The next scalar event's pre-image is bd's snapshot of the state after it.
        keep = []
        for i, e in enumerate(scal):
            if e["event_type"] == "updated" and not e["old_value"] and not e["new_value"]:
                nxt = scal[i + 1] if i + 1 < len(scal) else None
                if nxt is not None and pre_of(nxt) is not None:
                    e = {**e, "_post": pre_of(nxt)}
                    stats["empty_updated_post_from_next_preimage"] += 1
                else:
                    stats["empty_updated_ignored_no_next_preimage"] += 1
                    continue
            keep.append(e)
        scal = keep
        # closed_at precision: the event's created_at is truncated to the second, bd's own
        # closed_at is often 1 s later. Prefer bd's value: the next pre-image, else the export.
        for i, e in enumerate(scal):
            if closes(e):
                nxt = scal[i + 1] if i + 1 < len(scal) else None
                p = pre_of(nxt) if nxt is not None else None
                if (
                    p is not None
                    and p.get("closed_at")
                    and abs(_secs(p["closed_at"]) - _secs(e["created_at"])) <= 2
                ):
                    scal[i] = {**e, "_closed_at": p["closed_at"]}
                elif (
                    nxt is None
                    and b.get("closed_at")
                    and abs(_secs(b["closed_at"]) - _secs(e["created_at"])) <= 2
                ):
                    scal[i] = {**e, "_closed_at": b["closed_at"]}
                stats["closed_at_taken_from_bd_value"] += "_closed_at" in scal[i]
        stats["scalar_tie_groups"] += g
        stats["scalar_tie_groups_reordered_by_preimage_chain"] += r
        # synthesize a close from the snapshot when no closed event explains closed_at
        if b.get("closed_at") and not any(
            closes(e) and abs(_secs(e["created_at"]) - _secs(b["closed_at"])) <= 2 for e in scal
        ):
            synth = {
                "event_type": "closed",
                "created_at": b["closed_at"],
                "id": "synth-close",
                "new_value": b.get("close_reason", ""),
                "old_value": "",
                "synthetic": True,
            }
            stats[
                "closes_synthesized_from_snapshot_"
                + ("pre_window" if b["closed_at"] < window_start else "in_window")
            ] += 1
            scal = sorted([*scal, synth], key=lambda e: e["created_at"])
        # backward pass: seed state at t0
        s = clean(b)
        last_close = {}
        prev_close = None
        for e in scal:
            last_close[id(e)] = prev_close
            if e["event_type"] == "closed":
                prev_close = e["created_at"]
        for e in reversed(scal):
            p = pre_of(e)
            s = p if p is not None else invert(e, s, last_close[id(e)])
        seed = dict(s)
        seed.setdefault("created_at", b["created_at"])
        seed["updated_at"] = t0
        if not any(pre_of(e) is not None for e in scal):
            stats["beads_without_any_preimage"] += 1
        # forward pass: one version per scalar event
        versions = [(t0, seed, "seed")]
        s = seed
        for e in scal:
            p = pre_of(e)
            if p is not None:
                stats["preimage_reanchor_checks"] += 1
                for k in SCALAR_CMP + TEXT:
                    if s.get(k) != p.get(k):
                        drift_fields[k] += 1
                        a, z = str(s.get(k)), str(p.get(k))
                        drift_examples.append(
                            (bid, e["event_type"], k, len(a), len(z), a.split() == z.split())
                        )
                s = p
            s = delta(e, s)
            versions.append(
                (e["created_at"], s, e["id"] if not e.get("synthetic") else "synth-close")
            )
        for vf, st, src in versions:
            emit(vf, ("ins", "beads", bead_doc(bid, st, vf, src)))
        stats["bead_versions"] += len(versions)
        final_scalar = s

        # labels
        lev = collections.defaultdict(list)
        for e in evs:
            if e["event_type"] in LABEL and (lbl := label_of(e)):
                lev[lbl].append(e)
        cur_labels = set(b.get("labels", []))
        label_init = set()
        for lbl in set(lev) | cur_labels:
            first = lev[lbl][0]["event_type"] if lev[lbl] else None
            if first == "label_removed" or (first is None and lbl in cur_labels):
                label_init.add(lbl)
        held = set()
        for lbl in sorted(label_init):
            emit(
                t0,
                (
                    "ins",
                    "bead_labels",
                    {
                        "_id": esc(f"{bid}|{lbl}"),
                        "bead_id": bid,
                        "label": esc(lbl),
                        "_valid_from": tts(t0),
                    },
                ),
            )
            held.add(lbl)
            stats["label_rows_from_snapshot"] += 1
        for e in sorted((e for es in lev.values() for e in es), key=lambda e: e["created_at"]):
            lbl, t = label_of(e), e["created_at"]
            if e["event_type"] == "label_added":
                if lbl in held:
                    stats["label_add_when_held_noop"] += 1
                    continue
                held.add(lbl)
                emit(
                    t,
                    (
                        "ins",
                        "bead_labels",
                        {
                            "_id": esc(f"{bid}|{lbl}"),
                            "bead_id": bid,
                            "label": esc(lbl),
                            "_valid_from": tts(t),
                        },
                    ),
                )
                stats["label_insert_ops"] += 1
            else:
                if lbl not in held:
                    stats["label_remove_when_absent_noop"] += 1
                    continue
                held.discard(lbl)
                emit(t, ("del", "bead_labels", f"{bid}|{lbl}", t))
                stats["label_delete_ops"] += 1
        stats["final_label_mismatch_beads"] += held != cur_labels

        # edges (outgoing)
        cur_edges = {
            (d["type"], d["depends_on_id"]): d
            for d in b.get("dependencies", [])
            if d["issue_id"] == bid
        }
        eev = []
        for e in evs:
            if e["event_type"] == "dependency_added":
                m = DEP_ADDED.match(e["new_value"] or "")
                if m and m.group(1) == bid:
                    eev.append((e["created_at"], "add", (m.group(2), m.group(3)), e))
                else:
                    stats["dependency_added_unparsed_or_other_side"] += 1
            elif e["event_type"] == "dependency_removed":
                m = DEP_REMOVED.match(e["new_value"] or "")
                if m:
                    eev.append((e["created_at"], "remove", m.group(1), e))
        known = set(cur_edges) | {k for _, kind, k, _ in eev if kind == "add"}
        edge_init = set()
        for key in known:
            first = next(
                (
                    x
                    for x in eev
                    if (x[1] == "add" and x[2] == key) or (x[1] == "remove" and x[2] == key[1])
                ),
                None,
            )
            if first is None and key in cur_edges:
                edge_init.add(key)
            elif first is not None and first[1] == "remove" and key in cur_edges:
                edge_init.add(key)
        eheld = set()

        def edge_doc(key, vf, e=None, bid=bid, cur_edges=cur_edges):
            typ, dst = key
            d = cur_edges.get(key)
            doc = {
                "_id": esc(f"{bid}|{typ}|{dst}"),
                "from_id": bid,
                "type": typ,
                "to_id": esc(dst),
                "_valid_from": tts(vf),
            }
            if d is not None:
                doc.update(
                    created_by=esc(d["created_by"]),
                    created_at=tts(d["created_at"]),
                    metadata=esc(d["metadata"]),
                )
            elif e is not None:
                doc.update(created_by=esc(e["actor"]), created_at=tts(e["created_at"]))
            return doc

        edge_init_at = {
            k: (
                t0
                if any(x[1] == "remove" and x[2] == k[1] for x in eev)
                else max(cur_edges[k]["created_at"], t0)
            )
            for k in edge_init
        }
        for key in sorted(edge_init):
            emit(edge_init_at[key], ("ins", "bead_edges", edge_doc(key, edge_init_at[key])))
            eheld.add(key)
            stats["edge_rows_from_snapshot"] += 1
        for t, kind, key, e in eev:
            if kind == "add":
                if key in eheld:
                    stats["edge_add_when_held_noop"] += 1
                    continue
                eheld.add(key)
                emit(t, ("ins", "bead_edges", edge_doc(key, t, e)))
                stats["edge_insert_ops"] += 1
            else:
                hits = [k for k in eheld if k[1] == key]
                if not hits:
                    if any(k[1] == key for k in known):
                        stats["edge_remove_when_absent_noop"] += 1
                    else:
                        stats["edge_remove_unresolved_type_unknown"] += 1
                for k in hits:
                    eheld.discard(k)
                    emit(t, ("del", "bead_edges", f"{bid}|{k[0]}|{k[1]}", t))
                    stats["edge_delete_ops"] += 1
        stats["final_edge_mismatch_beads"] += eheld != set(cur_edges)

        # comments
        for c in b.get("comments", []):
            emit(
                c["created_at"],
                (
                    "ins",
                    "bead_comments",
                    {
                        "_id": c["id"],
                        "bead_id": bid,
                        "author": esc(c["author"]),
                        "text": esc(c["text"]),
                        "created_at": tts(c["created_at"]),
                        "source": "export",
                        "_valid_from": tts(c["created_at"]),
                    },
                ),
            )
            stats["comment_rows_export"] += 1
        for e in evs:
            if e["event_type"] == "commented":
                emit(
                    e["created_at"],
                    (
                        "ins",
                        "bead_comments",
                        {
                            "_id": e["id"],
                            "bead_id": bid,
                            "author": esc(e["actor"]),
                            "text": esc(e["comment"]),
                            "created_at": tts(e["created_at"]),
                            "source": "event",
                            "_valid_from": tts(e["created_at"]),
                        },
                    ),
                )
                stats["comment_rows_event"] += 1

        # ---------------- independent replay at the cuts (plain sets and dicts, no periods)
        for cut in [*CUTS, PRE_WINDOW_CUT]:
            if t0 > cut:
                expected[cut][bid] = None
                continue
            st = seed
            for e in scal:
                if e["created_at"] > cut:
                    break
                p = pre_of(e)
                st = delta(e, p if p is not None else st)
            labs = set(label_init)
            for e in sorted((e for es in lev.values() for e in es), key=lambda e: e["created_at"]):
                if e["created_at"] <= cut:
                    (labs.add if e["event_type"] == "label_added" else labs.discard)(label_of(e))
            eds = {k for k in edge_init if edge_init_at[k] <= cut}
            for t, kind, key, _ in eev:
                if t <= cut:
                    if kind == "add":
                        eds.add(key)
                    else:
                        eds = {k for k in eds if k[1] != key}
            expected[cut][bid] = project(st, labs, {f"{k[0]}|{k[1]}" for k in eds})
        # ---------------- bd's own witness: a pre-image after the cut that nothing in between
        # could have changed. Scalars are witnessed if no scalar event lies between the cut and
        # the pre-imaged event; labels if no label event does. Ties in that second disqualify.
        for cut in CUTS:
            if t0 > cut:
                continue
            touched = set()
            for n in (e for e in evs if e["created_at"] > cut):
                same = [e for e in evs if e["created_at"] == n["created_at"] and e is not n]
                pre = pj(n["old_value"])
                if pre is not None and not same:
                    w = {"event": n["event_type"], "at": n["created_at"]}
                    if "scalar" not in touched:
                        w.update(
                            {
                                k: (None if pre.get(k) == "" else pre.get(k))
                                for k in ["status", "assignee", "priority", "title", "issue_type"]
                            }
                        )
                    if "label" not in touched:
                        w["labels"] = sorted(pre.get("labels") or [])
                    if len(w) > 2:
                        witness[cut][bid] = w
                    break
                kind = (
                    "scalar"
                    if n["event_type"] in SCALAR
                    else "label"
                    if n["event_type"] in LABEL
                    else None
                )
                if kind:
                    touched.add(kind)
                if same:
                    touched |= {
                        "scalar" if e["event_type"] in SCALAR else "label"
                        for e in same
                        if e["event_type"] in SCALAR | LABEL
                    }
                if touched >= {"scalar", "label"}:
                    break
        final[bid] = project(final_scalar, held, {f"{k[0]}|{k[1]}" for k in eheld})
        final[bid]["export"] = project(clean(b), cur_labels, {f"{k[0]}|{k[1]}" for k in cur_edges})

    # ---------------- sample >= 50 beads, stratified, deterministic
    rng = random.Random("bh-3jp2g")
    n_scal = {bid: sum(e["event_type"] in SCALAR for e in by_bead[bid]) for bid in ex}
    pre_w = sorted(b for b in ex if ex[b]["created_at"] < window_start and by_bead[b])
    in_w = sorted(b for b in ex if ex[b]["created_at"] >= window_start and n_scal[b] >= 3)
    sample = rng.sample(pre_w, 20) + rng.sample(in_w, 40)

    tx = sorted(ops.items())
    assert all(a[0] < b[0] for a, b in zip(tx, tx[1:], strict=False)), (
        "recorded_at must strictly increase"
    )
    stream = [(ts, [op for _, op in sorted(lst)]) for ts, lst in tx]
    WORK.mkdir(parents=True, exist_ok=True)
    with open(WORK / "ops.pkl", "wb") as fh:
        pickle.dump(stream, fh)
    with open(WORK / "expected.json", "w") as fh:
        json.dump(
            {
                "cuts": CUTS,
                "pre_window_cut": PRE_WINDOW_CUT,
                "expected": expected,
                "witness": witness,
                "final": final,
                "sample": sample,
                "window_start": window_start,
            },
            fh,
        )

    per_table = collections.Counter()
    for _, lst in stream:
        for op in lst:
            per_table[f"{op[0]}:{op[1]}"] += 1
    stats["transactions_distinct_recorded_at"] = len(stream)
    stats["ops_total"] = sum(per_table.values())
    print("# history.py")
    print(
        json.dumps(
            {
                "window_start": window_start,
                "first_recorded": stream[0][0],
                "last_recorded": stream[-1][0],
                **dict(sorted(stats.items())),
                "ops_by_kind_table": dict(sorted(per_table.items())),
                "preimage_drift_by_field": dict(drift_fields),
                "preimage_drift_cases": drift_examples,
                "largest_tx_ops": max(len(x) for _, x in stream),
                "witnesses_per_cut": {
                    c: {
                        "beads": len(witness[c]),
                        "scalar": sum("status" in w for w in witness[c].values()),
                        "labels": sum("labels" in w for w in witness[c].values()),
                    }
                    for c in CUTS
                },
                "beads_existing_per_cut": {
                    c: sum(v is not None for v in expected[c].values()) for c in expected
                },
                "sample_size": len(sample),
            },
            indent=2,
        )
    )


def _secs(s: str) -> float:
    from xt import parse_ts

    return parse_ts(s).timestamp()


if __name__ == "__main__":
    main()
