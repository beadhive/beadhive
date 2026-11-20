"""Session renew then evidence, end to end through the HQ port (bh-zephr).

ONE test body runs against two backends behind one fixture (``tests/harness/hq_session_stack.py``):

* ``fake`` (always runs): an in-process fake of hq-service's session routes
  (``tests/harness/fake_hq_service.py``) built from the hq-service conformance corpus at 205fd5b
  and its ``store/session.go`` rules, on a mocked clock. Nothing sleeps, no network is touched.
* ``staged`` (opt-in, skipped by default): a real STAGED hq-service. Marked
  ``staged_hq_service`` and skipped unless ``BH_STAGED_HQ_SERVICE=1`` plus the variables named in
  ``harness.hq_session_stack.STAGED_ENV`` are set. This is an OPERATOR step: nothing in the normal
  gate or in CI reaches a staged stack. How to run it: docs/TEST_PROCESS_POLICY.md,
  "Staged hq-service session test (bh-zephr)".

Everything below the HTTP transport is production code: ``heartbeat_sender`` (timer tick and
conformance job), ``heartbeat_report`` (renew-first ordering, the retry), the hq-service adapter
and the client-owned renew seq. Only this frame's grant reads (the anchor and the beat generator)
are stubbed; the host is ``hq.route: api`` and has no SQL frame runtime (bh-65s71).

The assertions are the bh-a3369 review notes: (1) evidence is accepted straight after the
conformance process's own renew; (2) a cold incarnation posts nothing before its renew is accepted;
(3) a forced ``no_current_beat`` takes exactly one renew-and-retry; (4) the renew timer and the
conformance job interleaved do not produce ``evidence_digest_unmatched`` or ``seq_not_advanced``;
(5) an api host with no SQL frame runtime renews and posts evidence (bh-65s71, formerly the G1
refusal).

Interleaving (4): two actors, advanced step by step on a mocked clock. Each step is a whole timer
tick or a whole conformance run; the timer is one long-lived process and every conformance run is a
fresh cold process (as systemd runs them), sharing the per-host seq state directory. The staged run
is the same body bounded to one fixed sequential interleaving (a real service's floor is shared
state, so exhaustively replaying orderings on it would not be repeatable). The overlap INSIDE one
renew (two processes with no cross-process lock, bh-ktd2x) cannot be driven on a real stack and is
documented against the fake below.
"""

from __future__ import annotations

import hashlib
import itertools
import json

import pytest

from beadhive import heartbeat_report as report
from beadhive.hq_port import HqNoCurrentBeat, HqRefused, HqUnavailable
from harness.hq_session_stack import SessionStack, staged_skip_reason

FORBIDDEN_IN_STEADY_STATE = {"evidence_digest_unmatched", "seq_not_advanced"}
OK = ("accepted", "replayed")


@pytest.fixture(autouse=True)
def clean_acceptance():
    report._ACCEPTED.clear()
    yield
    report._ACCEPTED.clear()


@pytest.fixture(
    params=["fake", pytest.param("staged", marks=pytest.mark.staged_hq_service)],
)
def make_stack(request, tmp_path, monkeypatch):
    """A factory of stacks (``make_stack(n)`` is a fresh one) over the selected backend."""
    if request.param == "staged":
        reason = staged_skip_reason()
        if reason:
            pytest.skip(reason)

    def factory(index: int = 0) -> SessionStack:
        return SessionStack(request.param, tmp_path / f"stack-{index}", monkeypatch)

    factory.mode = request.param
    return factory


@pytest.fixture
def stack(make_stack):
    return make_stack()


def _renew_beat(entry: dict) -> dict:
    return json.loads(entry["body"])["spec"]


# ---- (1) evidence is accepted straight after the process's own renew -----------------------


def test_conformance_evidence_is_accepted_straight_after_its_own_renew(stack):
    stack.conformance_run(tag="c1")
    assert stack.kinds() == ["renew", "evidence"]
    renew, evidence = stack.log
    assert renew["outcome"] in OK and evidence["outcome"] in OK
    # the evidence post is the report the accepted beat digested
    posted = "sha256:" + hashlib.sha256(evidence["body"]).hexdigest()
    assert posted == _renew_beat(renew)["report_digest"]


# ---- (2) a cold incarnation posts nothing before its renew is accepted ---------------------


def test_a_cold_incarnation_posts_nothing_before_its_renew_is_accepted(stack):
    stack.fault("renew")  # the first renew is not accepted (outage)
    with pytest.raises(HqUnavailable):
        stack.conformance_run(tag="c1")
    assert stack.kinds() == ["renew"]  # nothing else was posted
    assert report._ACCEPTED == {}
    stack.advance(5)
    stack.conformance_run(tag="c1")  # a new cold process
    kinds = stack.kinds()
    assert kinds == ["renew", "renew", "evidence"]
    assert stack.log[1]["outcome"] in OK  # accepted, and only then evidence
    assert stack.log[2]["outcome"] in OK


def test_a_refused_renew_posts_nothing_and_is_not_retried(stack):
    stack.fault("renew", "identity_mismatch")
    with pytest.raises(HqRefused) as refused:
        stack.conformance_run(tag="c1")
    assert refused.value.code == "identity_mismatch"
    assert stack.kinds() == ["renew"]


# ---- (3) a forced no_current_beat is one renew and one retry -------------------------------


