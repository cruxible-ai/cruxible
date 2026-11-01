"""Ergonomic Procedure draft lowering onto the accepted Procedure artifact."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.authoring.examples import procedure_example, query_claims_by_type_example
from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.authoring.inputs import lower_authoring_input
from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayload,
    ChangeSetAuthoringPayload,
    ProcedureAuthoringPayload,
    ProcedureRuntimePolicyAuthoringPayload,
    QueryDefinitionAuthoringPayload,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_path,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureHaltTerminal,
    ProcedureRunReceipt,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinition,
    validate_query_definition,
)
from cruxible_client.contracts.query.grammar import QueryProjection
from cruxible_core.authoring import lowering
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import compute_preflight
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from tests.core_support._support import initialize_local
from tests.support.procedures import MAX_WALL_CLOCK, ref, spec_payload
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_ledger.test_activation import _sign

TIMESTAMP = "2026-08-21T12:00:00.000000Z"


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-authoring-test-v1", {"label": label}).tagged


#: The triage result's one field.
ROWS = {"rows": "json"}

#: The run limits every triage spec declares (one minute, no provider calls).
LIMITS = {
    "hard_caps": {
        "wall_clock": f"PT{MAX_WALL_CLOCK.microseconds // 1_000_000}S",
        "provider_calls": 0,
        "capture_bytes": 0,
        "items": 200,
        "repeat_attempts": 1,
    },
}


def _triage_definition(*, query: str | None = None) -> dict[str, object]:
    """The triage Procedure as authored: one typed spec; given a query, a read of it.

    Without a query the Procedure projects a constant, so it lowers in an empty
    instance.
    """

    steps: list[dict[str, object]] = []
    if query is not None:
        steps.append({"read": "rows", "id": "read", "query": query})
    steps.append(
        {
            "project": "result",
            "id": "shape",
            "fields": {"rows": ref("$steps.rows") if query is not None else []},
            "output": ROWS,
            "contract": "rows-output",
        }
    )
    return {
        "procedure": "triage",
        "description": "Read accepted claims and shape a bounded result.",
        "input": {},
        "output": {"rows": "json"},
        "input_contract": "empty-input",
        "output_contract": "rows-output",
        "activation": "drain",
        "steps": steps,
        "returns": "result",
        **LIMITS,
    }


def _layout_definition(*, halt_before_return: bool) -> dict[str, object]:
    base = _triage_definition()
    read = {
        "project": "rows",
        "id": "read",
        "fields": {"rows": []},
        "output": ROWS,
        "contract": "rows-output",
    }
    gate = {
        "guard": {"op": "eq", "left": {"literal": True}, "right": {"literal": True}},
        "id": "gate",
        "then": {"goto": "result"},
        "else": {"goto": "stop"},
    }
    result = {
        "project": "result",
        "fields": {"rows": ref("$steps.rows")},
        "output": ROWS,
        "contract": "rows-output",
    }
    stop = {"halt": "No rows are available.", "id": "stop"}
    tail = [stop, result] if halt_before_return else [result, stop]
    return {**base, "steps": [read, gate, *tail]}


def _coordinator(tmp_path: Path) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    instance, _owner = initialize_local(tmp_path)
    store = AuthoringIntentStore(
        instance.root / instance.descriptor.storage.exhaust,
        token_factory=lambda: "3" * 32,
    )
    return AuthoringIntentCoordinator(instance=instance, store=store), AuthenticatedActor(
        actor_id="owner"
    )


def _payload(definition: dict[str, object]) -> ProcedureAuthoringPayload:
    payload = spec_payload(definition)
    assert isinstance(payload, ProcedureAuthoringPayload)
    return payload


def _revise(payload: ProcedureAuthoringPayload, **changes: object) -> ProcedureAuthoringPayload:
    """The same authored Procedure with spec fields changed."""

    spec = dict(payload.definition["procedure_spec"])  # type: ignore[arg-type]
    return _payload({**spec, **changes})


def _change_set_query(*, named: bool = True) -> QueryDefinition:
    example = query_claims_by_type_example().query_definition
    assert example.projection is not None

    return example.model_copy(
        update={
            "projection": QueryProjection(fields=(example.projection.fields[0],)),
        }
    )


def _change_set_payload(query: QueryDefinition) -> ChangeSetAuthoringPayload:
    """The triage Procedure reading a Query staged in the same change set."""

    return ChangeSetAuthoringPayload(
        members=(
            _payload(_triage_definition(query=query.identity.name)),
            QueryDefinitionAuthoringPayload(query_definition=query),
        )
    )


def _submit_approve_activate(
    coordinator: AuthoringIntentCoordinator,
    *,
    actor: AuthenticatedActor,
    owner: GeneratedKeyMaterial,
    payload: ChangeSetAuthoringPayload,
    timestamp: str,
) -> None:
    created = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=timestamp,
    ).intent
    preflight = compute_preflight(coordinator.instance, intent=created, actor=actor)
    assert preflight.result.verdict == "passed", preflight.result.frontier
    submitted = coordinator.submit(created.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        coordinator.instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        coordinator.instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        coordinator.instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def _world_holding_a_document(
    tmp_path: Path,
    *,
    historical: bool = False,
) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    """One instance whose accepted tree governs a single Document."""

    instance, owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Accepted design\n")
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Accepted design",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            "documents/design.json": render_document(shell),
        },
        timestamp=TIMESTAMP,
        proposal_name="seed-governed-document",
    )
    instance.refresh()
    store = AuthoringIntentStore(
        instance.root / instance.descriptor.storage.exhaust,
        token_factory=lambda: "4" * 32,
    )
    return (
        AuthoringIntentCoordinator(instance=instance, store=store),
        AuthenticatedActor(actor_id="owner"),
    )


def _list_contract_payload() -> ProcedureAuthoringPayload:
    listed = {"rows": {"type": "list", "item_fields": {"id": {"type": "string"}}}}
    return _payload(
        {
            **_triage_definition(),
            "procedure": "bounded-projection",
            "output": listed,
            "activation": "snapshot",
            "steps": [
                {
                    "project": "result",
                    "fields": {"rows": []},
                    "output": listed,
                    "contract": "rows-output",
                }
            ],
        }
    )


def test_a_procedure_can_be_authored_in_a_world_that_governs_a_document(
    tmp_path: Path,
) -> None:
    """Unrelated governed Documents must not prevent Procedure authoring."""

    coordinator, actor = _world_holding_a_document(tmp_path)

    compiled = coordinator.compile(
        actor=actor,
        payload=_list_contract_payload(),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )

    assert compiled.verdict == "passed", [item.code for item in compiled.frontier.diagnostics]
    assert compiled.frontier.diagnostics == ()


def test_served_procedure_example_reaches_the_closed_transforms(tmp_path: Path) -> None:
    example = procedure_example()
    kinds = {step.kind for step in example.spec.steps if hasattr(step, "transform")}
    assert kinds == {"aggregate", "dedupe", "filter", "join", "shape"}

    coordinator, actor = _coordinator(tmp_path)
    result = coordinator.compile(
        actor=actor,
        payload=lower_authoring_input(example),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "passed", result.frontier


def test_approval_policy_is_a_real_singleton_authoring_scope(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    policy_payload = ApprovalPolicyAuthoringPayload(
        approval_policy=ApprovalPolicy(mode="independent_approval_required")
    )
    singleton = coordinator.compile(
        actor=actor,
        payload=policy_payload,
        canonical_timestamp=TIMESTAMP,
    )
    assert singleton.verdict == "passed", singleton.frontier

    mixed_coordinator = AuthoringIntentCoordinator(
        instance=coordinator.instance,
        store=AuthoringIntentStore(
            coordinator.instance.root / coordinator.instance.descriptor.storage.exhaust,
            token_factory=lambda: "4" * 32,
        ),
    )
    mixed = mixed_coordinator.compile(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(
                policy_payload,
                QueryDefinitionAuthoringPayload(query_definition=_change_set_query()),
            )
        ),
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )
    assert mixed.verdict == "refused"
    diagnostic = mixed.frontier.diagnostics[0]
    assert diagnostic.code == "cruxible.authoring.approval_policy_singleton_required"
    assert diagnostic.offending_element == "members"
    assert diagnostic.repairs[0].kind == "split_change_set"


def test_procedure_runtime_policy_is_a_real_singleton_authoring_scope(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    policy_payload = ProcedureRuntimePolicyAuthoringPayload(
        procedure_runtime_policy=ProcedureRuntimePolicy(provider_output_bytes_cap=2_097_152)
    )
    singleton = coordinator.compile(
        actor=actor,
        payload=policy_payload,
        canonical_timestamp=TIMESTAMP,
    )
    assert singleton.verdict == "passed", singleton.frontier

    mixed_coordinator = AuthoringIntentCoordinator(
        instance=coordinator.instance,
        store=AuthoringIntentStore(
            coordinator.instance.root / coordinator.instance.descriptor.storage.exhaust,
            token_factory=lambda: "5" * 32,
        ),
    )
    mixed = mixed_coordinator.compile(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(
                policy_payload,
                QueryDefinitionAuthoringPayload(query_definition=_change_set_query()),
            )
        ),
        canonical_timestamp="2026-08-21T12:02:00.000000Z",
    )
    assert mixed.verdict == "refused"
    assert mixed.frontier.diagnostics[0].code == (
        "cruxible.authoring.procedure_runtime_policy_singleton_required"
    )


def test_change_set_membership_and_candidate_reference_refusals(tmp_path: Path) -> None:
    query = _change_set_query()
    # One authoring intent is one changeset at every size. The two-member floor
    # this line once pinned made the uniform builder refuse the smallest real
    # set an author writes, so a one-member set is admitted and keeps its own
    # ChangeSet identity rather than collapsing into a singleton payload.
    single = ChangeSetAuthoringPayload(
        members=(QueryDefinitionAuthoringPayload(query_definition=query),)
    )
    assert len(single.members) == 1
    with pytest.raises(ValueError, match="sorted by semantic identity"):
        ChangeSetAuthoringPayload(members=tuple(reversed(_change_set_payload(query).members)))

    coordinator, actor = _coordinator(tmp_path)
    created = coordinator.create(
        actor=actor,
        payload=_change_set_payload(query),
        canonical_timestamp=TIMESTAMP,
    )
    renamed_values = query.model_dump(mode="json")
    renamed_values["identity"] = {"kind": "QueryDefinition", "name": "renamed"}
    renamed = validate_query_definition(renamed_values)
    with pytest.raises(ValueError, match="cannot change member identity"):
        coordinator.replace_payload(
            created.intent.intent_id,
            actor=actor,
            payload=_change_set_payload(renamed),
        )

    # A Query the change set does not stage (and the base does not hold) is
    # unresolved, named by the step that reads it.
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    coordinator, actor = _coordinator(outside_root)
    refused = coordinator.compile(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(
                _payload(_triage_definition(query="outside")),
                QueryDefinitionAuthoringPayload(query_definition=query),
            )
        ),
        canonical_timestamp=TIMESTAMP,
    )
    assert refused.verdict == "refused"
    diagnostic = refused.frontier.diagnostics[0]
    assert diagnostic.code == "cruxible.authoring.artifact_reference_unresolved"
    assert diagnostic.offending_element == "members[0].steps[0].query"
    assert "QueryDefinition:outside" in diagnostic.message


def test_invalid_definition_message_excludes_pydantic_metadata(tmp_path: Path) -> None:
    coordinator, actor = _coordinator(tmp_path)
    spec = _triage_definition()
    spec["steps"] = [{"project": "result", "fields": {}, "contract": 7}]

    result = coordinator.compile(
        actor=actor,
        payload=ProcedureAuthoringPayload(
            definition={"name": "triage", "procedure_spec": spec},
            activation_policy="drain",
            owned_contracts=(),
        ),
        canonical_timestamp=TIMESTAMP,
    )
    assert result.verdict == "refused"
    message = result.frontier.diagnostics[0].message
    assert "procedure_spec.steps[0]" in message
    assert "contract" in message
    assert "input_value=" not in message
    assert "input_type=" not in message
    assert "errors.pydantic.dev" not in message


@pytest.mark.parametrize(
    ("invalid_graph", "expected_cause"),
    (
        (
            "nonreturn_leaf",
            "Procedure leaf 'unused' neither halts, emits typed egress, nor returns",
        ),
        (
            "terminal_guard",
            "Procedure guard 'gate' with omitted on_true must name a forward true target",
        ),
    ),
)
def test_graph_law_failures_use_typed_definition_refusal(
    tmp_path: Path,
    invalid_graph: str,
    expected_cause: str,
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    spec = _triage_definition()
    always = {"op": "eq", "left": {"literal": True}, "right": {"literal": True}}
    if invalid_graph == "nonreturn_leaf":
        spec["steps"] = [
            {
                "project": "rows",
                "id": "read",
                "fields": {"rows": []},
                "output": ROWS,
                "contract": "rows-output",
            },
            {"guard": always, "id": "gate", "then": {"goto": "result"}, "else": {"goto": "unused"}},
            {
                "project": "result",
                "fields": {"rows": ref("$steps.rows")},
                "output": ROWS,
                "contract": "rows-output",
            },
            {
                "project": "unused",
                "fields": {"rows": ref("$steps.rows")},
                "output": ROWS,
                "contract": "rows-output",
            },
        ]
    else:
        spec["steps"] = [
            {
                "project": "result",
                "fields": {"rows": []},
                "output": ROWS,
                "contract": "rows-output",
            },
            {"guard": always, "id": "gate"},
        ]

    result = coordinator.compile(
        actor=actor,
        payload=_payload(spec),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "refused"
    diagnostic = result.frontier.diagnostics[0]
    assert diagnostic.code == "cruxible.authoring.procedure_definition_invalid"
    assert diagnostic.stage == "lowering"
    assert diagnostic.offending_element == "definition"
    assert expected_cause in diagnostic.message
    assert diagnostic.repairs[0].kind == "replace_definition"
    assert diagnostic.repairs[0].description == "Repair the indicated definition field."
    assert "errors.pydantic.dev" not in diagnostic.message


def test_layout_only_procedure_successor_refuses_in_coordinator(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    definition = _layout_definition(halt_before_return=False)
    first = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )
    assert first.verdict == "passed"
    seed = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=seed, actor=actor).lowered
    assert lowered is not None
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp=TIMESTAMP,
        proposal_name="seed-layout-procedure",
    )
    reordered = _layout_definition(halt_before_return=True)

    result = coordinator.compile(
        actor=actor,
        payload=_payload(reordered),
        canonical_timestamp="2026-08-21T12:01:00.000000Z",
    )

    assert result.verdict == "refused"
    assert result.frontier.diagnostics[0].code == "cruxible.proposal.non_singleton_scope"
    assert result.frontier.diagnostics[0].message == (
        "The proposal changes no registered semantic member."
    )


def test_procedure_compiles_without_writer_managed_envelope_fields(
    tmp_path: Path,
) -> None:
    coordinator, actor = _coordinator(tmp_path)
    definition = _triage_definition()

    result = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )

    assert result.verdict == "passed"
    assert result.certificate.resolved_authoring_digest.startswith("sha256:")
    resumed = coordinator.list_pending(actor=actor).intents[0]
    assert resumed.semantic_identity == "Procedure:triage"
    assert resumed.candidate_status.state == "ready_to_submit"
    assert not any(
        key in resumed.payload.definition
        for key in {"definition_digest", "identity", "lifecycle", "path", "pins"}
    )


@pytest.mark.parametrize("graph_format", [6, 5, 7])
def test_an_authored_procedure_cannot_state_a_graph_format(
    tmp_path: Path, graph_format: object
) -> None:
    """The spec carries no graph coordinate: lowering states the one format (7)."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="graph_format"):
        _payload({**_triage_definition(), "graph_format": graph_format})
    coordinator, actor = _coordinator(tmp_path)
    unstated = coordinator.compile(
        actor=actor,
        payload=_payload(_triage_definition()),
        canonical_timestamp=TIMESTAMP,
    )
    assert unstated.verdict == "passed"
    pending = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(coordinator.instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    path = procedure_path("triage")
    assert parse_procedure(lowered.proposed_tree[path], path=path).definition.graph_format == 7


def test_a_procedure_revision_submits_without_reaching_the_claim_revision_marker(
    tmp_path: Path,
) -> None:
    """A Procedure amend must not raise on the terminal success path.

    The submit and the store transition land before the result is built, so a
    raise there means the write happened and the call reported failure. The
    Claim revision marker used to run for every payload kind and refuse a
    Procedure identity.
    """
    instance, owner = initialize_local(tmp_path)
    minted = iter(("4" * 32, "5" * 32))
    store = AuthoringIntentStore(
        instance.root / instance.descriptor.storage.exhaust,
        token_factory=lambda: next(minted),
    )
    coordinator = AuthoringIntentCoordinator(instance=instance, store=store)
    actor = AuthenticatedActor(actor_id="owner")

    # An accepted Procedure, so the next authoring of the same name lowers with
    # a predecessor_digest -- the exact state the marker mishandled.
    definition = _triage_definition()
    first = coordinator.compile(
        actor=actor,
        payload=_payload(definition),
        canonical_timestamp=TIMESTAMP,
    )
    assert first.verdict == "passed"
    pending = coordinator.list_pending(actor=actor).intents[0]
    lowered = compute_preflight(instance, intent=pending, actor=actor).lowered
    assert lowered is not None
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp=TIMESTAMP,
        proposal_name="seed-procedure",
    )

    revised = {**definition, "description": "A revised triage."}
    compiled = coordinator.compile(
        actor=actor,
        payload=_payload(revised),
        canonical_timestamp=TIMESTAMP,
    )
    assert compiled.verdict == "passed", compiled.frontier

    intent_id = coordinator.list_pending(actor=actor).intents[-1].intent_id
    submitted = coordinator.submit(intent_id, actor=actor)

    assert submitted.tag == "playbill-authoring-submit-result-v1"
    assert submitted.intent.semantic_identity == "Procedure:triage"
    # The marker is a Claim concept; a Procedure carries the default.
    assert submitted.identity_stable is False
    assert submitted.claim_revision is None


