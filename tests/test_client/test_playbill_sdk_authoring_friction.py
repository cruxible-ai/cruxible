"""Concise authoring preserves explicit evidence and lowering semantics."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client import ClaimRole, Playbill
from cruxible_client import contracts as api
from tests.test_client.test_playbill_sdk import _COORDINATE, _Client, _workspace


@pytest.fixture
def pb(tmp_path: Path):
    _workspace(tmp_path)
    return Playbill._from_client(_Client(), instance_id="inst_test", workspace=tmp_path)


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
    status = api.PlaybillCandidateStatus(
        state="ready_to_activate",
        proposal_id="proposal-one",
        current_accepted_coordinate=_COORDINATE,
    )
    calls = []

    def submit(*args):
        calls.append("submit")
        return api.PlaybillAuthoringSubmitResult(intent=intent._raw, status=status)

    def refresh(*args):
        calls.append("status")
        return status.model_copy(update={"proposal_id": "proposal-two"})

    monkeypatch.setattr(pb._client, "submit_playbill_authoring_intent", submit, raising=False)
    monkeypatch.setattr(pb._client, "playbill_authoring_intent_status", refresh, raising=False)
    intent.submit()
    original = intent.proposal
    assert original.proposal_id == "proposal-one"
    assert intent.proposal.proposal_id == "proposal-one"
    assert calls == ["submit"]
    intent.status()
    assert intent.proposal.proposal_id == "proposal-two"
    assert original.proposal_id == "proposal-one"
    assert calls == ["submit", "status"]


@pytest.mark.parametrize("operation", ["prepare", "reprepare", "rebase", "submit"])
@pytest.mark.parametrize("fails", [False, True])
def test_mutations_clear_previous_candidate_even_on_uncertain_failure(
    pb, monkeypatch, operation, fails
):
    draft = pb.claim(**claim_args(), self_source="affected package")
    intent = draft.prepare()
    intent._candidate_status = api.PlaybillCandidateStatus(
        state="ready_to_activate", proposal_id="old", current_accepted_coordinate=_COORDINATE
    )
    methods = {
        "prepare": "preflight_playbill_authoring_intent",
        "reprepare": "compile_playbill_authoring",
        "rebase": "rebase_playbill_authoring_intent",
        "submit": "submit_playbill_authoring_intent",
    }

    def response(*args, **kwargs):
        if fails:
            raise TimeoutError("Remote outcome unknown")
        if operation in ("prepare", "reprepare"):
            return intent._preflight
        return SimpleNamespace(intent=intent._raw, status=None)

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
        preflight = api.PlaybillAuthoringPreflightResult(
            verdict="refused" if state == "preflight_refused" else "passed",
            certificate={"intent_id": "saved-intent"},
            frontier={"diagnostics": [{"code": "example", "message": "Recorded diagnostic"}]},
        ).model_dump(mode="json")
    status = api.PlaybillCandidateStatus(
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
        return api.PlaybillAuthoringIntentView(intent=raw)

    def forbidden(*args, **kwargs):
        pytest.fail("Reopening must not compile, prepare, submit, or query status again")

    monkeypatch.setattr(pb._client, "resume_playbill_authoring_intent", resume, raising=False)
    for name in (
        "compile_playbill_authoring",
        "preflight_playbill_authoring_intent",
        "submit_playbill_authoring_intent",
        "playbill_authoring_intent_status",
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


def test_line_trigger_input_is_an_explicit_authoring_decision(pb):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.authoring.models import LineAuthoringPayloadV1
    from cruxible_client.contracts.procedures.line_specs import CaptureLandingTriggerPolicyV2
    from cruxible_client.contracts.procedures.windows import CaptureEventSelectorV1

    trigger = CaptureLandingTriggerPolicyV2(
        event=CaptureEventSelectorV1(
            capture_contract_identity=ArtifactIdentity(kind="CaptureContract", name="feed"),
            capture_contract_digest="sha256:" + "a" * 64,
        )
    )
    draft = pb.changes(rationale="Consume each observed feed")
    draft.line(
        name="consume-feed",
        procedure="consume",
        acquisition_policy="feed-policy",
        max_authority="observe",
        trigger_policy=trigger,
        trigger_input="feed",
    )
    line = next(
        m for m in draft._compiled().payload.members if isinstance(m, LineAuthoringPayloadV1)
    )
    assert line.trigger_input == "feed" and line.trigger_policy == trigger
