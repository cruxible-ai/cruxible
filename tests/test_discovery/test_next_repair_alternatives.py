"""Repairs where the agent judges: one runnable primary, the other runnable options beside it.

Core resurfaces, agents judge: a contest offers one option per contender, a
drifted citation re-binds or retires, contradicting evidence is read first and
the Claim then restated or retired, a captured-only field restates on a Capture
its role admits. Every option is composed for the caller's surface, and one the
caller cannot run is left out while the primary still speaks for the row.
"""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimAdmissionPolicy,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy as ClaimEvidenceAdmissionPolicy,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionRule as ClaimEvidenceAdmissionRule,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.service.discovery.next import (
    PlaybillNextRepairV1,
    _CallerView,
    _citation_drift_item,
    _CitationCommitment,
    _contest_repair,
    _contradicting_capture_repair,
    _evidence_repair,
    _item,
)

CLAIM = "CLM-" + "a" * 32
OTHER = "CLM-" + "b" * 32
THIRD = "CLM-" + "c" * 32
CAPTURE = "sha256:" + "d" * 64
CITATION = "sha256:" + "e" * 64


def _captured_type(*, roles: tuple[str, ...] = ("observation",)) -> ClaimType:
    predicate = "project.work_item.measured"
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "integer"},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_requirement="captured",
        revision_evidence="accumulate",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="source",
                    claim_roles=roles,  # type: ignore[arg-type]
                    capture_contracts=(
                        ArtifactRef(
                            role=CAPTURE_CONTRACT_REF_ROLE,
                            target=ArtifactIdentity(
                                kind="CaptureContract",
                                name="playbill.foreign-source.repo.reports",
                            ),
                        ),
                    ),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
    )


_SET = {
    "subject": "project.work_item/wi-1",
    "field": "project.work_item.measured",
    "value": 5,
    "role": "observation",
}


def _row(repair: PlaybillNextRepairV1, surface: str | None = None):  # type: ignore[no-untyped-def]
    return _item(
        severity="repair",
        reason="claim_stale_evidence",
        subject_identity=f"Claim:{CLAIM}",
        detail={},
        repair=repair,
        surface=surface,  # type: ignore[arg-type]
    )


def test_a_captured_field_restates_on_contract_evidence_its_role_admits() -> None:
    repair = _evidence_repair(
        "cruxible.authoring.bind",
        target=f"Claim:{CLAIM}",
        required_change="recapture_expired_evidence",
        claim_id=CLAIM,
        claim_type=_captured_type(),
        set_arguments=_SET,
    )
    row = _row(repair)
    assert row.repair is not None
    assert row.repair.command == (
        "cruxible set project.work_item/wi-1 project.work_item.measured 5 --role observation "
        "--evidence-contract cruxible.foreign-source.repo.reports"
    )
    (retire,) = row.repair.alternatives
    assert retire.command == f"cruxible retire {CLAIM} --because TEXT"


def test_a_captured_field_whose_role_no_rule_admits_keeps_the_authoring_door() -> None:
    repair = _evidence_repair(
        "cruxible.authoring.bind",
        target=f"Claim:{CLAIM}",
        required_change="recapture_expired_evidence",
        claim_id=CLAIM,
        claim_type=_captured_type(roles=("normative",)),
        set_arguments=_SET,
    )
    assert repair.operation == "cruxible.authoring.bind"
    assert [item.operation for item in repair.alternatives] == ["cruxible.claim.retire"]


def test_a_contest_offers_each_contender_and_a_wider_one_one_write_each() -> None:
    identities = (f"Claim:{CLAIM}", f"Claim:{OTHER}", f"Claim:{THIRD}")
    repair = _contest_repair("subjects/k/x.json", identities, {"claim_ids": list(identities)})
    row = _row(repair)
    assert row.repair is not None
    options = (row.repair, *row.repair.alternatives)
    assert len(options) == 3
    assert {item.required_change for item in options} == {"choose_contender"}
    assert all(
        item.command and item.command.startswith("cruxible write - <<< ") for item in options
    )
    assert OTHER in options[0].command and THIRD in options[0].command  # type: ignore[operator]
    assert CLAIM not in options[0].command  # type: ignore[operator]

    split = _contest_repair(
        "subjects/k/x.json",
        identities[:2],
        {"claim_ids": list(identities[:2]), "qualifier_field": "region"},
    )
    assert [item.required_change for item in split.alternatives] == [
        "choose_contender",
        "revise_claims_into_distinct_qualifiers",
    ]


