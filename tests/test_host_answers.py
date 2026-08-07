"""The declarative answers file for `bh host provision` (bh-q160.2).

Validation is the point of this file, so most of these are rejection tests. The one that
matters most is :func:`test_a_typod_key_is_an_error_not_a_warning`: a permissive parser would
turn `adopts:` into a host that silently adopts nothing while reporting success, and the
operator's actual goal would vanish with no message.
"""

from __future__ import annotations

import pytest

from beadhive import host_answers
from beadhive.host_answers import AnswersInvalid


def test_minimal_file_is_just_a_role():
    """HQ carries the fleet's truth; only what cannot be known until somebody decides it for
    THIS host belongs here."""
    answers = host_answers.parse({"role": "viewer"})
    assert answers.role == "viewer"
    assert answers.hives is None and answers.adopt == []


def test_a_typod_key_is_an_error_not_a_warning():
    with pytest.raises(AnswersInvalid) as exc:
        host_answers.parse({"role": "viewer", "adopts": ["bh"]})
    assert "adopts" in str(exc.value), "the error must name the offending key"
    assert "adopt" in str(exc.value), "…and list what was allowed"


def test_missing_role_is_rejected():
    with pytest.raises(AnswersInvalid, match="role"):
        host_answers.parse({"adopt": ["bh"]})


def test_unknown_role_is_rejected():
    with pytest.raises(AnswersInvalid, match="role"):
        host_answers.parse({"role": "primary"})  # close to, but not, executor


def test_adopt_is_never_defaulted_from_hives():
    """Cloning is reversible and local; adopting CASes a fleet-visible lease. Listing a hive
    under `hives` says "put a copy here", never "take primary from wherever it is now"."""
    answers = host_answers.parse({"role": "viewer", "hives": ["bh", "other"]})
    assert answers.adopt == []


def test_adopting_a_hive_you_do_not_clone_is_rejected():
    with pytest.raises(AnswersInvalid, match="somewhere-else"):
        host_answers.parse({"role": "viewer", "hives": ["bh"], "adopt": ["somewhere-else"]})


def test_empty_hives_differs_from_omitted_hives():
    """`hives: []` is "carry none" and is a legitimate answer; omitting it means ALL."""
    assert host_answers.parse({"role": "viewer", "hives": []}).hives == []
    assert host_answers.parse({"role": "viewer"}).hives is None


@pytest.mark.parametrize("bad", ["bh", 3, {"a": 1}, [1, 2]])
def test_list_keys_must_be_lists_of_strings(bad):
    with pytest.raises(AnswersInvalid, match="hives"):
        host_answers.parse({"role": "viewer", "hives": bad})


def test_a_non_mapping_file_is_rejected():
    with pytest.raises(AnswersInvalid, match="mapping"):
        host_answers.parse(["role: worker"])  # type: ignore[arg-type]


def test_load_reports_the_path_on_bad_yaml(tmp_path):
    bad = tmp_path / "plan.yaml"
    bad.write_text("role: worker\n  bad indent:\n")
    with pytest.raises(AnswersInvalid, match="not valid YAML"):
        host_answers.load(bad)


def test_load_reports_a_missing_file_rather_than_raising_oserror(tmp_path):
    with pytest.raises(AnswersInvalid, match="cannot read"):
        host_answers.load(tmp_path / "absent.yaml")


def test_dotted_hq_remote_key_round_trips():
    """The key is `hq.remote`, matching bh's dotted config names rather than nesting."""
    answers = host_answers.parse({"role": "viewer", "hq.remote": "git@example.com:hq.git"})
    assert answers.hq_remote == "git@example.com:hq.git"
