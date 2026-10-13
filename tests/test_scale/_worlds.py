"""The scale guard's two worlds: one shape, built at two sizes through the served write path.

Each world is a governed daemon instance under one shared scratch state root,
initialized as the local operator (auth off), seeded with the write vocabulary
(``tests/core_support/_write_support.py``), then grown by the write verbs in
batches: every generation creates keyed work items (create-by-key, a key Claim
plus ``title`` and ``status``), every other generation also revises ``status``
on older items, and every fifth cites file evidence, so the world holds
Captures. After the bulk, one governed proposal accepts the definitions the
routes need (a named query, a propose Procedure and a settle Procedure with
their Lines and mandates, an armed no-terminal Line with a generation Trigger,
and the curation-detect Trigger), the workspace is attached with floor
delivery, and the operator attests some Claims.

Nothing is copied: the registry holds absolute paths, so each world is built
fresh where it lives.
"""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_client.authoring.attestations import LocalEd25519ClaimAttestationSigner
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    build_coordinator_self_source_capture,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationCaptureReference,
    ClaimAttestationStatement,
)
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.procedure_mandates import (
    MandateClaimScope,
    MandateCondition,
    ProcedureMandate,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_identity_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    ProposeChangeSetNode,
    SettleChangeSetNode,
)
from cruxible_client.contracts.query.definitions import (
    CLAIM_TYPE_PIN_ROLE,
    QueryDefinition,
    QueryEvaluationPolicy,
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimValueRef,
    QueryEntry,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import parse_subject, subject_digest, subject_path
from cruxible_client.contracts.triggers import (
    GenerationAcceptedSchedule,
    parse_trigger,
    trigger_path,
)
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.governance.keys import GeneratedKeyMaterial, generate_client_principal_key
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.registry import get_registry
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._knowledge_loop_support import work_item_query
from tests.core_support._write_support import CLAIM_TYPES, KIND, report_evidence
from tests.core_support._write_support import seed_write_vocabulary as _seed_vocabulary
from tests.support.lines import line_trigger, successor, trigger_members

OPERATOR = "operator"
STATUSES = ("ready", "blocked", "done")
QUERY_NAME = "scale.work_items"
CONDITION_NAME = "scale.settle-condition"
PROPOSE = "scale-propose"
SETTLE = "scale-settle"
TICK = "scale-tick"
#: Items each terminal states: ``priority`` on the first and the next ten work items.
TERMINAL_ITEMS = 10
_FAR_PAST = datetime(2020, 1, 1, tzinfo=UTC)
_FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)
_CURATION_DETECT = (
    Path(__file__).resolve().parents[2]
    / "src/cruxible_core/governance/seed_artifacts/genesis/triggers-4/triggers/curation-detect.json"
)


