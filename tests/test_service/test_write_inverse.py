"""A none through the write verbs: set/add/retire of an inverse Claim, and the reads.

An inverse is a Claim (slice IV): a ``{"none": KEY}`` value lowers to the
existing ``NoneClaimObject`` through the same set, add and retire. These
tests cover the found / none / unknown answers on the write and read paths:
a flip each way on a one-valued field, a flip on a many-valued unit by
retire-plus-add, the polarity refusal, evidence required, the non-v9 refusal
and a stale none.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactRef
from cruxible_client.contracts.claim_types import (
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimInverse,
    ClaimType,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.write import SetRequest, WriteOutcome, WriteRequest
from cruxible_core.proposals.proposals import ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.query_values import read_live_values
from tests.core_support._write_support import (
    KIND,
    OWNER,
    REPORTS,
    caller,
    report_evidence,
    seed_write_surface,
)

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
HOUR = 3_600_000_000


def _v9(
    field: str,
    inverse: dict[str, Any],
    *,
    cardinality: str = "one",
    object_kind: str = "literal",
    literal_schema: dict[str, Any] | None = None,
    object_kinds: tuple[str, ...] = (),
) -> ClaimType:
    """A ClaimType v9 of ``project.work_item`` whose evidence (and inverse) is repo.reports."""

    predicate = f"{KIND}.{field}"
    roles = ("observation",)
    return ClaimType(
        artifact_format="cruxible-claim-type-v9",
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=(KIND,),
        object_kind=object_kind,  # type: ignore[arg-type]
        literal_schema=literal_schema,
        allowed_object_subject_kinds=object_kinds,
        cardinality=cardinality,  # type: ignore[arg-type]
        permitted_roles=roles,  # type: ignore[arg-type]
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="source",
                    claim_roles=roles,  # type: ignore[arg-type]
                    capture_contracts=(
                        ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=REPORTS.identity),
                    ),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality=cardinality,  # type: ignore[arg-type]
            eligible_verdicts=("supported",),
            selector="all" if cardinality == "many" else "only_contender",
        ),
        evidence_freshness=ClaimEvidenceFreshness(
            stale_after=ClaimFreshnessDuration(microseconds=30 * 24 * HOUR)
        ),
        evidence_requirement="self",
        revision_evidence="replace",
        canonical_form=(),
        inverse=ClaimInverse.model_validate({"evidence_rules": ["source"], **inverse}),
    )


#: ``state`` (one-valued, its none ages in an hour), ``tags`` (many literals)
#: and ``blocks`` (many work items), each with an inverse.
INVERSE_TYPES = (
    _v9(
        "state",
        {"key": {"kind": "slot"}, "stale_after": {"microseconds": HOUR}},
        literal_schema={"enum": ["open", "shut"], "type": "string"},
    ),
    _v9(
        "tags",
        {"key": {"kind": "object"}},
        cardinality="many",
        literal_schema={"type": "string"},
    ),
    _v9(
        "blocks",
        {"key": {"kind": "object"}},
        cardinality="many",
        object_kind="subject",
        object_kinds=(KIND,),
    ),
)


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    live = seed_write_surface(tmp_path)[0]
    base = live.accepted_coordinate()
    tree = live.tree_at(base.git_oid)
    for claim_type in INVERSE_TYPES:
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    proposed = live.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/inverse-types", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:40.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        live, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    return live


def _write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> WriteOutcome:
    request = WriteRequest.model_validate(
        {"because": "The report says so.", "changes": list(changes), **options}
    )
    return service_playbill_write(instance, request=request, caller=caller())


def _change(op: str, field: str, value: object, evidence: Any = None, **extra: Any) -> Any:
    change = {"op": op, "subject": WI1, "field": field, "value": value, **extra}
    if evidence is not None:
        change["evidence"] = evidence
    return change


def _card(instance: PlaybillInstance, ref: str = WI1, **request: Any) -> Any:
    from cruxible_core.storage.cas import BodyAccessContext

    return service_playbill_get(
        instance,
        request=GetRequest(ref=ref, **request),
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    ).card


def _row(card: Any, field: str) -> Any:
    return next(row for row in card.claims if row.predicate == field)


def _values(instance: PlaybillInstance, field: str) -> list[object]:
    return sorted(
        str(item.value)
        for item in read_live_values(
            instance,
            instance.accepted_coordinate(),
            subject_paths=(subject_path(KIND, "wi-1"),),
            predicates=(f"{KIND}.{field}",),
        )
    )


def test_a_one_valued_field_flips_to_none_and_back_through_set(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    first = _write(instance, _change("set", "state", "open", report_evidence(tmp_path, "open")))
    assert first.status == "accepted", first
    value_claim = first.changes[0].claim
    flipped = _write(
        instance, _change("set", "state", {"none": None}, report_evidence(tmp_path, "closed"))
    )
    assert flipped.status == "accepted", flipped
    (change,) = flipped.changes
    # The set revised the live value into the none: one lineage, the other polarity.
    assert change.revises == value_claim and change.claim == value_claim
    assert change.before == "open"
    assert change.after is not None and change.after.object == {"none": None}  # type: ignore[union-attr]
    # Nones never enter value reads; the card lists the none beside no value.
    assert _values(instance, "state") == []
    row = _row(_card(instance), "state")
    assert row.claim == () and row.value == []
    (none,) = row.nones
    assert (none.claim, none.of, none.verdict) == (value_claim, None, "supported")
    # And back: set a value over the none.
    back = _write(instance, _change("set", "state", "shut", report_evidence(tmp_path, "shut")))
    assert back.status == "accepted", back
    assert back.changes[0].revises == value_claim
    assert back.changes[0].before.object == {"none": None}  # type: ignore[union-attr]
    row = _row(_card(instance), "state")
    assert row.value == "shut" and row.nones == ()


def test_a_many_valued_unit_flips_by_retiring_its_value_and_adding_the_none(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    added = _write(
        instance,
        _change("add", "tags", "urgent", report_evidence(tmp_path, "urgent")),
        _change("add", "tags", "small", report_evidence(tmp_path, "small")),
    )
    assert added.status == "accepted", added
    urgent = next(item.claim for item in added.changes if item.after == "urgent")
    # A none beside the live value of its unit is refused: the writer names what it retires.
    refused = _write(
        instance, _change("add", "tags", {"none": "urgent"}, report_evidence(tmp_path, "not u"))
    )
    assert refused.status == "refused"
    assert refused.refusal is not None and "polarity_conflict" in refused.refusal.code
    flip = _write(
        instance,
        {"op": "retire", "target": urgent, "reason": "superseded"},
        _change("add", "tags", {"none": "urgent"}, report_evidence(tmp_path, "no urgency")),
    )
    assert flip.status == "accepted", flip
    assert _values(instance, "tags") == ["small"]
    row = _row(_card(instance), "tags")
    assert row.value == ["small"]
    (none,) = row.nones
    assert none.of == "urgent" and none.verdict == "supported"
    # A none of another value is its own unit: no retirement needed.
    other = _write(
        instance, _change("add", "tags", {"none": "large"}, report_evidence(tmp_path, "no l"))
    )
    assert other.status == "accepted", other
    assert {item.of for item in _row(_card(instance), "tags").nones} == {"urgent", "large"}


def test_a_none_of_a_subject_names_it_and_flips_back_to_the_value(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    written = _write(
        instance, _change("add", "blocks", {"none": WI2}, report_evidence(tmp_path, "free"))
    )
    assert written.status == "accepted", written
    none_claim = written.changes[0].claim
    (none,) = _row(_card(instance), "blocks").nones
    assert none.of == WI2 and none.verdict == "supported"
    back = _write(
        instance,
        {"op": "retire", "target": none_claim, "reason": "superseded"},
        _change("add", "blocks", WI2, report_evidence(tmp_path, "blocked")),
    )
    assert back.status == "accepted", back
    row = _row(_card(instance), "blocks")
    assert row.value == [WI2] and row.nones == ()


def test_a_none_needs_evidence_its_inverse_rules_admit(instance: PlaybillInstance) -> None:
    refused = _write(instance, _change("set", "state", {"none": None}))
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.write.none_needs_evidence"
    assert "repo.reports" in " ".join(refused.refusal.candidates)
    # The shape is checked against the inverse's key.
    keyed = _write(instance, _change("set", "state", {"none": "open"}))
    assert keyed.refusal is not None and keyed.refusal.code == (
        "cruxible.write.none_key_not_admitted"
    )
    unkeyed = _write(instance, _change("add", "tags", {"none": None}))
    assert unkeyed.refusal is not None and unkeyed.refusal.code == "cruxible.write.none_needs_key"


def test_a_type_without_an_inverse_refuses_naming_the_declaration(
    instance: PlaybillInstance,
) -> None:
    refused = _write(instance, _change("set", "status", {"none": None}))
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.write.inverse_undeclared"
    assert refused.refusal.repair is not None and "`inverse`" in refused.refusal.repair


def test_set_request_carries_a_none_like_a_batch(instance: PlaybillInstance) -> None:
    request = SetRequest.model_validate(
        {"subject": WI1, "field": "state", "value": {"none": None}, "because": "Closed."}
    )
    assert request.change().value.none is None  # type: ignore[union-attr]
    outcome = service_playbill_write(
        instance,
        request=WriteRequest(because="Closed.", changes=(request.change(),)),
        caller=caller(),
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == "cruxible.write.none_needs_evidence"


def test_a_none_goes_stale_on_its_inverse_horizon_while_values_keep_the_types(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    written = _write(
        instance,
        _change("set", "state", {"none": None}, report_evidence(tmp_path, "closed")),
        _change("add", "tags", "urgent", report_evidence(tmp_path, "urgent")),
    )
    assert written.status == "accepted", written
    later = utc_now() + timedelta(hours=2)
    card = _card(instance, evaluation_time=later)
    (none,) = _row(card, "state").nones
    assert none.verdict in {"stale", "stale_evidence"} and "stale" in none.flags
    assert "stale" in _row(card, "state").flags
    # A value of the same types ages on the type's 30 days: still supported.
    assert _row(card, "tags").flags == ()


def test_query_rows_list_nones_beside_values_never_as_one(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    from cruxible_client.contracts.compact_query import QueryRequest
    from cruxible_core.service.discovery.compact_query import service_playbill_query

    written = _write(
        instance,
        _change("set", "state", {"none": None}, report_evidence(tmp_path, "closed")),
        _change("add", "tags", "small", report_evidence(tmp_path, "small")),
        _change("add", "tags", {"none": "urgent"}, report_evidence(tmp_path, "not urgent")),
    )
    assert written.status == "accepted", written
    state_none = next(item.claim for item in written.changes if item.field == "state")

    def query(**fields: Any) -> Any:
        request = QueryRequest.model_validate(
            {"kind": KIND, "where": [{"field": "subject_id", "eq": "wi-1"}], **fields}
        )
        (row,) = service_playbill_query(instance, request=request).model_dump(mode="json")["rows"]
        return row

    row = query(select=["state", "tags"])
    # The none is never the cell's value: state reads null (no value) beside its none.
    assert row["state"] is None and row["tags"] == ["small"]
    assert row["nones"] == {
        "state": [{"claim": state_none, "verdict": "supported", "status": "accepted"}],
        "tags": [
            {
                "claim": next(
                    item.claim
                    for item in written.changes
                    if item.field == "tags" and item.after != "small"
                ),
                "of": "urgent",
                "verdict": "supported",
                "status": "accepted",
            }
        ],
    }
    # A stale none flags its row, as a stale value would.
    stale = query(select=["state"], evaluation_time=utc_now() + timedelta(hours=2))
    assert "stale" in stale["flags"] and stale["nones"]["state"][0]["flags"] == ["stale"]
    # A column without nones has no entry.
    assert "nones" not in query(select=["blocks"])