def test_contradicting_evidence_is_read_then_restated_on_own_evidence_or_retired() -> None:
    repair = _contradicting_capture_repair(
        f"Claim:{CLAIM}",
        claim_id=CLAIM,
        capture_digest=CAPTURE,
        required_change="adjudicate_contradicting_evidence",
        set_arguments=_SET,
    )
    row = _row(repair)
    assert row.repair is not None
    assert row.repair.command == f"cruxible get Capture:{CAPTURE}"
    restate, retire = row.repair.alternatives
    # The restatement never cites the Capture that contradicts the Claim.
    assert restate.command is not None and "--capture" not in restate.command
    assert retire.command == f"cruxible retire {CLAIM} --because TEXT"
    mcp = _row(repair, "mcp").repair
    assert mcp is not None and mcp.command == f'cruxible_get(ref="Capture:{CAPTURE}")'


def _commitment(*, section: tuple[str, ...] | None) -> _CitationCommitment:
    return _CitationCommitment(
        citation_id=CITATION,
        commitment_digest=CAPTURE,
        byte_length=10,
        claim_identity=f"Claim:{CLAIM}",
        source_id="repo.reports",
        source_digest=CAPTURE,
        section=section,
    )


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def test_a_drifted_citation_re_binds_or_retires_on_every_surface() -> None:
    for state in ("changed", "gone", "ambiguous"):
        row = _citation_drift_item(
            _commitment(section=None), coordinate=_coordinate(), drift_state=state
        )
        assert row.repair is not None
        assert row.repair.command == (
            f"cruxible rebind {CLAIM} --citation {'e' * 12} --file FILE#ANCHOR --because TEXT"
        )
        assert row.repair.operands_to_add == ("file", "because")
        assert [item.operation for item in row.repair.alternatives] == ["cruxible.claim.retire"]

    section = _citation_drift_item(
        _commitment(section=("Pricing", "Widgets")),
        coordinate=_coordinate(),
        drift_state="changed",
    )
    assert section.repair is not None
    assert section.repair.command == (
        f"cruxible rebind {CLAIM} --citation {'e' * 12} --section 'repo.reports#Pricing/Widgets' "
        "--because TEXT"
    )
    mcp = _item(
        severity=section.severity,
        reason=section.reason,
        subject_identity=section.subject_identity,
        detail=section.detail,
        repair=section.repair,
        surface="mcp",
    ).repair
    assert mcp is not None and mcp.command is not None
    assert mcp.command.startswith('cruxible_write(changes=[{"op": "rebind"')
    sdk = _item(
        severity=section.severity,
        reason=section.reason,
        subject_identity=section.subject_identity,
        detail=section.detail,
        repair=section.repair,
        surface="sdk",
    ).repair
    assert sdk is not None
    assert sdk.command == (
        f'cx.rebind("{CLAIM}", citation="{"e" * 12}", section="repo.reports#Pricing/Widgets", '
        'because="TEXT")'
    )


def test_an_alternative_the_caller_cannot_run_is_left_out() -> None:
    repair = PlaybillNextRepairV1(
        operation="cruxible.get",
        target=f"Claim:{CLAIM}",
        required_change="adjudicate_contradicting_evidence",
        arguments={"ref": f"Capture:{CAPTURE}"},
        alternatives=(
            PlaybillNextRepairV1(
                operation="cruxible.claim.retire",
                target=f"Claim:{CLAIM}",
                required_change="retire_claim_with_attribution",
                arguments={"claim_id": CLAIM},
            ),
        ),
    )
    row = _row(repair)
    reader = _CallerView(None, surface=None, tools=None, caller_rung=0)  # type: ignore[arg-type]
    (shown,) = reader.render((row,))
    assert shown.repair is not None and shown.repair.command == f"cruxible get Capture:{CAPTURE}"
    assert shown.repair.alternatives == ()
    writer = _CallerView(None, surface=None, tools=None, caller_rung=1)  # type: ignore[arg-type]
    (kept,) = writer.render((row,))
    assert kept == row


def test_an_uncovered_row_in_a_role_no_rule_admits_reads_the_claim_type() -> None:
    from cruxible_core.service.discovery.next import _role_unadmitted

    assert _role_unadmitted(_captured_type(roles=("normative",)), "observation") == ("normative",)
    assert _role_unadmitted(_captured_type(), "observation") is None
    assert _role_unadmitted(None, "observation") is None