@dataclass(frozen=True)
class Shape:
    """How a world grows: ``generations`` bulk writes of ``creates`` new work items.

    ``attestations`` Claims are attested once the bulk is in.
    """

    name: str
    generations: int
    creates: int = 7
    revises: int = 4

    @property
    def attestations(self) -> int:
        return max(1, self.generations // 5)


@dataclass
class World:
    shape: Shape
    instance_id: str
    owner: GeneratedKeyMaterial
    workspace: Path
    items: list[str] = field(default_factory=list)
    claims: dict[str, str] = field(default_factory=dict)
    generation: int = 0
    #: When this world's emulated consumer runner began listening (its Triggers' clock).
    listening_since: datetime = field(default_factory=lambda: datetime.now(UTC))
    listening_generation: int | None = None
    terminal_proposals: dict[str, str | None] = field(default_factory=dict)
    held_coordinate: str | None = None
    prepared: Any = None

    @property
    def instance(self) -> PlaybillInstance:
        return get_playbill_manager().get(self.instance_id)

    @property
    def root(self) -> Path:
        return self.instance.root

    def subject(self, index: int) -> str:
        return self.items[index]


def settle_advertisement(instance: PlaybillInstance) -> None:
    """Run the workspace advertiser's pending refresh once, on this thread.

    The scale module stops the advertiser from starting its thread, so the
    refresh a write requested runs exactly once, here, where a measured window
    can count it.
    """

    with instance._advertisement_condition:  # noqa: SLF001
        target = instance._advertisement_requested  # noqa: SLF001
        pending = instance._advertisement_completed < target  # noqa: SLF001
    if not pending:
        return
    result = instance._advertise_workspace_now()  # noqa: SLF001
    with instance._advertisement_condition:  # noqa: SLF001
        instance._advertisement_completed = target  # noqa: SLF001
        instance._last_advertisement = result  # noqa: SLF001


def write(world: World, changes: list[dict[str, Any]], **options: Any) -> WriteOutcome:
    request = WriteRequest.model_validate(
        {"because": f"scale {world.shape.name} {world.generation}", "changes": changes, **options}
    )
    outcome = playbill_api.playbill_write(world.instance_id, request=request)
    assert outcome.refusal is None, outcome.refusal
    if outcome.status == "accepted":
        world.generation += 1
        for change in outcome.changes:
            if change.claim is not None and change.subject is not None and change.field:
                world.claims[f"{change.subject}#{change.field}"] = change.claim
    return outcome


# -- bulk ---------------------------------------------------------------------------


def _bulk(world: World) -> None:
    shape = world.shape
    for generation in range(shape.generations):
        older = list(world.items)
        changes: list[dict[str, Any]] = []
        for index in range(shape.creates):
            item = f"{KIND}/s{generation:03d}-{index:02d}"
            world.items.append(item)
            changes.append({"op": "set", "subject": item, "field": "title", "value": item})
            changes.append(
                {
                    "op": "set",
                    "subject": item,
                    "field": "status",
                    "value": STATUSES[(generation + index) % 3],
                }
            )
        if generation % 2 == 1:
            picked = sorted(
                {older[(generation * 7 + n * 13) % len(older)] for n in range(shape.revises)}
            )
            changes.extend(
                {
                    "op": "set",
                    "subject": item,
                    "field": "status",
                    "value": STATUSES[(generation + n + 1) % 3],
                }
                for n, item in enumerate(picked)
            )
        if generation % 5 == 2:
            changes.append(
                {
                    "op": "set",
                    "subject": world.items[-1],
                    "field": "measured",
                    "value": generation,
                    # Zero-padded and closed, so no anchor is a prefix of another.
                    "evidence": report_evidence(
                        world.workspace, f"Count {world.shape.name} {generation:03d}."
                    ),
                }
            )
        outcome = write(world, changes)
        assert outcome.status == "accepted", outcome
        settle_advertisement(world.instance)


# -- definitions ------------------------------------------------------------------------


def _contract(name: str) -> tuple[ProcedureOwnedContract, ArtifactPin, str]:
    contract = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=ContractSchema(fields={"status": PropertySchema(type="string")}),
    )
    return (
        contract,
        ArtifactPin(
            role="contract-in" if name.endswith("-input") else "contract-out",
            target=contract.identity,
            artifact_digest=procedure_owned_contract_digest(contract).tagged,
        ),
        name,
    )


