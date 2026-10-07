"""The write verbs over accepted state: set, add and retire, lowered onto one change set."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    SelfSourceBody,
)
from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring import write_verbs
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.query_values import read_live_values
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from tests.core_support._write_support import (
    CLAIM_TYPES,
    KIND,
    OWNER,
    REPORTS,
    caller,
    cited_captures,
    report_evidence,
    seed_write_surface,
)

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_write_surface(tmp_path)[0]


def _write(
    instance: PlaybillInstance,
    *changes: dict[str, Any],
    because: str = "The writer checked it.",
    who: WriteCaller | None = None,
    **options: Any,
) -> WriteOutcome:
    request = WriteRequest.model_validate({"because": because, "changes": list(changes), **options})
    return service_playbill_write(instance, request=request, caller=who or caller())


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def _add(subject: str, field: str, value: object) -> dict[str, Any]:
    return {"op": "add", "subject": subject, "field": field, "value": value}


def _values(instance: PlaybillInstance, subject: str, field: str) -> list[object]:
    kind, subject_id = subject.split("/", 1)
    return sorted(
        str(item.value)
        for item in read_live_values(
            instance,
            instance.accepted_coordinate(),
            subject_paths=(subject_path(kind, subject_id),),
            predicates=(f"{KIND}.{field}",),
        )
    )


def _refusal(outcome: WriteOutcome) -> Any:
    assert outcome.refusal is not None, outcome
    return outcome.refusal


# -- set ----------------------------------------------------------------------


def test_set_states_a_value_then_revises_the_live_claim_without_its_id(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    assert first.status == "accepted", first
    (change,) = first.changes
    assert (change.op, change.field, change.before, change.after) == (
        "set",
        "status",
        None,
        "ready",
    )
    assert change.revises is None and change.claim is not None
    assert first.coordinate.generation == first.base.generation + 1  # type: ignore[union-attr]

    second = _write(instance, _set(WI1, "status", "done"))
    assert second.status == "accepted", second
    (revised,) = second.changes
    assert revised.revises == change.claim and revised.claim == change.claim
    assert (revised.before, revised.after) == ("ready", "done")
    assert _values(instance, WI1, "status") == ["done"]


def test_the_role_is_inferred_when_only_one_is_permitted_and_required_otherwise(
    instance: PlaybillInstance,
) -> None:
    assert _write(instance, _set(WI1, "title", "Tidy the CLI")).status == "accepted"
    refused = _write(instance, _set(WI1, "priority", "high"))
    assert refused.status == "refused"
    assert _refusal(refused).code == "cruxible.write.role_required"
    assert set(_refusal(refused).candidates) == {"normative", "observation"}
    assert _write(instance, _set(WI1, "priority", "high", role="normative")).status == "accepted"
    wrong = _write(instance, _set(WI1, "title", "x", role="normative"))
    assert _refusal(wrong).code == "cruxible.write.role_not_permitted"
    assert _refusal(wrong).candidates == ("observation",)


def test_a_wrong_enum_member_refuses_listing_the_members(instance: PlaybillInstance) -> None:
    refused = _write(instance, _set(WI1, "status", "dne"))
    refusal = _refusal(refused)
    assert refusal.code == "cruxible.write.value_not_member"
    assert "blocked, done, ready" in refusal.message
    assert refusal.candidates == ("done",)
    assert refusal.change == 0 and refusal.field_path == "changes[0].value"


def test_the_claim_law_names_the_enum_members_too(instance: PlaybillInstance) -> None:
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    payload = ChangeSetAuthoringPayload(
        members=(
            ClaimAuthoringPayloadV2(
                statement=AuthoringClaimStatement(
                    subject=SemanticAddress.whole_artifact(subject_path(KIND, "wi-1")),
                    predicate=f"{KIND}.status",
                    object=LiteralClaimObject(value="dne"),
                    role="observation",
                ),
                rationale="Bypass the write verbs.",
                source=SelfSourceBody(content_base64=base64.b64encode(b"dne").decode()),
                dependency_drafts=ClaimDependencyDrafts(),
            ),
        )
    )
    result = coordinator.compile(
        actor=OWNER, payload=payload, canonical_timestamp="2026-09-29T12:00:00.000000Z"
    )
    messages = [item.message for item in result.frontier.diagnostics]
    assert any("its members are: blocked, done, ready" in text for text in messages), messages


def test_wrong_names_refuse_with_the_nearest_names(instance: PlaybillInstance) -> None:
    field = _refusal(_write(instance, _set(WI1, "stauts", "done")))
    assert field.code == "cruxible.write.unknown_field" and "status" in field.candidates
    assert field.field_path == "changes[0].field"
    kind = _refusal(_write(instance, _set("project.work_itme/wi-1", "status", "done")))
    assert kind.code == "cruxible.write.unknown_kind" and KIND in kind.candidates
    full = _write(instance, _set(WI1, f"{KIND}.status", "done"))
    assert full.status == "accepted" and full.changes[0].field == "status"


def test_set_and_add_respect_the_field_cardinality(instance: PlaybillInstance) -> None:
    many = _refusal(_write(instance, _set(WI1, "governs", WI2)))
    assert many.code == "cruxible.write.field_is_many"
    single = _refusal(_write(instance, _add(WI1, "status", "done")))
    assert single.code == "cruxible.write.field_is_single"


# -- add ----------------------------------------------------------------------


def test_two_adds_on_one_many_valued_field_land_in_one_change_set(
    instance: PlaybillInstance,
) -> None:
    outcome = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    assert outcome.status == "accepted", outcome
    assert outcome.coordinate.generation == outcome.base.generation + 1  # type: ignore[union-attr]
    claims = {item.claim for item in outcome.changes}
    assert len(claims) == 2 and None not in claims
    assert _values(instance, WI1, "governs") == [WI2, WI3]

    head = instance.accepted_coordinate().git_oid
    again = _write(instance, _add(WI1, "governs", WI2))
    assert again.status == "accepted" and again.proposal is None
    (change,) = again.changes
    assert change.already_live and change.claim in claims and change.after == WI2
    assert change.verdict == "supported"
    assert instance.accepted_coordinate().git_oid == head  # no change set was submitted
    assert _write(instance, _add(WI1, "governs", WI2), dry_run=True).status == "would_accept"

    # In a batch, only the new value is submitted.
    mixed = _write(instance, _add(WI1, "governs", WI2), _set(WI1, "status", "ready"))
    assert mixed.status == "accepted" and mixed.proposal is not None
    assert [item.already_live for item in mixed.changes] == [True, False]


@pytest.mark.parametrize(
    ("field", "value"),
    [("labels", "urgent"), ("governs", WI2)],
    ids=["literal", "relation"],
)
def test_an_add_beside_the_retirement_of_the_same_value_states_it_anew(
    instance: PlaybillInstance, field: str, value: str
) -> None:
    """A value retired in the same write is not live, so adding it back is a real add."""

    old = _write(instance, _add(WI1, field, value)).changes[0].claim
    outcome = _write(instance, {"op": "retire", "target": old}, _add(WI1, field, value))
    assert outcome.status == "accepted", outcome
    retired, added = outcome.changes
    assert retired.claim == old
    assert not added.already_live and added.claim not in (None, old)
    assert outcome.proposal is not None
    assert _values(instance, WI1, field) == [value]


@pytest.mark.parametrize(
    ("options", "status"),
    [({"dry_run": True}, "would_accept"), ({"accept": "never"}, "awaiting_approval")],
    ids=["dry_run", "accept_never"],
)
def test_an_already_live_add_keeps_its_verdict_beside_a_pending_change(
    instance: PlaybillInstance, options: dict[str, Any], status: str
) -> None:
    """R05 holds for a no-op member of a batch that is not yet accepted."""

    live = _write(instance, _add(WI1, "labels", "urgent")).changes[0]
    assert live.verdict == "uncovered"
    outcome = _write(
        instance, _add(WI1, "labels", "urgent"), _set(WI1, "status", "ready"), **options
    )
    assert outcome.status == status, outcome
    present, fresh = outcome.changes
    assert present.already_live and present.claim == live.claim
    assert (present.verdict, fresh.verdict) == ("uncovered", "supported")
    (warning,) = outcome.warnings
    assert (warning.change, warning.claim, warning.verdict) == (0, live.claim, "uncovered")


def test_without_the_filled_dispositions_the_same_adds_refuse(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispositions the verbs fill are exactly what the slot law demands."""

    monkeypatch.setattr(write_verbs, "_with_dispositions", lambda member, _claims: member)
    outcome = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    assert outcome.status == "refused"
    assert _refusal(outcome).code == "cruxible.authoring.existing_claim_dispositions_incomplete"


