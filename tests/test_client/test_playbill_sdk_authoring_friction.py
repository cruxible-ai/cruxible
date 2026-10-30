"""Concise authoring preserves explicit evidence and lowering semantics."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import ClaimRole, Cruxible
from cruxible_client import contracts as api
from tests.support.preflight_results import stub_diagnostic, stub_preflight_result
from tests.test_client.test_playbill_sdk import _COORDINATE, _Client, _workspace


@pytest.fixture
def pb(tmp_path: Path):
    _workspace(tmp_path)
    return Cruxible._from_client(_Client(), instance_id="inst_test", workspace=tmp_path)


def claim_args():
    return dict(
        subject="sec.vuln/cve-2026-0001",
        predicate="sec.vuln.affects_package",
        value="sec.package/demo",
        role=ClaimRole.OBSERVATION,
        rationale="Record observed package.",
    )


@pytest.mark.parametrize("batch", [False, True])
def test_omitted_options_preserve_payload_expectations_and_stamp(pb, batch):
    def build(**options):
        target = pb.changes(rationale="Package observation") if batch else pb
        result = target.claim(**claim_args(), self_source="affected package", **options)
        return result._compiled() if batch else result

    concise = build()
    explicit = build(
        supported_by=None,
        copied_from=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )
    assert concise.payload == explicit.payload
    assert concise.reference_expectations == explicit.reference_expectations
    assert concise.program_stamp == explicit.program_stamp


@pytest.mark.parametrize("batch", [False, True])
def test_evidence_choice_and_claim_rationale_remain_required(pb, batch):
    target = pb.changes(rationale="Whole change rationale") if batch else pb
    with pytest.raises(ValueError, match="exactly one"):
        target.claim(**claim_args())
    args = claim_args()
    del args["rationale"]
    with pytest.raises(TypeError, match="rationale"):
        target.claim(**args, self_source="affected package")


@pytest.mark.parametrize("batch", [False, True])
def test_claim_read_subject_path_round_trips_to_authoring(pb, batch):
    def build(subject):
        target = pb.changes(rationale="Read-modify-write") if batch else pb
        args = {**claim_args(), "subject": subject}
        draft = target.claim(**args, self_source="affected package")
        return draft._compiled() if batch else draft

    shorthand = build("sec.vuln/cve-2026-0001")
    returned_path = build("subjects/sec.vuln/cve-2026-0001.json")
    assert returned_path.payload == shorthand.payload
    assert returned_path.reference_expectations == shorthand.reference_expectations


@pytest.mark.parametrize(
    "subject",
    [
        "subjects/../sec.vuln/cve-2026-0001.json",
        "subjects/sec.vuln/cve-2026-0001.json/extra.json",
        "subjects/sec.vuln/cve-2026-0001.json#fragment",
    ],
)
def test_noncanonical_read_subject_paths_are_refused(pb, subject):
    with pytest.raises(ValueError, match="canonical"):
        pb.claim(**{**claim_args(), "subject": subject}, self_source="affected package")


def test_proposal_uses_submit_result_without_status_read(pb, monkeypatch):
    intent = pb.claim(**claim_args(), self_source="affected package").prepare()
    assert intent.proposal is None
    status = api.CandidateStatusRecord(
        state="ready_to_activate",
        proposal_id="proposal-one",
        current_accepted_coordinate=_COORDINATE,
    )
    calls = []

    def submit(*args, **kwargs):
        assert kwargs == {"accept": "if_allowed"}
        calls.append("submit")
        return api.AuthoringSubmitResultRecord(
            intent=intent._raw,
            status=status,
            outcome=api.AuthoringProposed(
                proposal_id=status.proposal_id, reason="accept_never", message="held"
            ),
        )

    def refresh(*args):
        calls.append("status")
        return status.model_copy(update={"proposal_id": "proposal-two"})

    monkeypatch.setattr(pb._client, "submit_authoring_intent", submit, raising=False)
    monkeypatch.setattr(pb._client, "authoring_intent_status", refresh, raising=False)
    intent.submit()
    original = intent.proposal
    assert original.proposal_id == "proposal-one"
    assert intent.proposal.proposal_id == "proposal-one"
    assert calls == ["submit"]
    intent.status()
    assert intent.proposal.proposal_id == "proposal-two"
    assert original.proposal_id == "proposal-one"
    assert calls == ["submit", "status"]


@pytest.mark.parametrize("operation", ["prepare", "reprepare", "submit"])
@pytest.mark.parametrize("fails", [False, True])
def test_mutations_clear_previous_candidate_even_on_uncertain_failure(
    pb, monkeypatch, operation, fails
):
    draft = pb.claim(**claim_args(), self_source="affected package")
    intent = draft.prepare()
    intent._candidate_status = api.CandidateStatusRecord(
        state="ready_to_activate", proposal_id="old", current_accepted_coordinate=_COORDINATE
    )
    methods = {
        "prepare": "preflight_authoring_intent",
        "reprepare": "compile_authoring",
        "submit": "submit_authoring_intent",
    }

    def response(*args, **kwargs):
        if fails:
            raise TimeoutError("Remote outcome unknown")
        if operation in ("prepare", "reprepare"):
            return intent._preflight
        return SimpleNamespace(intent=intent._raw, status=None, outcome=None)

    monkeypatch.setattr(pb._client, methods[operation], response, raising=False)
    options = {"draft": draft} if operation == "reprepare" else {}
    if fails:
        with pytest.raises(TimeoutError):
            getattr(intent, operation)(**options)
    else:
        getattr(intent, operation)(**options)
    assert intent.proposal is None


@pytest.mark.parametrize("state", ["draft", "preflight_refused", "ready_to_submit", "accepted"])
def test_resume_restores_server_revision_without_repeating_work(pb, monkeypatch, state):
    preflight = None
    if state != "draft":
        preflight = stub_preflight_result(
            verdict="refused" if state == "preflight_refused" else "passed",
            diagnostics=(
                (stub_diagnostic("example", "Recorded diagnostic"),)
                if state == "preflight_refused"
                else ()
            ),
        ).model_dump(mode="json")
    status = api.CandidateStatusRecord(
        state=state,
        proposal_id="saved-proposal" if state == "accepted" else None,
        accepted_generation=_COORDINATE if state == "accepted" else None,
        current_accepted_coordinate=_COORDINATE,
    )
    raw = dict(
        intent_id="saved-intent",
        intent_revision=3,
        last_preflight=preflight,
        candidate_status=status.model_dump(mode="json"),
    )
    calls = []

    def resume(instance_id, intent_id):
        calls.append((instance_id, intent_id))
        return api.AuthoringIntentViewRecord(intent=raw)

    def forbidden(*args, **kwargs):
        pytest.fail("Reopening must not compile, prepare, submit, or query status again")

    monkeypatch.setattr(pb._client, "get_authoring_intent", resume, raising=False)
    for name in (
        "compile_authoring",
        "preflight_authoring_intent",
        "submit_authoring_intent",
        "authoring_intent_status",
    ):
        monkeypatch.setattr(pb._client, name, forbidden, raising=False)
    intent = pb.resume_intent("saved-intent")
    assert intent.intent_id == "saved-intent" and intent.revision == 3
    assert intent.refused == (state == "preflight_refused")
    assert intent._candidate_status == status
    assert (intent.proposal is not None) == (state == "accepted")
    if intent.proposal is not None:
        assert intent.proposal.proposal_id == "saved-proposal"
    assert all(d.call_site is None for d in intent.diagnostics)
    assert calls == [("inst_test", "saved-intent")]


def test_a_line_and_the_triggers_aimed_at_it_are_separate_authoring_decisions(pb):
    import pytest
    from pydantic import ValidationError

    from cruxible_client.contracts.authoring.models import (
        CaptureLandingByNameSchedule,
        LineAuthoringPayload,
        TriggerAuthoringPayload,
    )
    from cruxible_client.contracts.triggers import CadenceSchedule

    landing = CaptureLandingByNameSchedule(capture_contract="feed")
    draft = pb.changes(rationale="Consume each observed feed")
    draft.line(
        name="consume-feed",
        procedure="consume",
        max_authority="observe",
        trigger_input="feed",
    )
    draft.trigger(name="on-feed", schedule=landing, line="consume-feed")
    draft.trigger(
        name="sweep-often",
        schedule=CadenceSchedule(interval_seconds=600),
        action="evidence.sweep",
    )
    members = draft._compiled().payload.members
    line = next(m for m in members if isinstance(m, LineAuthoringPayload))
    assert line.trigger_input == "feed" and "trigger_policy" not in line.model_dump()
    triggers = {m.name: m for m in members if isinstance(m, TriggerAuthoringPayload)}
    assert (triggers["on-feed"].line_name, triggers["on-feed"].schedule) == (
        "consume-feed",
        landing,
    )
    assert (triggers["sweep-often"].action, triggers["sweep-often"].line_name) == (
        "evidence.sweep",
        None,
    )
    with pytest.raises(ValidationError, match="exactly one target"):
        TriggerAuthoringPayload(
            name="both", schedule=landing, line_name="consume-feed", action="evidence.sweep"
        )


@pytest.mark.parametrize("batch", [False, True])
def test_a_claim_names_its_subject_and_value_by_kind_and_key(pb, batch):
    """No predicted id: the key reference rides to lowering as a subject-key-v1 address."""

    from cruxible_client.contracts.subject_kinds import SubjectKeyReference

    target = pb.changes(rationale="Assessed") if batch else pb
    draft = target.claim(
        **{
            **claim_args(),
            "subject": SubjectKeyReference(
                subject_kind="sec.assessment", key={"release": "R1", "cve": "CVE-2026-0001"}
            ),
            "value": SubjectKeyReference(subject_kind="sec.package", key={"slug": "Demo"}),
        },
        self_source="assessed",
    )
    compiled = draft._compiled() if batch else draft
    payload = compiled.payload.members[0] if batch else compiled.payload
    statement = payload.statement
    assert statement.subject.subject_key_reference == (
        "sec.assessment",
        {"cve": "CVE-2026-0001", "release": "R1"},
    )
    assert statement.object.address.subject_key_reference == ("sec.package", {"slug": "Demo"})
    # A key reference asserts no accepted address at the read coordinate.
    assert not [
        item
        for item in compiled.reference_expectations
        if item.payload_path in {"statement.subject", "statement.object.address"}
    ]


@pytest.mark.parametrize("batch", [False, True])
def test_sdk_claim_accepts_bare_subject_and_object_keys(pb, batch):
    from cruxible_client.contracts.subject_kinds import SubjectKeyReference

    target = pb.changes(rationale="Observed") if batch else pb
    draft = target.claim(
        subject=SubjectKeyReference(subject_kind="sec.vuln", key="CVE-2026-0001"),
        predicate="sec.vuln.affects_package",
        value=SubjectKeyReference(subject_kind="sec.package", key="Demo"),
        role="observation",
        rationale="Observed package.",
        self_source="package affected",
    )
    compiled = draft._compiled() if batch else draft
    payload = compiled.payload.members[0] if batch else compiled.payload
    assert payload.statement.subject.subject_key_reference == ("sec.vuln", "CVE-2026-0001")
    assert payload.statement.object.address.subject_key_reference == ("sec.package", "Demo")


@pytest.mark.parametrize("contract_first", [False, True])
def test_named_trigger_sdk_uses_the_shared_candidate_resolver(
    pb, tmp_path, monkeypatch, contract_first
):
    from cruxible_client.contracts.authoring.models import CaptureLandingByNameSchedule
    from cruxible_client.contracts.captures import (
        foreign_source_capture_contract,
    )
    from cruxible_core.authoring import lowering
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.core_support._identity_support import OWNER
    from tests.test_procedures.test_trigger_artifacts import _line_world

    contract = foreign_source_capture_contract("security.advisory")
    (tmp_path / "instance").mkdir()
    instance, _owner, line, _mandate = _line_world(tmp_path / "instance")
    draft = pb.changes(rationale="Consume contract by name")
    if contract_first:
        draft.capture_contract(contract)
    draft.trigger(
        name="on-feed",
        line=line.identity.name,
        schedule=CaptureLandingByNameSchedule(capture_contract=contract.identity.name),
    )
    if not contract_first:
        draft.capture_contract(contract)
    calls = []
    resolve = lowering._resolve_trigger_authoring_schedule

    def tracked(schedule, **kwargs):
        calls.append(schedule)
        return resolve(schedule, **kwargs)

    monkeypatch.setattr(lowering, "_resolve_trigger_authoring_schedule", tracked)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=OWNER,
        payload=draft._compiled().payload,
        canonical_timestamp="2026-10-17T12:00:00.000000Z",
    ).intent
    computed, _ = coordinator._compute_and_bind_preflight(created.intent_id, actor=OWNER)
    assert computed.result.verdict == "passed", computed.result.frontier
    assert calls and all(isinstance(schedule, CaptureLandingByNameSchedule) for schedule in calls)
    assert computed.lowered is not None
    assert "triggers/on-feed.json" in computed.lowered.proposed_tree
    from cruxible_client.authoring.sdk import Intent

    served = api.AuthoringPreflightResult.model_validate(computed.result.model_dump(mode="json"))
    handle = Intent(pb, None, {"intent_id": created.intent_id}, preflight=served)
    assert handle.resolved_trigger_contracts == served.resolved_trigger_contracts
    assert handle.resolved_trigger_contracts[0].event.capture_contract_identity == contract.identity


@pytest.mark.parametrize("window_close", [False, True])
def test_sdk_trigger_refuses_exact_event_authoring(pb, window_close):
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.windows import CaptureEventWindow
    from cruxible_client.contracts.triggers import CaptureLandingSchedule, WindowCloseSchedule
    from tests.test_authoring.test_capture_landing_name import SELECTOR

    schedule = (
        WindowCloseSchedule(window=CaptureEventWindow(event=SELECTOR, duration_seconds=60))
        if window_close
        else CaptureLandingSchedule(event=SELECTOR)
    )
    with pytest.raises(ValidationError):
        pb.changes(rationale="Invalid exact event").trigger(
            name="on-feed",
            line="triage",
            schedule=schedule,
        )


@pytest.mark.parametrize("entrypoint", ["draft", "changes", "intent"])
@pytest.mark.parametrize("accept", ["if_allowed", "never"])
@pytest.mark.parametrize("landed", [False, True])
def test_sdk_submit_forwards_accept_and_exposes_typed_outcome(
    pb, monkeypatch, entrypoint, accept, landed
):
    draft = pb.claim(**claim_args(), self_source="affected package")
    target = draft
    if entrypoint == "changes":
        target = pb.changes(rationale="One change").claim(
            **claim_args(), self_source="affected package"
        )
    elif entrypoint == "intent":
        target = draft.prepare()
    landed_coordinate = _COORDINATE.model_copy(update={"git_oid": "b" * 40})
    outcome = (
        api.AuthoringLanded(proposal_id="proposal", accepted_coordinate=landed_coordinate)
        if landed
        else api.AuthoringProposed(proposal_id="proposal", reason="accept_never", message="held")
    )
    seen = []

    def submit(*args, **kwargs):
        seen.append(kwargs["accept"])
        return api.AuthoringSubmitResultRecord(
            intent={"intent_id": "saved-intent"},
            status=api.CandidateStatusRecord(
                state="accepted" if landed else "ready_to_activate",
                proposal_id="proposal",
                current_accepted_coordinate=landed_coordinate if landed else _COORDINATE,
                accepted_generation=landed_coordinate if landed else None,
            ),
            outcome=outcome,
        )

    method = "submit_authoring_intent" if entrypoint == "intent" else "submit_authoring"
    monkeypatch.setattr(pb._client, method, submit, raising=False)
    result = target.submit() if accept == "if_allowed" else target.submit(accept=accept)
    assert seen == [accept]
    assert result.outcome == outcome
    if landed and entrypoint != "changes":
        assert pb._coordinate == landed_coordinate
    elif entrypoint == "changes":
        # A changeset retains its pinned read context; its outcome carries the
        # new coordinate without changing that context or its parent.
        assert pb._coordinate == _COORDINATE