def _procedure(
    name: str, *, terminal: ProposeChangeSetNode | SettleChangeSetNode | None, capability: int
) -> AcceptedProcedure:
    """A Procedure with no Source and no Provider: one Project node, then its terminal."""

    input_contract, contract_in, _ = _contract(f"{name}-input")
    output_contract, contract_out, _ = _contract(f"{name}-output")
    definition = ProcedureDefinition(
        name=name,
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            ProjectNode(
                node_id="shape",
                fields={"status": "$input.status"},
                contract_out=contract_out,
                as_="result",
                **({} if terminal is None else {"next": terminal.node_id}),
            ),
            *(() if terminal is None else (terminal,)),
        ),
        returns="result",
        budget=ProcedureBudget(
            wall_clock=CanonicalDuration(microseconds=600_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=CanonicalDuration(microseconds=600_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=capability,
    )
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        pins=tuple(
            sorted(
                (contract_in, contract_out),
                key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
            )
        ),
        owned_contracts=tuple(
            sorted(
                (input_contract, output_contract),
                key=lambda contract: canonical_bytes(
                    contract.model_dump(mode="json", by_alias=True)
                ),
            )
        ),
        activation_policy="drain",
    )
    return AcceptedProcedure(
        path=procedure_path(name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _pin(accepted: AcceptedProcedure) -> ArtifactPin:
    return ArtifactPin(
        role="procedure",
        target=accepted.procedure.identity,
        artifact_digest=accepted.artifact_digest,
    )


def _line(name: str, accepted: AcceptedProcedure, *, authority: str) -> LineSpec:
    pin = _pin(accepted)
    return LineSpec(
        identity=ArtifactIdentity(kind="Line", name=name),
        occurrence_epoch=1,
        procedure=pin,
        parameters={"status": "open"},
        max_authority=authority,  # type: ignore[arg-type]
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 600_000_000,
        },
        epsilon={"$decimal": "0.1"},
        pins=(pin,),
    )


def _item(subject: str, value: str) -> dict[str, object]:
    kind, subject_id = subject.split("/", 1)
    return {
        "tag": "playbill-procedure-claim-proposal-item-v2",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": SemanticAddress.whole_artifact(subject_path(kind, subject_id)).model_dump(
                mode="json"
            ),
            "predicate": f"{KIND}.priority",
            "qualifier": None,
            "object": {"kind": "literal", "value": value},
            "role": "normative",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": f"The scale guard sets {subject} priority.",
        "revises": None,
        "source": {
            "tag": "playbill-self-source-body-v1",
            "content_base64": base64.b64encode(f"{subject} {value}\n".encode()).decode(),
        },
    }


def _claim_type_pin(field_name: str) -> ArtifactPin:
    (claim_type,) = (item for item in CLAIM_TYPES if item.predicate == f"{KIND}.{field_name}")
    return ArtifactPin(
        role=CLAIM_TYPE_PIN_ROLE,
        target=claim_type.identity,
        artifact_digest=claim_type_digest(claim_type).tagged,
    )


def _condition() -> QueryDefinition:
    """The settle condition: the target work item exists; its status rides along."""

    return QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name=CONDITION_NAME),
        entry=QueryEntry(
            binding="item",
            subject_kinds=(KIND,),
            subject_id=QueryParameterRef(parameter="item_id"),
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id", value=QuerySubjectFieldRef(binding="item", field="subject_id")
                ),
                QueryProjectionField(
                    name="status",
                    value=QueryClaimValueRef(binding="item", predicate=f"{KIND}.status"),
                ),
            )
        ),
        parameters=(QueryParameterDeclaration(name="item_id", value_type="string"),),
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        pins=(_claim_type_pin("status"),),
    )


def definitions(world: World) -> dict[str, bytes]:
    """Every definition the routes run, as tree members."""

    status_type = next(item for item in CLAIM_TYPES if item.predicate == f"{KIND}.status")
    query = work_item_query(QUERY_NAME, claim_type=status_type)
    condition = _condition()
    propose = _procedure(
        PROPOSE,
        terminal=ProposeChangeSetNode(
            node_id="propose",
            candidate_templates=tuple(
                _item(world.subject(index), "high") for index in range(TERMINAL_ITEMS)
            ),
            result="$steps.result",
        ),
        capability=2,
    )
    settle = _procedure(
        SETTLE,
        terminal=SettleChangeSetNode(
            node_id="settle",
            candidate_templates=tuple(
                _item(world.subject(TERMINAL_ITEMS + index), "low")
                for index in range(TERMINAL_ITEMS)
            ),
            result="$steps.result",
        ),
        capability=3,
    )
    tick = _procedure(TICK, terminal=None, capability=1)
    propose_line = _line(f"{PROPOSE}-line", propose, authority="propose")
    settle_line = _line(f"{SETTLE}-line", settle, authority="settle")
    tick_line = _line(f"{TICK}-line", tick, authority="observe")
    propose_mandate = ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name=f"{PROPOSE}-mandate"),
        procedure=_pin(propose),
        grants="propose",
        resource_ceiling=propose.procedure.definition.hard_caps,
        namespace=("claims",),
        valid_from=_FAR_PAST,
        expires_at=_FAR_FUTURE,
    )
    settle_mandate = ProcedureMandate(
        identity=ArtifactIdentity(kind="ProcedureMandate", name=f"{SETTLE}-mandate"),
        procedure=_pin(settle),
        grants="settle",
        resource_ceiling=settle.procedure.definition.hard_caps,
        namespace=("claims",),
        valid_from=_FAR_PAST,
        expires_at=_FAR_FUTURE,
        scope=(
            MandateClaimScope(claim_type=_claim_type_pin("priority"), change_kinds=("create",)),
        ),
        condition=MandateCondition(
            query=ArtifactPin(
                role="condition-query",
                target=condition.identity,
                artifact_digest=query_definition_digest(condition).tagged,
            ),
            binding_parameter="item_id",
            required_fields=("id",),
            fallback="propose",
        ),
    )
    members: dict[str, bytes] = {
        query_definition_path(query.identity.name): render_query_definition(query),
        query_definition_path(condition.identity.name): render_query_definition(condition),
        **{
            accepted.path: render_procedure(accepted.procedure)
            for accepted in (propose, settle, tick)
        },
        **{
            line_spec_path(line.identity.name): render_line_spec(line)
            for line in (propose_line, settle_line, tick_line)
        },
        **{
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)
            for mandate in (propose_mandate, settle_mandate)
        },
        **trigger_members(
            line_trigger(
                f"{TICK}-on-generation",
                line=tick_line.identity.name,
                schedule=GenerationAcceptedSchedule(),
            )
        ),
        "triggers/curation-detect.json": _CURATION_DETECT.read_bytes(),
    }
    return members