def test_a_relation_value_never_creates_the_subject_it_names(instance: PlaybillInstance) -> None:
    refusal = _refusal(_write(instance, _add(WI1, "governs", f"{KIND}/wi-9")))
    assert refusal.code == "cruxible.write.value_subject_not_found"
    assert refusal.candidates  # the nearest existing work items
    kind = _refusal(_write(instance, _add(WI1, "governs", "other.kind/x")))
    assert kind.code == "cruxible.write.value_kind_not_admitted"
    # A Subject this same write adds may be named.
    linked = _write(
        instance, _set(f"{KIND}/wi-9", "title", "New item"), _add(WI1, "governs", f"{KIND}/wi-9")
    )
    assert linked.status == "accepted", linked
    assert linked.subjects_added == (f"{KIND}/wi-9",)


def test_an_at_sign_subject_reference_names_the_same_subject(
    instance: PlaybillInstance,
) -> None:
    """``@kind/id`` is the SDK scripts' Subject spelling; the verbs read it as ``kind/id``.

    It used to refuse ``value_kind_not_admitted`` with a repair naming the very
    kind it was given. No Subject kind starts with ``@``, so the sigil is
    unambiguous wherever a Subject is expected, and nowhere else is it touched.
    """

    linked = _write(instance, _add(f"@{WI1}", "governs", f"@{WI2}"))
    assert linked.status == "accepted", linked
    assert _values(instance, WI1, "governs") == [WI2]
    (change,) = linked.changes
    assert (change.subject, change.after) == (WI1, WI2)
    # Expectations compare a Subject the same way, and a slot names it too.
    slot = {"subject": f"@{WI1}", "field": "governs"}
    retired = _write(instance, {"op": "retire", "target": slot, "expect": [f"@{WI2}"]})
    assert retired.status == "accepted", retired
    assert _values(instance, WI1, "governs") == []
    # A literal field keeps the text exactly as written.
    titled = _write(instance, _set(WI1, "title", f"@{WI3}"))
    assert titled.status == "accepted", titled
    assert _values(instance, WI1, "title") == [f"@{WI3}"]


# -- subjects -------------------------------------------------------------------


def test_a_missing_subject_of_a_known_kind_is_added_in_the_same_change_set(
    instance: PlaybillInstance,
) -> None:
    outcome = _write(instance, _set(f"{KIND}/wi-new", "title", "Brand new"))
    assert outcome.status == "accepted", outcome
    assert outcome.subjects_added == (f"{KIND}/wi-new",)
    assert outcome.coordinate.generation == outcome.base.generation + 1  # type: ignore[union-attr]
    assert _values(instance, f"{KIND}/wi-new", "title") == ["Brand new"]


# -- exact content ----------------------------------------------------------------


def test_exact_content_takes_the_text_as_value_and_as_its_own_evidence(
    instance: PlaybillInstance,
) -> None:
    text = "Rulings are text.\nThis one ends with a newline.\n"
    outcome = _write(instance, _set(WI1, "ruling", text))
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].after == text
    (live,) = read_live_values(
        instance,
        instance.accepted_coordinate(),
        subject_paths=(subject_path(KIND, "wi-1"),),
        predicates=(f"{KIND}.ruling",),
    )
    assert live.value == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    revised = _write(instance, _set(WI1, "ruling", "Replaced."))
    assert revised.status == "accepted" and revised.changes[0].before == text
    assert revised.changes[0].revises == outcome.changes[0].claim

    mismatch = _write(
        instance, _set(WI1, "ruling", "Again.", evidence={"kind": "self", "self": "Again.\n"})
    )
    assert _refusal(mismatch).code == "cruxible.write.exact_content_evidence_mismatch"


# -- evidence ---------------------------------------------------------------------


def test_self_evidence_a_field_does_not_admit_lands_uncovered_and_says_so(
    instance: PlaybillInstance,
) -> None:
    """R05: the write succeeds, but the outcome names the verdict, the reason and the fix."""

    outcome = _write(instance, _set(WI1, "measured", 3), surface="cli")
    assert outcome.status == "accepted", outcome
    (change,) = outcome.changes
    assert change.verdict == "uncovered"
    (warning,) = outcome.warnings
    assert warning.code == "cruxible.write.verdict_not_supported"
    assert (warning.change, warning.claim, warning.verdict) == (0, change.claim, "uncovered")
    assert warning.admitted_contracts == (REPORTS.identity.name,)
    assert warning.used_contract == COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name
    assert "evidence not admitted" in warning.message
    assert REPORTS.identity.name in warning.message
    assert warning.repair is not None and "--capture" in warning.repair
    assert outcome.next == warning.repair
    assert _values(instance, WI1, "measured") == ["3"]

    preview = _write(instance, _set(WI2, "measured", 4), dry_run=True)
    assert preview.status == "would_accept"
    assert preview.changes[0].verdict == "uncovered" and len(preview.warnings) == 1

    supported = _write(instance, _set(WI1, "status", "ready"))
    assert supported.changes[0].verdict == "supported" and supported.warnings == ()


class _Sent(Exception):
    def __init__(self, request: Any) -> None:
        self.request = request


class _Recorder:
    """A client that keeps the request the SDK would send, and sends nothing."""

    def set(self, _instance_id: str, *, request: Any) -> Any:
        raise _Sent(request)

    def write(self, _instance_id: str, *, request: Any) -> Any:
        raise _Sent(request)