def test_coordinator_authored_halt_reason_reaches_terminal_and_receipt(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    reason = "No eligible work remains."
    payload = _payload(
        {
            "procedure": "halt-with-reason",
            "input": {},
            "output": {},
            "input_contract": "empty-input",
            "output_contract": "halt-output",
            "activation": "snapshot",
            **LIMITS,
            "steps": [
                {
                    "guard": {"op": "eq", "left": {"literal": False}, "right": {"literal": True}},
                    "id": "gate",
                    "else": {"halt": reason, "id": "stop"},
                },
                {"project": "result", "fields": {}, "contract": "halt-output"},
            ],
            "returns": "result",
        }
    )
    created = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    preflight = compute_preflight(instance, intent=created, actor=actor)
    assert preflight.result.verdict == "passed", preflight.result.frontier
    submitted = coordinator.submit(created.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )

    result = service_run_playbill_procedure(
        instance,
        name="halt-with-reason",
        request=ProcedureRunRequest(
            evaluation_time=datetime(2026, 8, 21, 12, 5, tzinfo=UTC),
            input={},
        ),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id=instance.descriptor.instance_id,
            operation_id="coordinator-halt-reason",
            timestamp=datetime(2026, 8, 21, 12, 5, tzinfo=UTC),
        ),
    )

    assert result.status == "halted"
    assert isinstance(result.terminal, ProcedureHaltTerminal)
    assert result.terminal.reason == reason
    assert isinstance(result.receipt, ProcedureRunReceipt)
    assert result.receipt.invocation_origin == "actor"
    assert result.receipt.terminal == result.terminal