def line_digest(name: str) -> str:
    return line_identity_digest(ArtifactIdentity(kind="Line", name=f"{name}-line"))


def accept_members(world: World, members: dict[str, bytes], *, name: str) -> None:
    instance = world.instance
    inspection = submit_member_candidate(
        instance,
        members=members,
        actor_id=OPERATOR,
        proposal_name=name,
        proposal_family="definitions",
        timestamp=timestamp(),
    )
    assert inspection.proposal.candidate is not None, inspection.proposal.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=inspection.proposal.admission.proposal_id, activated_by=OPERATOR
    )
    assert receipt.status == "accepted", receipt
    world.generation += 1
    settle_advertisement(instance)


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -- attestations -------------------------------------------------------------------------


def attestation_request(world: World, subject: str, *, basis: str) -> ClaimAttestationAppendRequest:
    """The operator's signed attestation of ``subject``'s status Claim, built client-side.

    ``new_capture`` first stores a fresh coordinator self-source Capture, as a
    client stores a body before it cites it.
    """

    instance = world.instance
    claim_id = world.claims[f"{subject}#status"]
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    tree = instance.tree_at(coordinate.git_oid)
    path = claim_path(claim_id)
    claim = parse_claim(tree[path], path=path)
    subject_artifact = claim.statement.subject.artifact_path
    now = datetime.now(UTC)
    if basis == "new_capture":
        capture = build_coordinator_self_source_capture(
            store=instance.body_store(),
            actor_id=OPERATOR,
            claim_id=claim_id,
            body=f"scale observation {subject} {now.isoformat()}\n".encode(),
            observed_at=now,
            accepted_coordinate=coordinate,
        )
        cited: tuple[str, ...] = (capture.capture_digest,)
    else:
        cited = tuple(
            sorted(
                {
                    citation.capture_digest
                    for citation in claim.backing.citations
                    if citation.role == "evidence"
                },
                key=lambda item: item.encode("ascii"),
            )
        )
    statement = ClaimAttestationStatement(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        claim_identity=claim.identity,
        claim_artifact_digest=claim_artifact_digest(claim).tagged,
        claim_statement_digest=claim_statement_digest(claim.statement).tagged,
        subject_shell_digest=subject_digest(
            parse_subject(tree[subject_artifact], path=subject_artifact)
        ).tagged,
        object_shell_digest=None,
        attesting_principal_id=OPERATOR,
        signing_key_digest=world.owner.principal.public_key_digest,
        attestation_basis=basis,  # type: ignore[arg-type]
        stance="support",
        cited_capture_digests=cited,
        attested_at=now,
        valid_until=None,
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer=OPERATOR,
        signing_key_id=world.owner.principal.public_key_digest,
        private_key_path=world.owner.private_key_path,
        expected_public_key=world.owner.principal.public_key,
        forbidden_roots=(world.workspace, instance.root),
    )
    return ClaimAttestationAppendRequest(
        attestation=signer.sign_claim_attestation_v2(statement),
        capture_references=(
            tuple(ClaimAttestationCaptureReference(capture_digest=digest) for digest in cited)
            if basis == "new_capture"
            else ()
        ),
    )


def append_attestation(world: World, request: ClaimAttestationAppendRequest) -> object:
    return playbill_api.playbill_append_claim_attestation(world.instance_id, request=request)