def test_every_sdk_evidence_repair_runs_against_the_real_builders(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    """The rendered repair is a call the SDK takes, and it sends the change it names."""

    from cruxible_client.authoring.sdk import Cruxible
    from cruxible_client.contracts.write import CaptureEvidence

    digest = "CAP-" + "b" * 12
    outcome = _write(
        instance,
        _set(WI1, "measured", 3),
        _add(WI1, "labels", "urgent"),
        surface="sdk",
    )
    assert outcome.status == "accepted", outcome
    assert [item.change for item in outcome.warnings] == [0, 1]
    for warning in outcome.warnings:
        assert warning.repair is not None
        placeholder = f"<CAP- handle of a Capture under {REPORTS.identity.name}>"
        assert placeholder in warning.repair
        pb = Cruxible(
            client=_Recorder(),  # type: ignore[arg-type]
            instance_id="inst",
            workspace=tmp_path,
            access_profile="governed_write",  # type: ignore[arg-type]
            clock=None,
        )
        with pytest.raises(_Sent) as sent:
            exec(  # noqa: S102 - the repair is the code under test
                warning.repair.replace(placeholder, digest),
                {"cx": pb, "CaptureEvidence": CaptureEvidence},
            )
        request = sent.value.request
        assert request.because == "The writer checked it."
        (change,) = request.changes if hasattr(request, "changes") else (request.change(),)
        written = outcome.changes[warning.change]
        assert (change.op, change.subject, change.field) == (
            written.op,
            written.subject,
            written.field,
        )
        assert change.evidence == CaptureEvidence(capture=digest)


def _move_to_v7(instance: PlaybillInstance, field: str, **fields: object) -> None:
    """Accept a ClaimType v7 successor of one write-vocabulary field."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactRef
    from cruxible_client.contracts.captures import capture_contract_digest
    from cruxible_client.contracts.claim_types import (
        ClaimType,
        claim_type_digest,
        claim_type_path,
        render_claim_type,
    )
    from cruxible_client.contracts.policies import (
        CAPTURE_CONTRACT_REF_ROLE,
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from cruxible_core.proposals.proposals import ProposalAdmissionRequest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    base = next(item for item in CLAIM_TYPES if item.predicate == f"{KIND}.{field}")
    (rule,) = base.evidence_admission_policy.rules
    contract = (
        REPORTS
        if rule.capture_contract_digests == (capture_contract_digest(REPORTS).tagged,)  # type: ignore[union-attr]
        else COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    )
    successor = ClaimType.model_validate(
        {
            **base.model_dump(mode="python"),
            "artifact_format": "playbill-claim-type-v7",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id=rule.rule_id,
                        claim_roles=rule.claim_roles,
                        capture_contracts=(
                            ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=contract.identity),
                        ),
                        evidence_kinds=rule.evidence_kinds,
                        admission=rule.admission,
                        subject_binding=rule.subject_binding,
                    ),
                )
            ),
            "evidence_requirement": "self",
            "revision_evidence": "replace",
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(base).tagged),
            **fields,
        }
    )
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    tree[claim_type_path(successor.predicate)] = render_claim_type(successor)
    proposed = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/v7-{field}", proposed_base_oid=coordinate.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:45.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


def test_a_captured_claim_type_refuses_own_words_naming_its_contracts(
    instance: PlaybillInstance,
) -> None:
    """ClaimType v7 ``captured`` makes the evidence_required refusal reachable."""

    assert write_verbs.requires_captured_evidence(CLAIM_TYPES[-1]) is False
    _move_to_v7(instance, "measured", evidence_requirement="captured")
    refusal = _refusal(_write(instance, _set(WI1, "measured", 3)))
    assert refusal.code == "cruxible.write.evidence_required"
    assert refusal.candidates == (REPORTS.identity.name,)
    assert "evidence_requirement 'captured'" in refusal.message
    captured = _write(
        instance,
        _set(WI1, "measured", 3, evidence={"kind": "capture", "capture": "sha256:" + "a" * 64}),
    )
    assert _refusal(captured).code != "cruxible.write.evidence_required"


def test_a_none_claim_type_is_supported_by_the_writers_words_without_a_warning(
    instance: PlaybillInstance,
) -> None:
    _move_to_v7(instance, "measured", evidence_requirement="none")
    outcome = _write(instance, _set(WI1, "measured", 3))
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].verdict == "supported"
    assert outcome.warnings == ()


def test_default_role_implies_the_role_and_an_explicit_role_overrides_it(
    instance: PlaybillInstance,
) -> None:
    refused = _refusal(_write(instance, _set(WI1, "priority", "high")))
    assert refused.code == "cruxible.write.role_required"
    assert refused.repair is not None and "default_role" in refused.repair
    _move_to_v7(instance, "priority", default_role="observation")
    implied = _write(instance, _set(WI1, "priority", "high"))
    assert implied.status == "accepted", implied
    explicit = _write(instance, _set(WI2, "priority", "low", role="normative"))
    assert explicit.status == "accepted", explicit
    from cruxible_client.contracts.claims import claim_path, parse_claim

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    roles = {}
    for outcome in (implied, explicit):
        claim_id = outcome.changes[0].claim
        assert claim_id is not None
        path = claim_path(claim_id.removeprefix("Claim:"))
        roles[outcome.changes[0].subject] = parse_claim(tree[path], path=path).statement.role
    assert roles == {WI1: "observation", WI2: "normative"}


def test_a_capture_handle_resolves_to_its_digest_before_lowering(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    from cruxible_client.contracts.write import capture_handle

    first = _write(
        instance, _set(WI1, "measured", 3, evidence=report_evidence(tmp_path, "Count: 3"))
    )
    assert first.status == "accepted", first
    assert first.changes[0].verdict == "supported" and first.warnings == ()
    (digest,) = cited_captures(instance, first.changes[0].claim or "")
    handle = capture_handle(digest)
    assert handle == "CAP-" + digest.removeprefix("sha256:")[:12]

    cited = _write(
        instance, _set(WI2, "measured", 3, evidence={"kind": "capture", "capture": handle})
    )
    assert cited.status == "accepted", cited
    (change,) = cited.changes
    assert change.capture == handle and change.verdict == "supported"
    assert cited_captures(instance, change.claim or "") == {digest}
    # The lowered payload names the digest, exactly as a digest-cited write would.
    preview = _write(
        instance,
        _add(WI3, "labels", "urgent") | {"evidence": {"kind": "capture", "capture": digest}},
        dry_run=True,
    )
    assert preview.changes[0].capture == handle

    unknown = _refusal(
        _write(
            instance,
            _set(WI3, "measured", 3, evidence={"kind": "capture", "capture": "CAP-" + "f" * 12}),
        )
    )
    assert unknown.code == "cruxible.write.capture_not_found"
    assert unknown.field_path == "changes[0].evidence.capture"
    assert 1 <= len(unknown.candidates) <= 8
    assert all(item.startswith("CAP-") for item in unknown.candidates)


def _held_captures(instance: PlaybillInstance) -> set[str]:
    """Every Capture envelope the instance's body store holds, by digest."""

    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.storage.cas import BodyAccessContext

    store = instance.body_store()
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    found: set[str] = set()
    scan = store.scan("", budget=1_000_000)
    assert scan.complete
    for digest in scan.digests:
        try:
            parse_capture_envelope(store.read(digest, access=access))
        except Exception:  # noqa: BLE001 - bodies of every other kind
            continue
        found.add(digest)
    return found


def test_a_capture_handle_names_a_capture_no_accepted_claim_cites_yet(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    """Citing a Capture for the first time is the common case: held and verified is enough."""

    from cruxible_client.contracts.write import capture_handle

    before = _held_captures(instance)
    pending = _write(
        instance,
        _set(WI1, "measured", 3, evidence=report_evidence(tmp_path, "Count: 3")),
        accept="never",
    )
    assert pending.status == "awaiting_approval", pending
    (fresh,) = {
        digest
        for digest in _held_captures(instance) - before
        if _contract_of(instance, digest) == REPORTS.identity.name
    }
    assert fresh not in _accepted_capture_digests(instance)
    handle = capture_handle(fresh)
    # One resolver: the handle the write verbs accept opens in get and
    # read_capture too, though no accepted Claim cites it yet.
    from cruxible_client.contracts.capture_reads import CaptureReadRequest
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
    from cruxible_core.storage.cas import BodyAccessContext

    reader = BodyAccessContext(principal_id="reader", can_read_body=True)
    opened = service_playbill_get(instance, request=GetRequest(ref=handle), access=reader)
    assert opened.ref == f"Capture:{fresh}"
    read = service_read_playbill_capture(
        instance, request=CaptureReadRequest(capture_digest=handle), access=reader
    )
    assert read.capture_digest == fresh and read.status == "verified"
    cited = _write(
        instance, _set(WI2, "measured", 3, evidence={"kind": "capture", "capture": handle})
    )
    assert cited.status == "accepted", cited
    assert cited.changes[0].capture == handle and cited.changes[0].verdict == "supported"
    assert cited_captures(instance, cited.changes[0].claim or "") == {fresh}
    # A held body that is not a Capture never answers a handle.
    body = instance.body_store().store(b"not a capture").digest
    stray = _refusal(
        _write(
            instance,
            _set(WI3, "measured", 3, evidence={"kind": "capture", "capture": capture_handle(body)}),
        )
    )
    assert stray.code == "cruxible.write.capture_not_found"
    assert capture_handle(body) not in stray.candidates


def _accepted_capture_digests(instance: PlaybillInstance) -> set[str]:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        rows = projection.typed.connection.execute("SELECT capture_digest FROM captures")
        return {str(row[0]) for row in rows}


def _contract_of(instance: PlaybillInstance, digest: str) -> str:
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.service.discovery.contract_names import CaptureContractNames
    from cruxible_core.storage.cas import BodyAccessContext

    envelope = parse_capture_envelope(
        instance.body_store().read(
            digest, access=BodyAccessContext(principal_id="test", can_read_body=True)
        )
    )
    return CaptureContractNames(instance, instance.accepted_coordinate()).name(
        envelope.capture_contract_digest
    )


def _seed_record_source(instance: PlaybillInstance) -> tuple[Any, Any]:
    """Accept an external record contract and its provider; answer (contract, provider)."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.captures import (
        capture_contract_digest,
        capture_contract_path,
        render_capture_contract,
    )
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicyV1,
        ClaimEvidenceAdmissionRuleV1,
    )
    from cruxible_client.contracts.providers import provider_path, render_provider
    from cruxible_core.proposals.proposals import ProposalAdmissionRequest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.core_support._pc_c_support import capture_contract, provider

    contract = capture_contract()
    provider_artifact = provider(contract)
    # A field whose evidence is the record, bound to its Subject by the source.
    predicate = f"{KIND}.recorded_status"
    recorded = CLAIM_TYPES[2].model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="order-record",
                        claim_roles=("observation",),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=("database_record",),
                        admission="direct",
                        subject_binding="contract_source_mapping",
                    ),
                )
            ),
        }
    )
    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=OWNER,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/record-source", proposed_base_oid=base.git_oid
        ),
        candidate_tree={
            **instance.tree_at(base.git_oid),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            provider_path(provider_artifact.identity.name): render_provider(provider_artifact),
            claim_type_path(predicate): render_claim_type(recorded),
        },
        timestamp="2026-09-30T11:00:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    return contract, provider_artifact


def _record_capture(
    instance: PlaybillInstance,
    contract: Any,
    provider_artifact: Any,
    subject: str,
    *,
    exact: bool = True,
    observed_at: Any = None,
) -> str:
    """Store (and cite nowhere) external record Captures whose selector names ``subject``.

    The reader stores the record as a canonical value; with ``exact`` the same
    record is also stored as exact bytes, which a Claim can cite, and that one
    is answered.
    """

    from cruxible_core.evidence.source_readers import (
        ExternalSourceReadRequestV1,
        FakeVersionedExternalSourceReader,
        ProducerBindingV1,
    )
    from tests.core_support._pc_c_support import NOW, digest, provider_run

    kind, subject_id = subject.split("/", 1)
    selector = {
        "relation": "orders",
        "key": {"order_id": subject_id},
        "semantic_subject": SemanticAddress.whole_artifact(
            subject_path(kind, subject_id)
        ).model_dump(mode="json"),
    }
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate="0/16B6C50",
        selector_type="relation-primary-key-v1",
        selector=selector,
        value={"order_id": subject_id, "status": "ready"},
    )
    acquired = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=provider_artifact,
            binding=ProducerBindingV1(
                provider=provider_artifact.identity,
                logical_source_identity="commerce.production.orders",
                adapter_digest=digest("test-adapter", "postgres-v1"),
            ),
            coordinate_type="postgres-lsn-v1",
            coordinate="0/16B6C50",
            selector_type="relation-primary-key-v1",
            selector=selector,
            materialization="cas",
            run_coordinate=provider_run(provider_artifact),
            observed_at=NOW if observed_at is None else observed_at,
            resource_budget=contract.selection_budget,
        ),
        store=instance.body_store(),
    )
    if not exact:
        return str(acquired.capture_digest)
    # The reader commits the record as a canonical value, which a Claim cannot
    # map a byte span onto; the same record committed as exact bytes can be cited.
    from cruxible_client.contracts.captures import render_capture_envelope

    store = instance.body_store()
    body = f'{{"order_id":"{subject_id}","status":"ready"}}'.encode()
    exact = acquired.envelope.model_dump(mode="json")
    exact["commitment"] = {
        "tag": "playbill-evidence-commitment-v1",
        "digest_kind": "exact_bytes",
        "digest": store.store(body).digest,
        "byte_length": len(body),
        "materialization": "cas",
    }
    envelope = type(acquired.envelope).model_validate(exact)
    return store.store(render_capture_envelope(envelope)).digest


def test_contract_evidence_finds_an_uncited_capture_whose_source_names_the_subject(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.write import capture_handle

    contract, provider_artifact = _seed_record_source(instance)
    held = _record_capture(instance, contract, provider_artifact, WI1)
    assert held not in _accepted_capture_digests(instance)
    by_contract = {"kind": "contract", "contract": contract.identity.name}
    outcome = _write(instance, _set(WI1, "recorded_status", "ready") | {"evidence": by_contract})
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].capture == capture_handle(held)
    assert outcome.changes[0].verdict == "supported"
    assert cited_captures(instance, outcome.changes[0].claim or "") == {held}
    # Its selector names wi-1 exactly, so it is nobody else's.
    other = _refusal(
        _write(instance, _set(WI2, "recorded_status", "ready") | {"evidence": by_contract})
    )
    assert other.code == "cruxible.write.contract_capture_not_found"


def test_a_newest_capture_no_claim_can_cite_is_named_not_skipped(
    instance: PlaybillInstance,
) -> None:
    """Only a canonical-value record: a refusal naming it, never 'capture it first'."""

    from datetime import timedelta

    from cruxible_client.contracts.write import capture_handle
    from tests.core_support._pc_c_support import NOW

    contract, provider_artifact = _seed_record_source(instance)
    canonical = _record_capture(instance, contract, provider_artifact, WI1, exact=False)
    by_contract = {"kind": "contract", "contract": contract.identity.name}
    only = _refusal(
        _write(instance, _set(WI1, "recorded_status", "ready") | {"evidence": by_contract})
    )
    assert only.code == "cruxible.write.contract_capture_not_citable"
    assert only.candidates == (capture_handle(canonical),)
    assert capture_handle(canonical) in only.message and "canonical value" in only.message
    assert only.repair is not None and "exact bytes" in only.repair
    assert "first" not in only.repair

    # An older exact-bytes record is cited, with a note naming the newer one.
    older = _record_capture(
        instance, contract, provider_artifact, WI1, observed_at=NOW - timedelta(hours=1)
    )
    outcome = _write(instance, _set(WI1, "recorded_status", "ready") | {"evidence": by_contract})
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].capture == capture_handle(older)
    (note,) = [
        item for item in outcome.warnings if item.code != "cruxible.write.verdict_not_supported"
    ]
    assert note.code == "cruxible.write.newer_capture_not_citable"
    assert (note.change, note.capture) == (0, capture_handle(canonical))  # type: ignore[union-attr]
    assert "verdict" not in note.model_dump(mode="json")
    assert capture_handle(older) in note.message and "canonical value" in note.message
    assert outcome.next != note.repair
    # A dry run says the same.
    preview = _write(
        instance, _set(WI1, "recorded_status", "done") | {"evidence": by_contract}, dry_run=True
    )
    assert preview.status == "would_accept", preview
    assert "cruxible.write.newer_capture_not_citable" in {item.code for item in preview.warnings}


def test_a_handle_matching_more_captures_than_the_verification_budget_refuses(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real prefix scan runs out: that is never taken as a unique match."""

    from cruxible_client.contracts.errors import WriteRefusalError

    for index, subject in enumerate((WI1, WI2, WI3)):
        assert _write(instance, _set(subject, "title", f"T{index}")).status == "accepted"
    head = instance.accepted_coordinate()
    planner = write_verbs._Planner(
        instance,
        head=head,
        read_at=head,
        request=WriteRequest.model_validate({"because": "x", "changes": [_set(WI1, "title", "x")]}),
    )
    from cruxible_core.service.evidence import capture_reads

    monkeypatch.setattr(capture_reads, "CAPTURE_HANDLE_MAX_VERIFIED", 1)
    with pytest.raises(WriteRefusalError) as caught:
        planner.capture_by_handle("CAP-", index=0, path="changes[0].evidence.capture")
    assert caught.value.error_code == "cruxible.write.capture_scan_exhausted"
    assert "longer handle" in (caught.value.repair_line or "")


def test_a_handle_in_a_crowded_shard_stops_at_the_work_limit(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Almost nothing in the shard matches the full prefix, and the scan still stops."""

    shard = instance.body_store().root / "sha256" / "ab"
    shard.mkdir(exist_ok=True)
    for index in range(3000):
        (shard / f"ab{index:062x}").touch()
    from cruxible_core.service.evidence import capture_reads

    monkeypatch.setattr(capture_reads, "CAPTURE_HANDLE_SCAN_BUDGET", 500)
    refusal = _refusal(
        _write(
            instance,
            _set(WI1, "measured", 3, evidence={"kind": "capture", "capture": "CAP-abffffffffff"}),
        )
    )
    assert refusal.code == "cruxible.write.capture_scan_exhausted"
    scan = instance.body_store().scan("abffffffffff", budget=500, nearest=8)
    assert not scan.complete and scan.examined <= 500 and len(scan.nearest) <= 8


def test_an_ambiguous_capture_handle_refuses_with_the_longer_handles(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "sha256:" + "a" * 12
    found = [prefix + "0" * 52, prefix + "1" * 52]
    from cruxible_core.service.evidence import capture_reads

    monkeypatch.setattr(
        capture_reads,
        "retained_captures",
        lambda _instance, **_kw: capture_reads.RetainedCaptureInventory(
            captures=tuple(
                capture_reads.RetainedCapture(digest=item, envelope=None)  # type: ignore[arg-type]
                for item in found
            ),
            complete=True,
        ),
    )
    monkeypatch.setattr(write_verbs._Planner, "_verified_capture", lambda _self, _digest: True)
    refusal = _refusal(
        _write(
            instance,
            _set(WI1, "measured", 3, evidence={"kind": "capture", "capture": "CAP-" + "a" * 12}),
        )
    )
    assert refusal.code == "cruxible.write.capture_ambiguous"
    assert refusal.candidates == ("CAP-" + "a" * 12 + "0", "CAP-" + "a" * 12 + "1")


def test_contract_evidence_cites_the_newest_capture_about_the_subject(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    from cruxible_client.contracts.write import capture_handle

    older = _write(
        instance, _set(WI1, "measured", 3, evidence=report_evidence(tmp_path, "Count: 3"))
    )
    newer = _write(
        instance, _add(WI1, "labels", "four") | {"evidence": report_evidence(tmp_path, "Count: 4")}
    )
    assert older.status == newer.status == "accepted"
    (newest,) = cited_captures(instance, newer.changes[0].claim or "")
    assert {newest} != cited_captures(instance, older.changes[0].claim or "")

    by_contract = {"kind": "contract", "contract": REPORTS.identity.name}
    outcome = _write(instance, _set(WI1, "measured", 4) | {"evidence": by_contract})
    assert outcome.status == "accepted", outcome
    (change,) = outcome.changes
    assert change.capture == capture_handle(newest) and change.verdict == "supported"
    assert newest in cited_captures(instance, change.claim or "")
    qualified = {"kind": "contract", "contract": f"CaptureContract:{REPORTS.identity.name}"}
    assert _write(instance, _add(WI1, "labels", "again") | {"evidence": qualified}).status == (
        "accepted"
    )

    # wi-2 has no Capture under the contract: refused, with a repair.
    none = _refusal(_write(instance, _add(WI2, "labels", "counted") | {"evidence": by_contract}))
    assert none.code == "cruxible.write.contract_capture_not_found"
    assert none.field_path == "changes[0].evidence.contract"
    assert none.repair is not None and "CAP-" in none.repair
    typo = _refusal(
        _write(
            instance,
            _add(WI1, "labels", "x")
            | {"evidence": {"kind": "contract", "contract": "repo.reprots"}},
        )
    )
    assert typo.code == "cruxible.write.unknown_contract"
    # Self-source Captures are bound to their own Claim, so they are never picked.
    self_source = {
        "kind": "contract",
        "contract": COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name,
    }
    _write(instance, _set(WI2, "title", "Named"))
    refused = _refusal(
        _write(instance, _set(WI2, "priority", "high", role="normative", evidence=self_source))
    )
    assert refused.code == "cruxible.write.contract_capture_not_found"


def test_file_evidence_must_be_observed_by_the_writer(instance: PlaybillInstance) -> None:
    unobserved = _refusal(
        _write(
            instance,
            _set(WI1, "title", "x", evidence={"kind": "file", "file": "notes.md#Title"}),
        )
    )
    assert unobserved.code == "cruxible.write.file_evidence_unobserved"


def test_explicit_self_evidence_backs_the_value(instance: PlaybillInstance) -> None:
    outcome = _write(
        instance,
        _set(WI1, "title", "Named", evidence={"kind": "self", "self": "The page says so."}),
    )
    assert outcome.status == "accepted", outcome


# -- stale writes (decision a, Q05) ----------------------------------------------


def test_a_set_refuses_when_its_slot_moved_since_the_read_coordinate(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    read_at = first.coordinate.git_oid
    other = _write(instance, _set(WI1, "status", "blocked"), because="Someone else saw it.")
    assert other.status == "accepted"
    # An unrelated slot moving does not refuse.
    assert _write(instance, _set(WI2, "status", "done"), at=read_at).status == "accepted"

    stale = _write(instance, _set(WI1, "status", "done"), at=read_at)
    refusal = _refusal(stale)
    assert refusal.code == "cruxible.write.slot_changed"
    assert "'blocked'" in refusal.message and "owner" in refusal.message
    assert f"generation {other.coordinate.generation}" in refusal.message
    assert refusal.candidates == (first.changes[0].claim,)
    assert "contend" in (refusal.repair or "")

    contended = _write(instance, _set(WI1, "status", "done", contend=True), at=read_at)
    assert contended.status == "accepted", contended
    assert contended.changes[0].contenders_created == (first.changes[0].claim,)
    contested = _refusal(_write(instance, _set(WI1, "status", "ready")))
    assert contested.code == "cruxible.write.slot_contested"
    assert len(contested.candidates) == 2


def _no_proposal_to_activate(instance: PlaybillInstance) -> None:
    assert service_list_playbill_proposals(instance, status="open").entries == ()


@pytest.mark.parametrize("slot", ["replaced", "filled"])
@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_a_competing_set_between_planning_and_submit_refuses_leaving_nothing_to_activate(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, pinned: bool, slot: str
) -> None:
    """The slot is held to the planned coordinate through admission, not only before it."""

    subject = WI1 if slot == "replaced" else WI2
    _write(instance, _set(WI1, "status", "ready"))
    read_at = instance.accepted_coordinate().git_oid
    real = write_verbs._coordinator
    interleaved: list[WriteOutcome | None] = []

    def coordinator(target: PlaybillInstance, claim_ids: Any) -> Any:
        # Planning is done; a competing set lands before this write is admitted.
        if not interleaved:
            interleaved.append(None)
            interleaved[0] = _write(
                target, _set(subject, "status", "blocked"), because="Someone else."
            )
        return real(target, claim_ids)

    monkeypatch.setattr(write_verbs, "_coordinator", coordinator)
    options = {"at": read_at} if pinned else {}
    outcome = _write(instance, _set(subject, "status", "done"), **options)
    assert interleaved[0] is not None and interleaved[0].status == "accepted"
    refusal = _refusal(outcome)
    assert refusal.code == "cruxible.write.slot_changed", refusal
    assert "'blocked'" in refusal.message
    assert outcome.proposal is None
    assert _values(instance, subject, "status") == ["blocked"]
    _no_proposal_to_activate(instance)


def _contend(target: PlaybillInstance) -> None:
    contender = _write(target, _set(WI1, "status", "blocked", contend=True))
    assert contender.status == "accepted", contender


_RETIRE_BY_SLOT = {"op": "retire", "target": {"subject": WI1, "field": "status"}}


@pytest.mark.parametrize("stage", ["before_create", "before_submit"])
@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_a_contender_joining_the_slot_before_admission_refuses_the_retire_unproposed(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, pinned: bool, stage: str
) -> None:
    """Admission checks the slot's live membership, so nothing is left to activate."""

    ready = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    read_at = instance.accepted_coordinate().git_oid
    landed: list[bool] = []
    if stage == "before_create":
        real = write_verbs._coordinator

        def coordinator(target: PlaybillInstance, claim_ids: Any) -> Any:
            if not landed:
                landed.append(True)
                _contend(target)
            return real(target, claim_ids)

        monkeypatch.setattr(write_verbs, "_coordinator", coordinator)
    else:
        submit = AuthoringIntentCoordinator.submit

        def submitting(self: AuthoringIntentCoordinator, *args: Any, **kwargs: Any) -> Any:
            if not landed:
                landed.append(True)
                _contend(self.instance)
            return submit(self, *args, **kwargs)

        monkeypatch.setattr(AuthoringIntentCoordinator, "submit", submitting)
    options = {"at": read_at} if pinned else {}
    outcome = _write(instance, _RETIRE_BY_SLOT, **options)
    assert landed
    assert _refusal(outcome).code == "cruxible.write.slot_changed", outcome
    assert outcome.proposal is None
    assert _values(instance, WI1, "status") == ["blocked", "ready"]
    assert ready in _live_status_claims(instance)
    _no_proposal_to_activate(instance)


@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_a_contender_between_preflight_and_admission_publishes_nothing(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """Admission is bound to the coordinate preflight checked the slot at."""

    from cruxible_core.proposals.proposals import ProposalService

    ready = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    read_at = instance.accepted_coordinate().git_oid
    submit = ProposalService.submit
    landed: list[bool] = []

    def submitting(self: ProposalService, **kwargs: Any) -> Any:
        # Preflight has passed; the contender lands before the service reads
        # the coordinate it would evaluate and publish at.
        if not landed and str(kwargs["request"].target_ref).split("/")[-1].startswith("intent-"):
            landed.append(True)
            _contend(instance)
        return submit(self, **kwargs)

    monkeypatch.setattr(ProposalService, "submit", submitting)
    options = {"at": read_at} if pinned else {}
    outcome = _write(instance, _RETIRE_BY_SLOT, **options)
    assert landed
    assert _refusal(outcome).code == "cruxible.write.slot_changed", outcome
    assert outcome.proposal is None
    assert _values(instance, WI1, "status") == ["blocked", "ready"]
    assert ready in _live_status_claims(instance)
    _no_proposal_to_activate(instance)


def _live_status_claims(instance: PlaybillInstance) -> set[str]:
    return {
        item.identity.removeprefix("Claim:")
        for item in read_live_values(
            instance,
            instance.accepted_coordinate(),
            subject_paths=(subject_path(KIND, "wi-1"),),
            predicates=(f"{KIND}.status",),
        )
    }


def test_a_contender_after_admission_makes_the_writes_own_activation_refuse(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.authoring import documents

    ready = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    activate = documents.service_activate_playbill_proposal
    landed: list[bool] = []

    def activating(target: PlaybillInstance, **kwargs: Any) -> Any:
        if not landed:
            landed.append(True)
            _contend(target)
        return activate(target, **kwargs)

    monkeypatch.setattr(documents, "service_activate_playbill_proposal", activating)
    outcome = _write(instance, _RETIRE_BY_SLOT)
    assert landed
    assert _refusal(outcome).code == "cruxible.write.slot_changed", outcome
    assert _values(instance, WI1, "status") == ["blocked", "ready"]
    assert ready in _live_status_claims(instance)


def test_a_contender_after_admission_refuses_activation_and_readmission(
    instance: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.errors import (
        ProposalReadmitRequiresResubmission,
        SettlementIntegrityError,
    )
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal

    ready = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    pending = _write(instance, _RETIRE_BY_SLOT, accept="never")
    assert pending.status == "awaiting_approval" and pending.proposal is not None
    _contend(instance)
    with pytest.raises(SettlementIntegrityError):
        service_activate_playbill_proposal(
            instance, proposal_id=pending.proposal.proposal_id, activated_by="owner"
        )
    with pytest.raises(ProposalReadmitRequiresResubmission, match="slot membership"):
        service_readmit_playbill_proposal(
            instance, proposal_id=pending.proposal.proposal_id, actor_id="owner"
        )
    assert _values(instance, WI1, "status") == ["blocked", "ready"]
    assert ready in _live_status_claims(instance)


def test_resetting_at_the_new_head_replaces_the_value(instance: PlaybillInstance) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    second = _write(instance, _set(WI1, "status", "blocked"))
    again = _write(instance, _set(WI1, "status", "done"), at=second.coordinate.git_oid)
    assert again.status == "accepted"
    assert again.changes[0].revises == first.changes[0].claim


# -- retire -------------------------------------------------------------------------


def test_retire_by_claim_id_or_by_slot(instance: PlaybillInstance) -> None:
    status = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    title = _write(instance, _set(WI1, "title", "Old")).changes[0].claim
    by_id = _write(instance, {"op": "retire", "target": f"Claim:{status}"})
    assert by_id.status == "accepted", by_id
    assert by_id.changes[0].claim == status and by_id.changes[0].before == "ready"
    by_slot = _write(
        instance, {"op": "retire", "target": {"subject": WI1, "field": "title"}}, because="Gone."
    )
    assert by_slot.status == "accepted" and by_slot.changes[0].claim == title
    assert _values(instance, WI1, "status") == [] and _values(instance, WI1, "title") == []

    again = _refusal(_write(instance, {"op": "retire", "target": status}))
    assert again.code == "cruxible.write.claim_not_live"
    empty = _refusal(
        _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "title"}})
    )
    assert empty.code == "cruxible.write.slot_empty"


def test_retire_names_the_values_of_an_ambiguous_many_valued_slot(
    instance: PlaybillInstance,
) -> None:
    _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    refusal = _refusal(
        _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "governs"}})
    )
    assert refusal.code == "cruxible.write.slot_ambiguous" and len(refusal.candidates) == 2