def test_a_forced_no_current_beat_takes_exactly_one_renew_and_retry(stack):
    process = stack.new_process()
    stack.conformance_run(tag="c1", process=process)  # warm: this process holds an accepted beat
    stack.reset_log()
    stack.advance(5)
    stack.fault("evidence", "no_current_beat")  # the service holds no current beat
    stack.conformance_run(tag="c2", process=process)
    assert stack.kinds() == ["evidence", "renew", "evidence"]
    refused, renew, retried = stack.log
    assert refused["code"] == "no_current_beat"
    assert renew["outcome"] in OK and retried["outcome"] in OK
    # the retry posts the report the newly accepted beat digested
    assert (
        "sha256:" + hashlib.sha256(retried["body"]).hexdigest()
        == _renew_beat(renew)["report_digest"]
    )


def test_a_second_no_current_beat_is_raised_after_one_retry(stack):
    process = stack.new_process()
    stack.conformance_run(tag="c1", process=process)
    stack.reset_log()
    stack.advance(5)
    stack.fault("evidence", "no_current_beat")
    stack.fault("evidence", "no_current_beat")
    with pytest.raises(HqNoCurrentBeat):
        stack.conformance_run(tag="c2", process=process)
    assert stack.kinds() == ["evidence", "renew", "evidence"]  # no loop


# ---- (4) timer and conformance job interleaved: steady state is clean ----------------------

ORDERINGS = sorted(set(itertools.permutations("TTCC")))
STAGED_ORDERING = [("T", "C", "T", "C")]


def _interleave(stack: SessionStack, order: tuple[str, ...]) -> None:
    timer = stack.new_process()  # the renew timer: one long-lived process
    stack.timer_tick(timer)  # steady state starts from an accepted renew
    stack.advance(7)
    for number, actor in enumerate(order):
        if actor == "T":
            stack.timer_tick(timer)
        else:
            stack.conformance_run(tag=f"conformance-{number}")  # cache refresh + cold process
        stack.advance(7)


def test_timer_and_conformance_interleaved_never_collide_in_steady_state(make_stack):
    orderings = ORDERINGS if make_stack.mode == "fake" else STAGED_ORDERING
    for index, order in enumerate(orderings):
        stack = make_stack(index)
        _interleave(stack, order)
        assert not FORBIDDEN_IN_STEADY_STATE & set(stack.refusals()), (order, stack.refusals())
        assert stack.refusals() == [], (order, stack.refusals())
        assert all(e["outcome"] in OK for e in stack.log), order
        assert stack.kinds().count("evidence") == order.count("C"), order


# ---- (5) an api host with no SQL frame runtime renews and posts evidence (bh-65s71) ---------


def test_an_api_host_without_a_frame_runtime_renews_and_posts_evidence(stack):
    """The stack's SQL plane raises if read: G1 no longer gates an api host."""
    stack.timer_tick(stack.new_process())
    stack.conformance_run(tag="c1")
    assert stack.kinds() == ["renew", "renew", "evidence"]
    assert all(entry["outcome"] in OK for entry in stack.log)


# ---- fake only: faults a staged stack cannot be made to give, and the unlocked overlap -------


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return SessionStack("fake", tmp_path / "fake-only", monkeypatch)


def test_a_genuinely_missing_beat_on_the_service_takes_one_renew_and_retry(fake):
    """Not scripted at the client: the service itself lost its current beat."""
    process = fake.new_process()
    fake.conformance_run(tag="c1", process=process)
    fake.reset_log()
    fake.fake.principals[fake.identity.principal].observation = None
    fake.advance(5)
    fake.conformance_run(tag="c2", process=process)
    assert fake.kinds() == ["evidence", "renew", "evidence"]
    assert fake.refusals() == ["no_current_beat"]
    assert fake.fake.seen[-3:] == [
        ("evidence", "no_current_beat"),
        ("renew", "accepted"),
        ("evidence", "accepted"),
    ]


def test_a_service_side_refused_renew_leaves_the_service_with_no_evidence_post(fake):
    fake.fake.principals[fake.identity.principal].grant = "none"
    with pytest.raises(HqRefused):
        fake.conformance_run(tag="c1")
    assert fake.fake.seen == [("renew", "no_current_grant")]  # the service saw one renew only


def test_overlap_inside_one_renew_collides_on_seq_then_self_heals(fake):
    """bh-ktd2x: there is NO cross-process lock. A conformance run that lands between the timer's
    seq read and its send makes the timer reuse that seq with a different beat: the service refuses
    ``seq_not_advanced``. This documents the real, current behaviour: the loss is one tick, the
    client rebases above the wall clock, and the next tick, and every evidence post, is accepted.
    No ``evidence_digest_unmatched`` arises."""
    timer = fake.new_process()
    fake.timer_tick(timer)
    fake.advance(7)

    def conformance_lands_mid_tick():
        fake.conformance_run(tag="c-mid")
        fake.advance(1)  # the timer's beat is stamped after the other run: a different beat

    fake._on_generate = conformance_lands_mid_tick
    with pytest.raises(HqRefused) as refused:
        fake.timer_tick(timer)
    assert refused.value.code == "seq_not_advanced"
    fake.advance(7)
    fake.timer_tick(timer)  # self-healed: rebased above the floor
    fake.advance(7)
    fake.conformance_run(tag="c-after")
    assert fake.refusals() == ["seq_not_advanced"]  # exactly the one collision, nothing else
    assert fake.log[-1]["outcome"] in OK and fake.log[-1]["kind"] == "evidence"


def test_the_same_overlap_with_an_identical_beat_is_a_harmless_replay(fake):
    timer = fake.new_process()
    fake.timer_tick(timer)
    fake.advance(7)
    fake._on_generate = lambda: fake.conformance_run(tag="c-mid")  # same second, same cache
    fake.timer_tick(timer)
    assert fake.refusals() == []
    assert fake.log[-1]["kind"] == "renew" and fake.log[-1]["outcome"] == "replayed"
