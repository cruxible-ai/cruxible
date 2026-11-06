"""Records refused alone at extract (D-R40), with typed reasons.

A JSON record that is not an object is refused ``not_an_object``; a key part
outside its key ClaimType's declared value type is refused
``key_part_out_of_bounds`` (a run's key rules bound it). Each is an invalid
record arm: its siblings read on, and the extract is incomplete. The cost is
one check per record: refusing records among N siblings stays linear.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cruxible_client.contracts.shapes import InvalidRecordArm, KeyPartOutOfBounds
from cruxible_core.procedures import extract
from tests.test_procedures.test_extract import _catalog, _entry, _node, _run

KEY = "key_part<security.vulnerability:cve_id>"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    extract.clear_extract_cache()
    extract.STATS.reset()


def _bounded_rules(max_len: int = 16) -> Any:
    """security.vulnerability by cve_id, whose key ClaimType holds at most ``max_len``."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.identity_normalizers import NoneNormalizer
    from cruxible_client.contracts.subject_kinds import (
        SubjectKeyRule,
        SubjectKind,
        SubjectKindKeys,
    )
    from cruxible_core.service.procedures.procedure_runs import BoundedKeyRules
    from tests.core_support._identity_support import identity_claim_type

    declared = SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name="security.vulnerability"),
        identity_rule=SubjectKindKeys(
            parts=(
                SubjectKeyRule(
                    claim_type="security.vulnerability.cve_id",
                    normalizer=NoneNormalizer(version=1),
                ),
            ),
            uniqueness="unique",
        ),
    )
    from cruxible_client.contracts.shapes import parse_type

    claim_type = identity_claim_type(
        "cve_id",
        canonical_form=None,
        predicate="security.vulnerability.cve_id",
        subject_kinds=("security.vulnerability",),
    ).model_copy(update={"value": parse_type(f"string{{max_len: {max_len}}}")})
    return BoundedKeyRules(
        lambda kind: declared if kind == "security.vulnerability" else None,
        lambda predicate: claim_type if predicate == "security.vulnerability.cve_id" else None,
    )


def _document(*records: object) -> bytes:
    return json.dumps(
        {
            "catalogVersion": "2026.09.30",
            "dateReleased": "2026-09-30T12:00:00.000000Z",
            "count": len(records),
            "vulnerabilities": list(records),
        }
    ).encode()


@pytest.mark.parametrize(
    ("bad", "lexeme"), [("CVE-2026-9", "string"), (["a"], "array"), (7, "number"), (None, "null")]
)
def test_a_record_that_is_not_an_object_is_refused_alone(bad: object, lexeme: str) -> None:
    outcome = _run(_document(_entry("CVE-2026-0001"), bad, _entry("CVE-2026-0002")))
    assert [record.key["cve_id"] for record in outcome.records] == [
        "CVE-2026-0001",
        "CVE-2026-0002",
    ]
    (refused,) = [arm for arm in outcome.held if isinstance(arm, InvalidRecordArm)]
    assert refused.record == "/vulnerabilities/1"
    (problem,) = refused.problems
    assert (problem.code, problem.field, problem.lexeme) == ("not_an_object", None, lexeme)
    assert not outcome.complete and outcome.invalid_records == 1


def test_a_key_part_out_of_its_bounds_is_refused_alone() -> None:
    node = _node(key={"cve_id": {"at": "cveID", "type": KEY}})
    long = "CVE-2026-" + "9" * 20
    content = _catalog(_entry("CVE-2026-0001"), _entry(long), _entry("CVE-2026-0002"))
    outcome = _run(content, node, keys=_bounded_rules())
    assert [record.key["cve_id"] for record in outcome.records] == [
        "CVE-2026-0001",
        "CVE-2026-0002",
    ]
    (refused,) = [arm for arm in outcome.held if isinstance(arm, InvalidRecordArm)]
    (problem,) = refused.problems
    assert (problem.code, problem.field, problem.lexeme) == (
        "key_part_out_of_bounds",
        "cve_id",
        long,
    )
    assert not outcome.complete


def test_the_bounded_rules_refuse_only_out_of_bounds_parts() -> None:
    rules = _bounded_rules()
    assert rules.canonical("security.vulnerability", "cve_id", "CVE-2026-0001") == "CVE-2026-0001"
    assert rules.canonical("security.vulnerability", None, "CVE-2026-0001") == "CVE-2026-0001"
    with pytest.raises(KeyPartOutOfBounds):
        rules.canonical("security.vulnerability", "cve_id", "CVE-" + "9" * 20)
    with pytest.raises(KeyPartOutOfBounds):
        rules.canonical("security.vulnerability", None, {"cve_id": "CVE-" + "9" * 20})
    # The bound is part of what types a key: a different bound is a different index.
    assert rules.rule_token("security.vulnerability") != _bounded_rules(32).rule_token(
        "security.vulnerability"
    )


def test_refusing_records_among_n_siblings_is_one_check_per_record() -> None:
    """Records read and field reads grow with N alone; each bad record is refused once."""

    measured = {}
    for size in (10, 40):
        extract.clear_extract_cache()
        extract.STATS.reset()
        records: list[object] = []
        for index in range(size):
            records.append(_entry(f"CVE-2026-{index:04d}"))
            if index % 5 == 0:
                records.append("not a record")
        outcome = _run(_document(*records))
        refused = [arm for arm in outcome.held if isinstance(arm, InvalidRecordArm)]
        assert len(outcome.records) == size and len(refused) == size // 5
        measured[size] = (extract.STATS.selected, extract.STATS.field_reads, len(outcome.held))
    (small_selected, small_reads, small_held), (large_selected, large_reads, large_held) = (
        measured[10],
        measured[40],
    )
    assert large_selected == 4 * small_selected and large_held == 4 * small_held, measured
    assert large_reads == 4 * small_reads, measured