def test_one_write_may_retire_one_contender_and_replace_the_other(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    second = _write(instance, _set(WI1, "status", "blocked", contend=True)).changes[0].claim
    outcome = _write(
        instance,
        _set(WI1, "status", "done"),
        {"op": "retire", "target": second, "because": "It was a mistake."},
    )
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].revises == first
    assert _values(instance, WI1, "status") == ["done"]
    current = outcome.changes[0].claim
    # Retiring the live Claim while setting the field states a new one beside nothing.
    fresh = _write(instance, _set(WI1, "status", "ready"), {"op": "retire", "target": current})
    assert fresh.status == "accepted", fresh
    assert fresh.changes[0].revises is None and fresh.changes[0].claim != current
    assert _values(instance, WI1, "status") == ["ready"]
    twice = _write(
        instance,
        {"op": "retire", "target": fresh.changes[0].claim},
        {"op": "retire", "target": {"subject": WI1, "field": "status"}},
    )
    assert _refusal(twice).code == "cruxible.write.claim_changed_twice"


# -- expect: compare-and-set by value ------------------------------------------------


def test_set_with_expect_replaces_only_the_value_it_expected(instance: PlaybillInstance) -> None:
    first = _write(instance, _set(WI1, "status", "ready", expect=[]))
    assert first.status == "accepted", first
    head = instance.accepted_coordinate().git_oid

    refused = _write(instance, _set(WI1, "status", "done", expect="blocked"))
    refusal = _refusal(refused)
    assert refusal.code == "cruxible.write.slot_changed"
    assert refusal.field_path == "changes[0].expect"
    assert "holds 'ready', not 'blocked' as expected" in refusal.message
    assert refusal.candidates == (first.changes[0].claim,)
    assert refusal.repair is not None and '"ready"' in refusal.repair
    assert instance.accepted_coordinate().git_oid == head
    assert _refusal(_write(instance, _set(WI1, "status", "done", expect=[]))).code == (
        "cruxible.write.slot_changed"
    )

    replaced = _write(instance, _set(WI1, "status", "done", expect="ready"))
    assert replaced.status == "accepted", replaced
    assert replaced.changes[0].revises == first.changes[0].claim
    assert _values(instance, WI1, "status") == ["done"]

    empty = _refusal(_write(instance, _set(WI2, "status", "done", expect="ready")))
    assert empty.code == "cruxible.write.slot_changed" and "holds no value" in empty.message