def _retired_cadences(world: World) -> dict[str, bytes]:
    """Successors retiring the daily evidence sweep and the hourly anchor retry.

    Both are deliberate whole-population cadence work, kept out of the guard;
    retiring them keeps any tick here from firing them as a wall-clock hour or
    day passes.
    """

    tree = world.instance.tree_at(world.instance.accepted_coordinate().git_oid)
    members = {}
    for name in ("evidence-sweep", "prediction-anchor-retry"):
        path = trigger_path(name)
        members.update(
            trigger_members(successor(parse_trigger(tree[path], path=path), state="retired"))
        )
    return members


# -- consumer ticks ----------------------------------------------------------------------

#: The daemon's consumer runner, one part at a time: its Trigger evaluation, then
#: each kind (``next`` split into its parts), in the order a tick reaches them.
TICK_PARTS = (
    "triggers",
    "line",
    "floor",
    "queue",
    "evidence",
    "prediction",
    "curation",
    "settled_refusals",
)


def tick(world: World, part: str) -> object:
    """One tick of one consumer part: match, then run each unit it reports due.

    What the runner's loop and pools do (``consumers/runner.py``), on this
    thread and once, so a window counts exactly one tick.
    """

    from cruxible_core.consumers.floor import FLOOR
    from cruxible_core.consumers.lines import LINE_ARMS
    from cruxible_core.consumers.next import (
        curation,
        evidence,
        predictions,
        queue,
        settled_refusals,
    )
    from cruxible_core.triggers.journal import evaluate_triggers

    instance = world.instance
    manager = get_playbill_manager()
    now = datetime.now(UTC)
    if part == "triggers":
        return evaluate_triggers(
            instance,
            now=now,
            listening_since=world.listening_since,
            listening_generation=world.listening_generation,
        )
    kind: Any = {
        "line": LINE_ARMS,
        "floor": FLOOR,
        "queue": queue._PART,  # noqa: SLF001
        "evidence": evidence._PART,  # noqa: SLF001
        "prediction": predictions._PART,  # noqa: SLF001
        "curation": curation._PART,  # noqa: SLF001
        "settled_refusals": settled_refusals._PART,  # noqa: SLF001
    }[part]
    if part in ("line", "floor") and not kind.active(instance):
        raise AssertionError(f"the {part} consumer is not active")
    daemon_id = manager.consumer_runner.daemon_id
    kind.match(instance, now=now, daemon_id=daemon_id)
    units = tuple(kind.due(instance, now=now))
    for unit in units:
        kind.run(manager, world.instance_id, unit, now=now)
    return units


# -- the world ----------------------------------------------------------------------------


def _git_workspace(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", "--object-format=sha1", str(path)],
        check=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    return path


def build_world(directory: Path, shape: Shape) -> World:
    """Initialize, seed and grow one world under ``directory`` (a fresh scratch path)."""

    from cruxible_core.runtime.host_api import attach_workspace

    instance_id = f"inst_scale_{shape.name}"
    registered = get_registry().create_governed_instance_with_id(instance_id)
    managed = Path(registered.record.location)
    owner = generate_client_principal_key(
        directory / "operator-custody",
        principal_id=OPERATOR,
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    playbill_api.playbill_init(instance_id, principals=(owner.principal,))
    world = World(
        shape=shape,
        instance_id=instance_id,
        owner=owner,
        workspace=_git_workspace(directory / "workspace"),
    )
    _seed_vocabulary(world.instance, actor_id=OPERATOR)
    settle_advertisement(world.instance)
    _bulk(world)
    accept_members(
        world,
        {**definitions(world), **_retired_cadences(world)},
        name=f"scale-definitions-{shape.name}",
    )
    # The newest items, so no route's attestation meets an earlier one by the same principal.
    for index in range(shape.attestations):
        subject = world.items[-1 - index]
        append_attestation(world, attestation_request(world, subject, basis="examined_existing"))
    # The workspace attaches (floor delivery on) after the bulk: an attached
    # workspace fetches the ledger after every write, which the bulk need not pay.
    attach_workspace(instance_id, str(world.workspace))
    playbill_api.playbill_line_enable(instance_id, f"{TICK}-line")
    settle_advertisement(world.instance)
    world.listening_since = datetime.now(UTC)
    with world.instance.accepted_history_reader() as history:
        world.listening_generation = history.sequence
    # Every consumer part has run once, so a measured tick is a steady-state
    # tick, never a part's first catch-up over the whole instance.
    for part in TICK_PARTS:
        tick(world, part)
    settle_advertisement(world.instance)
    return world