def _accepted_authoring_trio(tmp_path):
    from cruxible_client.contracts.authoring.models import (
        LineAuthoringPayload,
        ProcedureMandateAuthoringPayload,
        authoring_member_identity,
    )

    instance, owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    procedure = _list_contract_payload()
    name = str(procedure.definition["name"])
    line = LineAuthoringPayload(
        name=name,
        procedure_name=name,
        max_authority="observe",
    )
    mandate = ProcedureMandateAuthoringPayload(
        name=name,
        line_name=name,
        grants="propose",
        namespace=("captures",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(
                (procedure, line, mandate),
                key=authoring_member_identity,
            )
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    lowered = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    _accept_tree(
        instance, owner, lowered.proposed_tree, timestamp=TIMESTAMP, proposal_name="authoring-trio"
    )
    return coordinator, actor, owner, payload, procedure, line, mandate


def test_a_propose_mandate_with_settle_fields_refuses_typed_by_field(tmp_path):
    """A propose grant naming a settle-only field refuses by name, never as a model dump."""

    coordinator, actor, _, _payload, _procedure, _line, mandate = _accepted_authoring_trio(tmp_path)
    instance = coordinator.instance
    revised = mandate.model_copy(update={"subject_creation": ("acme.item",)})
    intent = coordinator.create(
        actor=actor, payload=revised, canonical_timestamp="2026-08-21T12:03:00.000000Z"
    ).intent
    computed = compute_preflight(instance, intent=intent, actor=actor)

    assert computed.result.verdict != "passed"
    (diagnostic,) = [
        item
        for item in computed.result.frontier.diagnostics
        if item.code == "cruxible.authoring.procedure_mandate_propose_scope"
    ]
    assert diagnostic.offending_element == "subject_creation"
    assert "validation error" not in diagnostic.message
    assert "grants: settle" in diagnostic.repairs[0].description


def test_unchanged_procedure_line_mandate_submit_reuses_accepted_state(tmp_path):
    coordinator, actor, _, payload, procedure, line, mandate = _accepted_authoring_trio(tmp_path)
    instance = coordinator.instance
    coordinate = instance.accepted_coordinate()
    tree = instance.tree_at(coordinate.git_oid)
    # Exercise individual doors, the combined changeset, and submit retry.
    for member in (
        procedure,
        line,
        mandate,
        payload.model_copy(update={"rationale": "Reauthor accepted definitions"}),
    ):
        intent = coordinator.create(
            actor=actor, payload=member, canonical_timestamp="2026-08-21T12:02:00.000000Z"
        ).intent
        computed = compute_preflight(instance, intent=intent, actor=actor)
        assert computed.result.verdict == "passed", computed.result.frontier.model_dump_json(
            indent=2
        )
        assert computed.lowered.idempotent
        assert computed.lowered.changed_members == ()
        assert dict(computed.lowered.proposed_tree) == dict(tree)
        for _ in range(2):
            result = coordinator.submit(intent.intent_id, actor=actor)
            assert result.status.state == "accepted"
            assert result.status.proposal_id is None
            assert result.identity_stable
            assert result.claim_revision is None
            assert instance.accepted_coordinate() == coordinate


def test_changed_procedure_rebinds_dependents_then_reauthoring_is_unchanged(tmp_path):
    from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate_any
    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from cruxible_client.contracts.procedures.line_specs import parse_line_spec

    coordinator, actor, owner, payload, procedure, _, _ = _accepted_authoring_trio(tmp_path)
    instance = coordinator.instance
    before = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(str(procedure.definition["name"]))
    old_digest = procedure_artifact_digest(parse_procedure(before[path], path=path)).tagged
    changed = _revise(procedure, description="Real change")
    revised = payload.model_copy(
        update={"members": tuple(changed if m == procedure else m for m in payload.members)}
    )
    intent = coordinator.create(
        actor=actor, payload=revised, canonical_timestamp="2026-08-21T12:02:00.000000Z"
    ).intent
    computed = compute_preflight(instance, intent=intent, actor=actor)
    assert computed.result.verdict == "passed", computed.result.frontier
    lowered = computed.lowered
    assert not lowered.idempotent and len(lowered.changed_members) == 3
    new = parse_procedure(lowered.proposed_tree[path], path=path)
    assert new.lifecycle.predecessor_digest == old_digest
    new_digest = procedure_artifact_digest(new).tagged
    for p, b in lowered.changed_members:
        if p.startswith("lines/"):
            assert parse_line_spec(b, path=p).procedure.artifact_digest == new_digest
        if p.startswith("procedure-mandates/"):
            line_path = f"lines/{procedure.definition['name']}.json"
            revised_line = parse_line_spec(lowered.proposed_tree[line_path], path=line_path)
            from cruxible_client.contracts.procedures.line_specs import line_spec_digest

            assert (
                parse_procedure_mandate_any(b, path=p).line.artifact_digest
                == line_spec_digest(revised_line).tagged
            )
    _accept_tree(
        instance,
        owner,
        lowered.proposed_tree,
        timestamp="2026-08-21T12:01:00.000000Z",
        proposal_name="revised-trio",
    )
    again = coordinator.create(
        actor=actor,
        payload=revised.model_copy(update={"rationale": "Reauthor successor"}),
        canonical_timestamp="2026-08-21T12:03:00.000000Z",
    ).intent
    unchanged = lowering.lower_authoring(instance, intent=again, actor_id=actor.actor_id)
    assert unchanged.idempotent and unchanged.changed_members == ()
    assert unchanged.proposed_tree[path] == lowered.proposed_tree[path]
    # An old-base no-change intent must not claim the newer, different graph
    # already satisfies its requested content.
    stale = coordinator.create(
        actor=actor,
        payload=procedure,
        canonical_timestamp=TIMESTAMP,
        base_coordinate=intent.base_coordinate,
    ).intent
    stale_result = compute_preflight(instance, intent=stale, actor=actor)
    assert stale_result.result.verdict == "refused"


def test_authoring_no_change_comparison_keeps_lifecycle_and_policy_changes(tmp_path):
    from cruxible_client.contracts.authoring.models import LineAuthoringPayload

    coordinator, actor, _, _, procedure, line, mandate = _accepted_authoring_trio(tmp_path)
    instance = coordinator.instance
    # These are material changes even with unchanged Procedure graph content.
    variants = (
        _revise(procedure, activation="drain"),
        procedure.model_copy(update={"retire": True}),
        line.model_copy(update={"occurrence_epoch": 2}),
        line.model_copy(update={"retire": True}),
        mandate.model_copy(update={"expires_at": datetime(2027, 2, 1, tzinfo=UTC)}),
        mandate.model_copy(update={"retire": True}),
    )
    for member in variants:
        intent = coordinator.create(
            actor=actor, payload=member, canonical_timestamp="2026-08-21T12:02:00.000000Z"
        ).intent
        changed = lowering.lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
        assert not changed.idempotent
        # Any Line change renews its exact Mandate in the same change set.
        expected = 2 if isinstance(member, LineAuthoringPayload) else 1
        assert len(changed.changed_members) == expected


@pytest.mark.parametrize("in_change_set", [False, True])
def test_a_graph_definition_refuses_at_compile_and_preflight_naming_the_spec(
    tmp_path: Path, in_change_set: bool
) -> None:
    """A graph is the stored form, never an authored one (retired kinds included)."""

    coordinator, actor = _coordinator(tmp_path)
    payload: object = ProcedureAuthoringPayload(
        definition={"name": "triage", "nodes": [{"kind": "state_tap", "node_id": "retired"}]},
        activation_policy="drain",
        owned_contracts=(),
    )
    if in_change_set:
        payload = ChangeSetAuthoringPayload(members=(payload,))  # type: ignore[arg-type]
    compiled = coordinator.compile(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP)
    for result in (compiled, coordinator.preflight(compiled.certificate.intent_id, actor=actor)):
        assert result.verdict == "refused"
        diagnostic = result.frontier.diagnostics[0]
        assert diagnostic.code == "cruxible.authoring.procedure_authoring_form_retired"
        assert "ProcedureSpec" in diagnostic.message


@pytest.mark.parametrize(
    ("step", "replacement"),
    [
        ({"source": "doc", "contract": "c", "slot": "read", "request": {}}, "'call:'"),
        ({"tap": "rows", "query": "q"}, "'read:' naming its 'query'"),
        (
            {"claim": "value", "claim_type": "a.b", "subject": "x"},
            "'read:' naming its 'claim_type'",
        ),
        ({"explicit": {"kind": "state_tap", "node_id": "rows"}}, "the typed step for the node"),
    ],
    ids=["source", "tap", "claim", "explicit-state-tap"],
)
def test_a_retired_authored_step_is_refused_naming_its_replacement(
    step: dict[str, object], replacement: str
) -> None:
    from pydantic import ValidationError

    from cruxible_client.contracts.procedures.authored import ProcedureSpec

    with pytest.raises(ValidationError) as caught:
        ProcedureSpec.model_validate(
            {"procedure": "retired", "input": {}, "steps": [step, {"halt": "done"}]}
        )
    message = str(caught.value)
    assert "procedure_node_kind_retired" in message and replacement in message