def test_expect_is_checked_like_a_value_before_it_is_compared(instance: PlaybillInstance) -> None:
    member = _refusal(_write(instance, _set(WI1, "status", "done", expect="dne")))
    assert member.code == "cruxible.write.value_not_member"
    assert member.field_path == "changes[0].expect"
    # The CLI's text spelling of an integer is read by the field's type.
    assert _write(instance, _set(WI1, "measured", 3)).status == "accepted"
    assert _write(instance, _set(WI1, "measured", 4, expect="3")).status == "accepted"
    wrong = _refusal(_write(instance, _set(WI1, "measured", 5, expect="3")))
    assert wrong.code == "cruxible.write.slot_changed" and "holds 4" in wrong.message


def test_retire_with_expect_compares_every_live_value_of_the_slot(
    instance: PlaybillInstance,
) -> None:
    links = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    by_value = {item.after: item.claim for item in links.changes}
    partial = _refusal(_write(instance, {"op": "retire", "target": by_value[WI2], "expect": [WI2]}))
    assert partial.code == "cruxible.write.slot_changed"
    assert set(partial.candidates) == set(by_value.values())
    retired = _write(instance, {"op": "retire", "target": by_value[WI2], "expect": [WI3, WI2]})
    assert retired.status == "accepted", retired
    assert _values(instance, WI1, "governs") == [WI3]

    _write(instance, _set(WI1, "title", "Old"))
    slot = {"subject": WI1, "field": "title"}
    stale = _refusal(_write(instance, {"op": "retire", "target": slot, "expect": "New"}))
    assert stale.code == "cruxible.write.slot_changed" and "holds 'Old'" in stale.message
    ended = _write(instance, {"op": "retire", "target": slot, "expect": "Old"})
    assert ended.status == "accepted", ended


def test_add_with_expect_absent_refuses_what_would_be_already_done(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, {**_add(WI1, "governs", WI2), "expect_absent": True})
    assert first.status == "accepted", first
    again = _refusal(_write(instance, {**_add(WI1, "governs", WI2), "expect_absent": True}))
    assert again.code == "cruxible.write.value_already_present"
    assert again.candidates == (first.changes[0].claim,)
    assert again.field_path == "changes[0].expect_absent"
    # Without it, the same add is answered as done.
    assert _write(instance, _add(WI1, "governs", WI2)).changes[0].already_live


def test_expect_composes_with_the_read_coordinate(instance: PlaybillInstance) -> None:
    _write(instance, _set(WI1, "status", "ready"))
    read_at = instance.accepted_coordinate().git_oid
    # Unmoved since the read, and holding what was expected: accepted.
    assert _write(instance, _set(WI1, "status", "blocked", expect="ready"), at=read_at).status == (
        "accepted"
    )
    # Moved back to the expected value since the read: the value matches, but the
    # read coordinate still refuses the slot that moved.
    _write(instance, _set(WI1, "status", "ready"), because="Back again.")
    moved = _refusal(_write(instance, _set(WI1, "status", "done", expect="ready"), at=read_at))
    assert moved.code == "cruxible.write.slot_changed" and "after your read" in moved.message


def test_a_contender_after_the_expect_check_still_refuses_at_admission(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matched Claim IDs are pinned, so a value joining the slot later refuses."""

    ready = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    real = write_verbs._coordinator
    landed: list[bool] = []

    def coordinator(target: PlaybillInstance, claim_ids: Any) -> Any:
        if not landed:
            landed.append(True)
            _contend(target)
        return real(target, claim_ids)

    monkeypatch.setattr(write_verbs, "_coordinator", coordinator)
    outcome = _write(instance, _set(WI1, "status", "done", expect="ready"))
    assert landed
    refusal = _refusal(outcome)
    assert refusal.code == "cruxible.write.slot_changed", refusal
    assert outcome.proposal is None
    assert _values(instance, WI1, "status") == ["blocked", "ready"]
    assert ready in _live_status_claims(instance)
    _no_proposal_to_activate(instance)


# -- the write's default subject ------------------------------------------------------


def test_changes_that_name_no_subject_take_the_writes_own(instance: PlaybillInstance) -> None:
    _write(instance, _set(WI1, "title", "Old"))
    outcome = _write(
        instance,
        {"op": "set", "field": "status", "value": "ready"},
        {"op": "add", "field": "governs", "value": WI3},
        {"op": "set", "subject": WI2, "field": "status", "value": "done"},
        {"op": "retire", "target": {"field": "title"}},
        subject=WI1,
    )
    assert outcome.status == "accepted", outcome
    assert [item.subject for item in outcome.changes] == [WI1, WI1, WI2, WI1]
    assert _values(instance, WI1, "status") == ["ready"]
    assert _values(instance, WI1, "governs") == [WI3]
    assert _values(instance, WI2, "status") == ["done"]
    assert _values(instance, WI1, "title") == []


def test_a_change_with_no_subject_and_no_default_refuses_by_name(
    instance: PlaybillInstance,
) -> None:
    refusal = _refusal(
        _write(
            instance, _set(WI1, "status", "ready"), {"op": "add", "field": "governs", "value": WI2}
        )
    )
    assert refusal.code == "cruxible.write.subject_required"
    assert (refusal.change, refusal.field_path) == (1, "changes[1].subject")
    retire = _refusal(_write(instance, {"op": "retire", "target": {"field": "title"}}))
    assert retire.code == "cruxible.write.subject_required"
    assert retire.field_path == "changes[0].target.subject"
    assert _write(
        instance, {"op": "retire", "target": {"field": "title"}}, dry_run=True
    ).status == ("would_refuse")


# -- dry runs (R12) -----------------------------------------------------------------


def _snapshot(instance: PlaybillInstance) -> dict[str, tuple[int, int, str]]:
    """Every file under the managed root, after background ref upkeep has settled.

    An acceptance queues advisory review-ref upkeep on a worker thread; that is
    not the dry run's, so it is settled first. The derived history index is
    deliberately left as it is: after an acceptance it is behind the head, and a
    dry run must leave it that way.
    """

    instance.settled_workspace_advertisement()
    root = instance.root
    found: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            found[str(path.relative_to(root))] = (
                len(data),
                path.stat().st_mtime_ns,
                hashlib.sha256(data).hexdigest(),
            )
    return found


def test_a_dry_run_takes_the_write_path_and_writes_nothing(instance: PlaybillInstance) -> None:
    _write(instance, _set(WI1, "status", "ready"))
    before = _snapshot(instance)
    preview = _write(
        instance,
        _set(WI1, "status", "done"),
        _set(f"{KIND}/wi-new", "ruling", "A new ruling nobody stored yet.\n"),
        dry_run=True,
    )
    assert preview.status == "would_accept", preview
    assert _snapshot(instance) == before
    assert preview.subjects_added == (f"{KIND}/wi-new",)
    assert preview.changes[0].before == "ready" and preview.changes[0].revises is not None
    assert preview.proposal is None
    assert preview.next is not None and preview.coordinate.git_oid in preview.next

    refused = _write(instance, _set(WI1, "status", "dne"), dry_run=True)
    assert refused.status == "would_refuse"
    committed = _write(instance, _set(WI1, "status", "done"), at=preview.coordinate.git_oid)
    assert committed.status == "accepted"
    assert committed.changes[0].revises == preview.changes[0].revises


@pytest.mark.parametrize("cache", ["cold", "behind_head"])
def test_a_dry_run_leaves_a_cold_or_behind_history_index_as_it_found_it(
    instance: PlaybillInstance, cache: str
) -> None:
    """The dry run's history reads build nothing on disk, however stale the index."""

    read_at = instance.accepted_coordinate().git_oid
    _write(instance, _set(WI1, "status", "ready"))
    instance.settled_workspace_advertisement()
    kept = {path: path.read_bytes() for path in instance.root.rglob("history.sqlite3*")}
    assert kept
    if cache == "behind_head":
        _write(instance, _set(WI2, "status", "ready"))
        instance.settled_workspace_advertisement()
    for path in instance.root.rglob("history.sqlite3*"):
        path.unlink()
    if cache == "behind_head":
        # The index as it stood one generation ago: behind the head.
        for path, data in kept.items():
            path.write_bytes(data)
    before = _snapshot(instance)
    preview = _write(
        instance,
        _set(WI3, "status", "done"),
        _add(WI1, "governs", WI2),
        at=read_at,
        dry_run=True,
    )
    assert preview.status == "would_accept", preview
    assert _snapshot(instance) == before
    stale = _write(instance, _set(WI1, "status", "done"), at=read_at, dry_run=True)
    assert _refusal(stale).code == "cruxible.write.slot_changed"
    assert _snapshot(instance) == before


def test_a_dry_run_reports_a_refusal_found_only_by_preflight(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(write_verbs, "_with_dispositions", lambda member, _claims: member)
    before = _snapshot(instance)
    preview = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3), dry_run=True)
    assert preview.status == "would_refuse"
    assert _refusal(preview).code == "cruxible.authoring.existing_claim_dispositions_incomplete"
    assert _snapshot(instance) == before


# -- accept (decision d) --------------------------------------------------------------


def test_independent_approval_leaves_the_write_awaiting_the_named_approvers(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    preview = _write(instance, _set(WI1, "status", "ready"), dry_run=True)
    assert preview.status == "would_await_approval"
    assert preview.approval is not None and preview.approval.eligible_approvers == ("reviewer",)

    outcome = _write(instance, _set(WI1, "status", "ready"), surface="cli")
    assert outcome.status == "awaiting_approval", outcome
    assert outcome.proposal is not None
    approval = outcome.approval
    assert approval is not None
    assert approval.reason == "independent_approval_required"
    assert approval.eligible_approvers == ("reviewer",)
    pid = outcome.proposal.proposal_id
    assert approval.approve == (
        f"cruxible proposal approve {pid} --signer-id reviewer --key <reviewer.ed25519>"
    )
    assert approval.activate == f"cruxible proposal activate {pid}"
    assert outcome.next == approval.approve
    assert _values(instance, WI1, "status") == []


@pytest.mark.parametrize(
    ("surface", "approve", "activate"),
    [
        (
            "mcp",
            'cruxible_proposal_approve(proposal_id="{pid}", signer_id="reviewer")',
            'cruxible_proposal_activate(proposal_id="{pid}")',
        ),
        (
            "sdk",
            'cx.proposal("{pid}").approve(signer=<reviewer signer>, '
            'reviewed=cx.proposal("{pid}").review())',
            'cx.proposal("{pid}").activate()',
        ),
    ],
)
def test_the_approve_call_is_rendered_for_the_callers_surface(
    tmp_path: Path, surface: str, approve: str, activate: str
) -> None:
    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    outcome = _write(instance, _set(WI1, "status", "ready"), surface=surface)
    assert outcome.proposal is not None and outcome.approval is not None
    pid = outcome.proposal.proposal_id
    assert outcome.approval.approve == approve.format(pid=pid)
    assert outcome.approval.activate == activate.format(pid=pid)


def test_accept_never_and_a_tier_without_activation_stop_at_the_proposal(
    instance: PlaybillInstance,
) -> None:
    never = _write(instance, _set(WI1, "status", "ready"), accept="never")
    assert never.status == "awaiting_approval"
    assert never.approval is not None and never.approval.reason == "accept_never"
    assert never.approval.approve is None and never.next == never.approval.activate
    tier = _write(instance, _set(WI2, "status", "ready"), who=caller(may_activate=False))
    assert tier.status == "awaiting_approval"
    assert tier.approval is not None and tier.approval.reason == "activation_not_permitted"
    assert _values(instance, WI1, "status") == [] and _values(instance, WI2, "status") == []


def test_an_unaccepted_read_coordinate_refuses_as_an_outcome(instance: PlaybillInstance) -> None:
    outcome = _write(instance, _set(WI1, "status", "ready"), at="f" * 40)
    assert outcome.status == "refused"
    assert _refusal(outcome).code == "cruxible.read.coordinate_not_accepted"


def test_a_contest_row_offers_one_keep_option_per_contender_and_picks_none(
    instance: PlaybillInstance,
) -> None:
    """The next row names no winner: each option keeps one contender and retires the rest."""

    from datetime import UTC, datetime

    from cruxible_client.contracts.captures import CanonicalDuration
    from cruxible_core.coverage.contracts import CoverageAccessProfile
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.service.discovery.next import NextRequestV1, service_playbill_next

    first = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    second = _write(instance, _set(WI1, "status", "blocked", contend=True)).changes[0].claim

    def conflict_rows() -> list[Any]:
        request = NextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=datetime.now(UTC),
            access_profile=CoverageAccessProfile(
                profile_id="write-contest", permitted_access_classes=("instance", "public")
            ),
            expiring_within=CanonicalDuration(microseconds=604_800_000_000),
        )
        return [
            item
            for item in service_playbill_next(instance, request=request).items
            if item.reason == "claim_conflicted"
        ]

    (row,) = conflict_rows()
    repair = row.repair
    assert repair.operation == "cruxible.write"
    assert repair.command is None  # no single command: the choice is the caller's
    assert "changes" not in repair.arguments
    options = {item["keep"]: item["changes"] for item in repair.arguments["options"]}
    assert options == {
        first: [{"op": "retire", "target": second}],
        second: [{"op": "retire", "target": first}],
    }

    # Each option is a runnable write; running one resolves the contest.
    outcome = _write(instance, *options[second], because="The later reading is right.")
    assert outcome.status == "accepted", outcome
    assert conflict_rows() == []
    assert _values(instance, WI1, "status") == ["blocked"]
