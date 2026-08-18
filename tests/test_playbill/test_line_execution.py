"""§8.5 five-term effective-rung cap, typed egress, and the v1 effect gate."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, get_args

import pytest

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_core.playbill.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.captures import (
    CanonicalDurationV1,
    parse_capture_envelope,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.errors import PlaybillExecutionError
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_core.playbill.procedures.egress import (
    EFFECTIVE_RUNG_TERMS,
    NO_TERMINAL_EGRESS,
    RECOGNIZED_EFFECT_GRANT_TAGS,
    CaptureTerminalEgressSink,
    EffectiveRungV1,
    TerminalEgressChildReceiptV1,
    TerminalEgressError,
    TerminalEgressReceiptV1,
    TerminalEgressRequestV1,
    compute_effective_rung,
    effective_rung_digest,
)
from cruxible_core.playbill.procedures.execution import (
    ProcedureExecutor,
    ProcedureRunResultV1,
    ProviderInvocationResultV1,
    prepare_direct_procedure_run,
)
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.models import (
    CaptureEgressNodeV3,
    InboxEgressNodeV3,
    MandateSettlementNodeV3,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedureNodeV3,
    ProcedurePinSlotRefV1,
    ProposeChangeSetNodeV3,
    ProviderNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_core.playbill.procedures.resolution import AcceptedAuthorityBasisV1
from cruxible_core.playbill.procedures.terminal_dependencies import (
    TAINT_UNPROMOTED_EXHAUST,
    TerminalDependencySlotV1,
    TerminalItemDependencyManifestV1,
)
from cruxible_core.playbill.run_inputs import (
    ProcedureMandateReadV1,
    admit_line_procedure_run,
    build_calibration_read,
    line_effective_rung,
    line_run_slot_pins,
    read_mandate_basis,
    select_line_run_sources,
)
from cruxible_core.playbill.standing_mandates import MandateGrantV1, MandateRuntimeCapV1
from tests.test_playbill._line_runtime_support import (
    CAPTURE_CONTRACT,
    CAPTURE_PIN,
    CONTRACT_IN,
    CONTRACT_OUT,
    FILTER_IN,
    FILTER_OUT,
    INTERFACE_DIGESTS,
    NOW,
    PRODUCER_BINDING,
    PROVIDER_PIN,
    SOURCE_PROVIDER,
    Authority,
    Contracts,
    FixedClock,
    LineRuntimeFixture,
    StateReader,
    accepted_procedure,
    actor,
    build_fixture,
    cadence_occurrence,
    coordinate,
    digest,
    pin,
    source_node,
)

_ACCESS = BodyAccessContext(principal_id="line-execution-test", can_read_body=True)

MANDATE_PIN = pin("mandate", "StandingMandate", "orders-refresh")
TARGET_LAW_PIN = pin("target-law", "Policy", "order-status-claim-law")
EFFECT_POLICY_PIN = pin("effect-policy", "EffectPolicy", "dispatch-restock")
EFFECT_PROVIDER_PIN = pin("provider", "Provider", "acme.dispatch")
ENVIRONMENT_PIN = pin("environment", "EnvironmentManifest", "production")
EFFECT_IN = pin("contract-in", "Contract", "effect-input")
EFFECT_OUT = pin("contract-out", "Contract", "effect-output")

MANDATE_BASIS = digest("mandate-basis-live")
EXPIRED_BASIS = digest("mandate-basis-expired")

SETTLE_GRANT = MandateGrantV1(
    settlement="settle_named_deltas",
    permitted_operations=("activate_change_set", "compile_capture", "propose_change_set"),
)
PROPOSE_ONLY_GRANT = MandateGrantV1(
    settlement="propose_only",
    permitted_operations=("compile_capture", "propose_change_set"),
)


# ---------------------------------------------------------------------------
# Graph, authority, and sink fixtures
# ---------------------------------------------------------------------------


def _nodes(terminal: ProcedureNodeV3, *, acquire: bool = False) -> tuple[ProcedureNodeV3, ...]:
    """The smallest graph that reaches one terminal, optionally through a source."""

    read = StateTapNodeV3(
        node_id="read",
        query=ProcedurePinSlotRefV1(slot_name="query"),
        parameters={"status": "open"},
        as_="rows",
        next="fetch" if acquire else "pick",
    )
    pick = TransformNodeV3(
        node_id="pick",
        transform_kind="filter_items",
        contract_in=FILTER_IN,
        contract_out=FILTER_OUT,
        spec={"items": "$steps.rows.items", "where": {"status": "open"}},
        as_="picked",
        next="emit",
    )
    if acquire:
        return (read, source_node(next_node="pick"), pick, terminal)
    return (read, pick, terminal)


def inbox_terminal() -> InboxEgressNodeV3:
    return InboxEgressNodeV3(node_id="emit", input={"items": "$steps.picked.items"})


def capture_terminal() -> CaptureEgressNodeV3:
    return CaptureEgressNodeV3(
        node_id="emit",
        capture_contract=CAPTURE_PIN,
        input={"items": "$steps.picked.items"},
    )


def proposal_terminal(*, observe: bool = False) -> ProposeChangeSetNodeV3:
    template: dict[str, object] = {"subject": "$steps.picked.items"}
    if observe:
        template["observation"] = "$steps.orders"
    return ProposeChangeSetNodeV3(node_id="emit", candidate_templates=(template,))


def settlement_terminal() -> MandateSettlementNodeV3:
    return MandateSettlementNodeV3(
        node_id="emit",
        mandate=MANDATE_PIN,
        target_law=TARGET_LAW_PIN,
        input={"items": "$steps.picked.items"},
    )


def _procedure(
    terminal: ProcedureNodeV3,
    *,
    acquire: bool = False,
    terminal_capability: Literal[1, 2, 3] = 3,
) -> AcceptedProcedureV1:
    return accepted_procedure(
        nodes=_nodes(terminal, acquire=acquire),
        terminal_capability=terminal_capability,
        extra_pins=(MANDATE_PIN, TARGET_LAW_PIN),
    )


def _authority_basis(*, valid: bool = True) -> AcceptedAuthorityBasisV1:
    artifact = digest("mandate-artifact")
    return AcceptedAuthorityBasisV1(
        kind="standing_mandate",
        basis_digest=MANDATE_BASIS if valid else EXPIRED_BASIS,
        accepted_coordinate=coordinate(),
        valid_from=NOW - timedelta(days=1) if valid else NOW - timedelta(days=10),
        valid_until=NOW + timedelta(days=1) if valid else NOW - timedelta(days=5),
        current_artifact_digest=artifact,
        artifact_digest=artifact,
    )


def _mandate_read(*, live: bool = False, expired: bool = False) -> ProcedureMandateReadV1:
    candidates = []
    if live:
        candidates.append(_authority_basis(valid=True))
    if expired:
        candidates.append(_authority_basis(valid=False))
    return read_mandate_basis(
        tuple(sorted({item.basis_digest for item in candidates})),
        accepted_basis={item.basis_digest: item for item in candidates},
        accepted_coordinate=coordinate(),
        evaluation_time=NOW,
    )


class RecordingEgressSink:
    """A sink that records exactly what each typed egress handed it."""

    def __init__(
        self,
        *,
        disposition: str | None = None,
        bound_artifact_digest: str | None = None,
        drop_child: bool = False,
    ) -> None:
        self.requests: list[TerminalEgressRequestV1] = []
        self.disposition = disposition
        self.bound_artifact_digest = bound_artifact_digest
        self.drop_child = drop_child

    def deliver_terminal_egress(
        self,
        *,
        request: TerminalEgressRequestV1,
    ) -> TerminalEgressReceiptV1:
        self.requests.append(request)
        items = request.items[:-1] if self.drop_child and len(request.items) > 1 else request.items
        bound = (
            None
            if request.bound_artifact_pin is None
            else request.bound_artifact_pin.artifact_digest
        )
        return TerminalEgressReceiptV1(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition=self.disposition
            or {  # type: ignore[arg-type]
                "emit_capture": "emitted",
                "post_inbox": "posted",
                "propose_change_set": "received",
                "mandate_settlement": "settled",
            }[request.kind],
            bound_artifact_digest=self.bound_artifact_digest or bound,
            children=tuple(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=typed_digest(
                        Sha256Value,
                        "playbill-e2-egress-test-v1",
                        {"item_key": item.item_key, "kind": request.kind},
                    ).tagged,
                )
                for item in items
            ),
        )


class ProviderRunner:
    """One provider executor that always succeeds, which is never world truth."""

    def __init__(self) -> None:
        self.calls: list[ArtifactPin] = []

    def execute_provider(
        self,
        *,
        provider: ArtifactPin,
        environment: ArtifactPin,
        contract_in: ArtifactPin,
        contract_out: ArtifactPin,
        payload: object,
        actor_context: object,
    ) -> ProviderInvocationResultV1:
        self.calls.append(provider)
        return ProviderInvocationResultV1(
            output={"dispatched": True, "vendor_ticket": "T-9001"},
            trace={"status": "ok"},
        )


def _capture_sink(fixture: LineRuntimeFixture) -> CaptureTerminalEgressSink:
    return CaptureTerminalEgressSink(
        store=fixture.captures,
        contracts={CAPTURE_PIN.artifact_digest: CAPTURE_CONTRACT},
        producer=SOURCE_PROVIDER.identity,
        producer_binding_digest=PRODUCER_BINDING.digest,
    )


def _admit(
    fixture: LineRuntimeFixture,
    *,
    mandate_read: ProcedureMandateReadV1 | None = None,
    taint_labels: tuple[str, ...] = (),
    attempt: int = 1,
    run_id: str = "line-run-1",
):
    selection = select_line_run_sources(
        fixture.policy,
        (),
        anchor=None,
        evaluation_time=NOW,
        source_input_names=frozenset({"orders"}),
    )
    return admit_line_procedure_run(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        policy=fixture.policy,
        deployment=fixture.deployment,
        lease=fixture.lease,
        occurrence=cadence_occurrence(),
        attempt=attempt,
        run_id=run_id,
        accepted_coordinate=coordinate(),
        invocation_input={"request": "triage"},
        actor_context=actor(),
        state_reader=StateReader(),
        selection=selection,
        binding_snapshot=fixture.binding_snapshot(),
        mandate_read=mandate_read or _mandate_read(),
        sensitivity_policy=fixture.sensitivity(),
        interface_digests=INTERFACE_DIGESTS,
        admitted_at=NOW,
        acquirer=fixture.acquirer,
        taint_labels=taint_labels,
    )


def _rung(
    fixture: LineRuntimeFixture,
    *,
    mandate_read: ProcedureMandateReadV1 | None = None,
    mandate_grants: dict[str, MandateGrantV1] | None = None,
    calibration_caps: tuple[MandateRuntimeCapV1, ...] = (),
    taint_labels: tuple[str, ...] = (),
) -> EffectiveRungV1:
    return line_effective_rung(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        sensitivity_policy=fixture.sensitivity(),
        mandate_read=mandate_read or _mandate_read(),
        calibration=build_calibration_read(
            accepted_line=fixture.accepted_line,
            occurrence=cadence_occurrence(),
            accepted_coordinate=coordinate(),
        ),
        mandate_grants=mandate_grants,
        calibration_caps=calibration_caps,
        taint_labels=taint_labels,
        evaluation_time=NOW,
    )


def _executor(
    fixture: LineRuntimeFixture,
    *,
    effective_rung: EffectiveRungV1 | None = None,
    egress_sink: object | None = None,
    provider_executor: object | None = None,
    declared_effect_grants: tuple[str, ...] = (),
) -> ProcedureExecutor:
    return ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token=fixture.lease.fencing_token,
        activation_authority=Authority(fixture.accepted_procedure.artifact_digest),
        contract_validator=Contracts(),
        provider_executor=provider_executor,  # type: ignore[arg-type]
        source_acquirer=fixture.acquirer,
        acquisition_policy=fixture.policy,
        slot_pins=line_run_slot_pins(fixture.accepted_line),
        effective_rung=effective_rung,
        egress_sink=egress_sink,  # type: ignore[arg-type]
        declared_effect_grants=declared_effect_grants,
        clock=FixedClock(),
    )


def _payloads(fixture: LineRuntimeFixture, kind: str) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for stored in fixture.journal.all_records(fixture.stream, fixture.run_partition):
        if stored.record.event_kind != kind:
            continue
        payload = parse_journal_payload(
            fixture.bodies.read(stored.record.payload_digest, access=_ACCESS)
        )
        assert isinstance(payload, dict)
        found.append(payload)
    return found


def _run(
    fixture: LineRuntimeFixture,
    *,
    rung: EffectiveRungV1 | None,
    sink: object | None,
    mandate_read: ProcedureMandateReadV1 | None = None,
    taint_labels: tuple[str, ...] = (),
) -> ProcedureRunResultV1:
    prepared = _admit(fixture, mandate_read=mandate_read, taint_labels=taint_labels)
    return _executor(fixture, effective_rung=rung, egress_sink=sink).execute(
        prepared,
        fixture.accepted_procedure,
    )


# ---------------------------------------------------------------------------
# The five-term cap
# ---------------------------------------------------------------------------


def _terms(**overrides: object) -> EffectiveRungV1:
    """One rung computation with every term wide open unless overridden."""

    arguments: dict[str, object] = {
        "procedure_terminal_capability": 3,
        "requested_terminal_rung": 3,
        "selector_privacies": {"orders": "direct_allowed"},
        "taint_labels": (),
        "mandate_grants": {MANDATE_BASIS: SETTLE_GRANT},
        "calibration_caps": (),
        "evaluation_time": NOW,
        "procedure_definition_digest": digest("definition"),
        "line_spec_digest": digest("line"),
        "sensitivity_policy_digest": digest("sensitivity"),
        "mandate_coordinate_digest": digest("mandate-coordinate"),
        "calibration_coordinate_digest": digest("calibration-coordinate"),
    }
    arguments.update(overrides)
    return compute_effective_rung(**arguments)  # type: ignore[arg-type]


def test_every_term_can_be_the_one_that_caps_the_run() -> None:
    """Each of the five §8.5.1 terms limits alone, and names itself when it does."""

    unrestricted = _terms()
    assert unrestricted.effective_rung == 3
    assert tuple(item.term for item in unrestricted.terms) == EFFECTIVE_RUNG_TERMS

    cases = {
        "procedure_terminal_capability": ({"procedure_terminal_capability": 2}, 2),
        "line_requested_rung": ({"requested_terminal_rung": 1}, 1),
        "propagated_sensitivity": (
            {"selector_privacies": {"orders": "pseudonymous_required"}},
            2,
        ),
        "mandate_grant": ({"mandate_grants": {}}, 2),
        "calibration": (
            {
                "calibration_caps": (
                    MandateRuntimeCapV1(
                        cap_kind="calibration",
                        permitted_operations=("compile_capture", "propose_change_set"),
                    ),
                )
            },
            2,
        ),
    }
    for term, (override, expected) in cases.items():
        rung = _terms(**override)
        assert rung.effective_rung == expected, term
        assert rung.limiting_term == term
        assert rung.refusal_code == f"terminal_rung_capped_by_{term}"
        assert rung.term(term).rung == expected  # type: ignore[arg-type]


def test_no_term_may_widen_another() -> None:
    """A grant, a calibration cap, and clean inputs never lift a lower term."""

    assert (
        _terms(
            requested_terminal_rung=1,
            mandate_grants={MANDATE_BASIS: SETTLE_GRANT},
            calibration_caps=(
                MandateRuntimeCapV1(
                    cap_kind="calibration",
                    permitted_operations=(
                        "activate_change_set",
                        "compile_capture",
                        "propose_change_set",
                    ),
                ),
            ),
        ).effective_rung
        == 1
    )
    both = _terms(procedure_terminal_capability=2, requested_terminal_rung=2)
    assert both.effective_rung == 2
    assert both.limiting_term == "procedure_terminal_capability"


def test_absent_expired_and_propose_only_mandates_contribute_nothing() -> None:
    """Absence leaves the mandate-free ceiling exactly where it already was."""

    assert _terms(mandate_grants={}).term("mandate_grant").rung == 2
    assert _terms(mandate_grants={MANDATE_BASIS: PROPOSE_ONLY_GRANT}).effective_rung == 2

    expired_read = _mandate_read(live=False, expired=True)
    assert expired_read.resolved_basis_digests == ()
    assert expired_read.requested_basis_digests == (EXPIRED_BASIS,)


def test_an_unrecognized_taint_or_privacy_label_refuses_every_governed_egress() -> None:
    """An epistemic constraint this version cannot read is refused, never graded."""

    unknown_taint = _terms(taint_labels=("playbill.taint.some-future-constraint",))
    assert unknown_taint.effective_rung == NO_TERMINAL_EGRESS
    assert unknown_taint.limiting_term == "propagated_sensitivity"

    unknown_privacy = _terms(selector_privacies={"orders": "future_privacy_mode"})
    assert unknown_privacy.effective_rung == NO_TERMINAL_EGRESS


def test_calibration_caps_narrow_monotonically_and_stay_calibration() -> None:
    def cap(**kwargs: object) -> MandateRuntimeCapV1:
        return MandateRuntimeCapV1(cap_kind="calibration", **kwargs)  # type: ignore[arg-type]

    assert _terms(calibration_caps=(cap(suspended=True),)).effective_rung == NO_TERMINAL_EGRESS
    assert (
        _terms(calibration_caps=(cap(valid_until=NOW - timedelta(hours=1)),)).effective_rung
        == NO_TERMINAL_EGRESS
    )
    assert (
        _terms(calibration_caps=(cap(permitted_operations=("propose_change_set",)),)).effective_rung
        == NO_TERMINAL_EGRESS
    )
    assert (
        _terms(calibration_caps=(cap(permitted_operations=("compile_capture",)),)).effective_rung
        == 1
    )
    assert _terms(calibration_caps=(cap(valid_until=NOW + timedelta(hours=1)),)).effective_rung == 3

    with pytest.raises(TerminalEgressError, match="calibration caps only"):
        _terms(
            calibration_caps=(
                MandateRuntimeCapV1(cap_kind="safety", permitted_operations=("compile_capture",)),
            )
        )


def test_a_rung_that_is_not_the_minimum_of_its_own_terms_is_unconstructible() -> None:
    honest = _terms(requested_terminal_rung=1)
    with pytest.raises(ValueError, match="minimum of its own terms"):
        EffectiveRungV1.model_validate(honest.model_dump(mode="json") | {"effective_rung": 3})
    with pytest.raises(ValueError, match="first term reaching the minimum"):
        EffectiveRungV1.model_validate(
            honest.model_dump(mode="json") | {"limiting_term": "calibration"}
        )


def test_epsilon_membership_and_the_rung_are_stable_across_retries(tmp_path) -> None:
    """A retry is the same occurrence, so it draws the same epsilon and the same cap."""

    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    first = _rung(fixture)
    second = _rung(fixture)
    assert effective_rung_digest(first) == effective_rung_digest(second)

    first_attempt = _admit(fixture, attempt=1, run_id="line-run-a")
    retry = _admit(fixture, attempt=2, run_id="line-run-b")
    assert first_attempt.admission.epsilon_member == retry.admission.epsilon_member
    assert (
        first_attempt.admission.calibration_coordinate_digest
        == retry.admission.calibration_coordinate_digest
        == first.calibration_coordinate_digest
    )


# ---------------------------------------------------------------------------
# Typed egress
# ---------------------------------------------------------------------------


def test_emit_capture_emits_inert_evidence_through_the_capture_machinery(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(capture_terminal()))
    sink = _capture_sink(fixture)
    result = _run(fixture, rung=_rung(fixture), sink=sink)
    assert result.status == "succeeded"

    egress = _payloads(fixture, "terminal_egress")[0]
    assert egress["verdict"] == "delivered"
    assert egress["kind"] == "emit_capture"
    assert egress["required_rung"] == 0
    assert egress["granted_operation"] == "compile_capture"

    receipt = TerminalEgressReceiptV1.model_validate(egress["receipt"])
    assert receipt.disposition == "emitted"
    assert receipt.bound_artifact_digest == CAPTURE_PIN.artifact_digest
    envelope = parse_capture_envelope(
        fixture.captures.read(receipt.children[0].egress_digest, access=_ACCESS)
    )
    assert envelope.run_coordinate.run_kind == "procedure"
    assert envelope.run_coordinate.run_id == "line-run-1"
    assert envelope.capture_contract_digest == CAPTURE_PIN.artifact_digest


def test_post_inbox_delivers_at_rung_one_and_carries_no_granted_operation(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    sink = RecordingEgressSink()
    assert _run(fixture, rung=_rung(fixture), sink=sink).status == "succeeded"

    request = sink.requests[0]
    assert request.kind == "post_inbox"
    assert request.required_rung == 1
    assert request.granted_operation is None
    assert request.bound_artifact_pin is None and request.mandate_pin is None
    assert [item.value for item in request.items] == [{"id": "a", "status": "open"}]


def test_propose_change_set_reaches_receive_only_and_can_never_activate(tmp_path) -> None:
    """Rung 2 consumes ``propose_change_set``; nothing in its shape can activate."""

    fixture = build_fixture(
        tmp_path,
        accepted=_procedure(proposal_terminal()),
        requested_terminal_rung=2,
    )
    sink = RecordingEgressSink()
    assert _run(fixture, rung=_rung(fixture), sink=sink).status == "succeeded"

    request = sink.requests[0]
    assert request.required_rung == 2
    assert request.granted_operation == "propose_change_set"
    assert request.mandate_basis_digests == ()
    assert "activate" not in str(request.model_dump(mode="json"))
    assert not hasattr(proposal_terminal(), "activation")

    with pytest.raises(ValueError, match="propose_change_set egress reports 'received'"):
        TerminalEgressReceiptV1(
            kind="propose_change_set",
            run_id="line-run-1",
            node_id="emit",
            disposition="settled",
            children=(
                TerminalEgressChildReceiptV1(
                    child_index=0,
                    item_key="00000000.deadbeef",
                    egress_digest=digest("candidate"),
                ),
            ),
        )


def test_mandate_settlement_traverses_the_pinned_target_law_with_the_resolved_mandate(
    tmp_path,
) -> None:
    fixture = build_fixture(
        tmp_path,
        accepted=_procedure(settlement_terminal()),
        requested_terminal_rung=3,
    )
    live = _mandate_read(live=True)
    rung = _rung(fixture, mandate_read=live, mandate_grants={MANDATE_BASIS: SETTLE_GRANT})
    assert rung.effective_rung == 3

    sink = RecordingEgressSink()
    result = _run(fixture, rung=rung, sink=sink, mandate_read=live)
    assert result.status == "succeeded"

    request = sink.requests[0]
    assert request.kind == "mandate_settlement"
    assert request.granted_operation == "activate_change_set"
    assert request.bound_artifact_pin == TARGET_LAW_PIN
    assert request.mandate_pin == MANDATE_PIN
    assert request.mandate_basis_digests == (MANDATE_BASIS,)


def test_a_settlement_receipt_that_missed_the_pinned_law_refuses(tmp_path) -> None:
    fixture = build_fixture(
        tmp_path,
        accepted=_procedure(settlement_terminal()),
        requested_terminal_rung=3,
    )
    live = _mandate_read(live=True)
    rung = _rung(fixture, mandate_read=live, mandate_grants={MANDATE_BASIS: SETTLE_GRANT})
    result = _run(
        fixture,
        rung=rung,
        sink=RecordingEgressSink(bound_artifact_digest=digest("another-law")),
        mandate_read=live,
    )
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "terminal_egress_unverified"
    assert "exact pinned artifact" in result.refusal.message


def test_landing_and_calibration_alone_never_grant_rung_three(tmp_path) -> None:
    """A cadence occurrence and a permissive calibration cap cannot settle."""

    fixture = build_fixture(
        tmp_path,
        accepted=_procedure(settlement_terminal()),
        requested_terminal_rung=3,
    )
    rung = _rung(
        fixture,
        calibration_caps=(
            MandateRuntimeCapV1(
                cap_kind="calibration",
                permitted_operations=(
                    "activate_change_set",
                    "compile_capture",
                    "propose_change_set",
                ),
            ),
        ),
    )
    assert rung.effective_rung == 2
    assert rung.limiting_term == "mandate_grant"

    result = _run(fixture, rung=rung, sink=RecordingEgressSink())
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "terminal_rung_capped_by_mandate_grant"

    egress = _payloads(fixture, "terminal_egress")[0]
    assert egress["verdict"] == "refused_effective_rung"
    assert egress["limiting_term"] == "mandate_grant"
    assert egress["required_rung"] == 3 and egress["effective_rung"] == 2
    assert egress["children"], "a refused terminal still owes its bound closure"


def test_a_sensitivity_capped_run_names_sensitivity_and_still_binds_its_closure(
    tmp_path,
) -> None:
    fixture = build_fixture(
        tmp_path,
        accepted=_procedure(settlement_terminal()),
        requested_terminal_rung=3,
    )
    live = _mandate_read(live=True)
    rung = _rung(
        fixture,
        mandate_read=live,
        mandate_grants={MANDATE_BASIS: SETTLE_GRANT},
        taint_labels=(TAINT_UNPROMOTED_EXHAUST,),
    )
    assert rung.limiting_term == "propagated_sensitivity"

    result = _run(
        fixture,
        rung=rung,
        sink=RecordingEgressSink(),
        mandate_read=live,
        taint_labels=(TAINT_UNPROMOTED_EXHAUST,),
    )
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "terminal_rung_capped_by_propagated_sensitivity"
    assert len(_payloads(fixture, "item_dependencies")) == 1


def test_terminal_egress_without_a_bound_rung_or_sink_refuses(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    result = _run(fixture, rung=None, sink=None)
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "terminal_not_available"


def test_a_rung_computed_for_another_admission_is_refused(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    foreign = _rung(fixture).model_copy(update={"line_spec_digest": digest("another-line")})
    with pytest.raises(PlaybillExecutionError, match="another admission binding"):
        _executor(fixture, effective_rung=foreign, egress_sink=RecordingEgressSink()).execute(
            _admit(fixture),
            fixture.accepted_procedure,
        )


def test_a_dropped_egress_child_refuses(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    reader = StateReader({"items": [{"id": "a", "status": "open"}, {"id": "b", "status": "open"}]})
    prepared = admit_line_procedure_run(
        accepted_line=fixture.accepted_line,
        accepted_procedure=fixture.accepted_procedure,
        policy=fixture.policy,
        deployment=fixture.deployment,
        lease=fixture.lease,
        occurrence=cadence_occurrence(),
        attempt=1,
        run_id="line-run-1",
        accepted_coordinate=coordinate(),
        invocation_input={"request": "triage"},
        actor_context=actor(),
        state_reader=reader,
        selection=select_line_run_sources(
            fixture.policy,
            (),
            anchor=None,
            evaluation_time=NOW,
            source_input_names=frozenset({"orders"}),
        ),
        binding_snapshot=fixture.binding_snapshot(),
        mandate_read=_mandate_read(),
        sensitivity_policy=fixture.sensitivity(),
        interface_digests=INTERFACE_DIGESTS,
        admitted_at=NOW,
        acquirer=fixture.acquirer,
    )
    result = _executor(
        fixture,
        effective_rung=_rung(fixture),
        egress_sink=RecordingEgressSink(drop_child=True),
    ).execute(prepared, fixture.accepted_procedure)
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "terminal_egress_unverified"
    assert "drops or invents a child" in result.refusal.message


# ---------------------------------------------------------------------------
# The v1 effect gate
# ---------------------------------------------------------------------------


def _effect_definition() -> ProcedureDefinitionV3:
    return ProcedureDefinitionV3(
        name="dispatch-restock",
        contract_in=CONTRACT_IN,
        contract_out=CONTRACT_OUT,
        nodes=(
            ProviderNodeV3(
                node_id="dispatch",
                provider=EFFECT_PROVIDER_PIN,
                contract_in=EFFECT_IN,
                contract_out=EFFECT_OUT,
                environment=ENVIRONMENT_PIN,
                effect_policy=EFFECT_POLICY_PIN,
                input={"order_id": "$input.order_id"},
                as_="dispatched",
            ),
        ),
        returns="dispatched",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=5_000_000),
            max_provider_calls=1,
            max_capture_bytes=65_536,
            max_items=10,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=10_000_000),
            max_provider_calls=2,
            max_capture_bytes=131_072,
            max_items=20,
            max_repeat_attempts=2,
        ),
        terminal_capability=1,
    )


def _effect_procedure() -> AcceptedProcedureV1:
    definition = _effect_definition()
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        authority=ArtifactAuthority(
            propose_roles=("procedure-author",),
            approve_roles=("procedure-reviewer",),
        ),
        pins=tuple(
            sorted(
                {
                    CONTRACT_IN,
                    CONTRACT_OUT,
                    EFFECT_IN,
                    EFFECT_OUT,
                    EFFECT_POLICY_PIN,
                    EFFECT_PROVIDER_PIN,
                    ENVIRONMENT_PIN,
                },
                key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
            )
        ),
        activation_policy="drain",
    )
    return AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _direct_effect_run(
    fixture: LineRuntimeFixture,
    *,
    accepted: AcceptedProcedureV1,
    actor_type: str = "service_account",
    declared_effect_grants: tuple[str, ...] = (),
    run_id: str = "actor-effect-run",
) -> tuple[ProcedureRunResultV1, ProviderRunner]:
    prepared = prepare_direct_procedure_run(
        accepted,
        instance_id=fixture.deployment.instance_id,
        run_id=run_id,
        accepted_coordinate=coordinate(),
        invocation_input={"order_id": "o-1"},
        actor_context=actor().model_copy(update={"actor_type": actor_type}),
        state_reader=StateReader(),
        journal_stream=fixture.stream,
        journal_partition_id=fixture.run_partition,
        admitted_at=NOW,
    )
    provider_executor = ProviderRunner()
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token=fixture.lease.fencing_token,
        activation_authority=Authority(accepted.artifact_digest),
        contract_validator=Contracts(),
        provider_executor=provider_executor,
        declared_effect_grants=declared_effect_grants,
        clock=FixedClock(),
    )
    return executor.execute(prepared, accepted), provider_executor


def _line_effect_fixture(tmp_path) -> LineRuntimeFixture:
    """One line whose graph reaches an effectful provider before its terminal."""

    nodes = (
        StateTapNodeV3(
            node_id="read",
            query=ProcedurePinSlotRefV1(slot_name="query"),
            parameters={"status": "open"},
            as_="rows",
            next="dispatch",
        ),
        ProviderNodeV3(
            node_id="dispatch",
            provider=EFFECT_PROVIDER_PIN,
            contract_in=EFFECT_IN,
            contract_out=EFFECT_OUT,
            environment=ENVIRONMENT_PIN,
            effect_policy=EFFECT_POLICY_PIN,
            input={"rows": "$steps.rows.items"},
            as_="dispatched",
            next="emit",
        ),
        InboxEgressNodeV3(node_id="emit", input={"items": "$steps.rows.items"}),
    )
    accepted = accepted_procedure(
        nodes=nodes,
        returns="rows",
        terminal_capability=1,
        extra_pins=(
            EFFECT_IN,
            EFFECT_OUT,
            EFFECT_POLICY_PIN,
            EFFECT_PROVIDER_PIN,
            ENVIRONMENT_PIN,
        ),
    )
    return build_fixture(tmp_path, accepted=accepted, max_provider_calls=1)


def test_an_unattended_line_prepares_an_effect_intent_but_never_dispatches(tmp_path) -> None:
    """The line is otherwise fully authorized; v1 still refuses the dispatch."""

    fixture = _line_effect_fixture(tmp_path)
    provider_executor = ProviderRunner()
    prepared = _admit(fixture, mandate_read=_mandate_read(live=True))
    result = _executor(
        fixture,
        effective_rung=_rung(
            fixture,
            mandate_read=_mandate_read(live=True),
            mandate_grants={MANDATE_BASIS: SETTLE_GRANT},
        ),
        egress_sink=RecordingEgressSink(),
        provider_executor=provider_executor,
    ).execute(prepared, fixture.accepted_procedure)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "effect_dispatch_requires_actor"
    assert provider_executor.calls == []
    assert len(_payloads(fixture, "effect_intent")) == 1
    assert _payloads(fixture, "effect_result") == []
    assert _payloads(fixture, "terminal_egress") == []


def test_an_authenticated_actor_invocation_dispatches_the_effect(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    result, provider_executor = _direct_effect_run(fixture, accepted=_effect_procedure())
    assert result.status == "succeeded"
    assert provider_executor.calls == [EFFECT_PROVIDER_PIN]
    assert len(_payloads(fixture, "effect_intent")) == 1
    assert len(_payloads(fixture, "effect_result")) == 1


def test_a_system_context_is_not_the_authenticated_actor_effects_require(tmp_path) -> None:
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    result, provider_executor = _direct_effect_run(
        fixture,
        accepted=_effect_procedure(),
        actor_type="system",
    )
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "effect_dispatch_requires_authenticated_actor"
    assert provider_executor.calls == []


def test_a_future_effect_grant_tag_refuses_fail_closed(tmp_path) -> None:
    """V1 registers no effect grant, so an unknown tag is refused, not interpreted."""

    assert RECOGNIZED_EFFECT_GRANT_TAGS == frozenset()
    fixture = build_fixture(tmp_path, accepted=_procedure(inbox_terminal()))
    result, provider_executor = _direct_effect_run(
        fixture,
        accepted=_effect_procedure(),
        declared_effect_grants=("playbill-mandate-grant-v2",),
    )
    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "effect_grant_unrecognized"
    assert "playbill-mandate-grant-v2" in result.refusal.message
    assert provider_executor.calls == []


# ---------------------------------------------------------------------------
# World observation
# ---------------------------------------------------------------------------


def test_provider_success_is_never_world_truth_in_the_observation_round_trip(
    tmp_path,
) -> None:
    """Accepted state -> effect intent/result -> later Capture -> observation candidate.

    The effect provider reports success, and the later observation is backed by
    the Capture that really read the world.  No effect record can enter a
    terminal item's closure: the dependency slot vocabulary has no room for one.
    """

    assert "effect_result" not in get_args(TerminalDependencySlotV1)

    effect_fixture = build_fixture(tmp_path / "effect", accepted=_procedure(inbox_terminal()))
    effect_result, provider_executor = _direct_effect_run(
        effect_fixture,
        accepted=_effect_procedure(),
    )
    assert effect_result.status == "succeeded"
    assert provider_executor.calls == [EFFECT_PROVIDER_PIN]
    effect_payload = _payloads(effect_fixture, "effect_result")[0]
    effect_output_digest = effect_payload["output_digest"]

    observation = build_fixture(
        tmp_path / "observation",
        accepted=_procedure(proposal_terminal(observe=True), acquire=True),
        requested_terminal_rung=2,
    )
    result = _run(observation, rung=_rung(observation), sink=RecordingEgressSink())
    assert result.status == "succeeded"

    produced = _payloads(observation, "produced_capture")
    assert len(produced) == 1
    capture_digest_value = produced[0]["capture_digest"]

    manifest = TerminalItemDependencyManifestV1.model_validate(
        _payloads(observation, "item_dependencies")[0]["manifest"]
    )
    assert manifest.produced_capture_digests == (capture_digest_value,)
    assert effect_output_digest not in manifest.produced_capture_digests
    assert effect_output_digest not in manifest.accepted_state_input_digests
    assert effect_output_digest not in manifest.receipt_digests
    assert effect_output_digest not in manifest.policy_and_law_digests

    envelope = parse_capture_envelope(
        observation.captures.read(capture_digest_value, access=_ACCESS)
    )
    assert envelope.producer == SOURCE_PROVIDER.identity
    assert envelope.run_coordinate.executable_digest == PROVIDER_PIN.artifact_digest


def test_source_and_terminal_egress_never_share_a_capture_lineage(tmp_path) -> None:
    """The Capture a terminal emits is the run's own output, not its source input."""

    fixture = build_fixture(
        tmp_path,
        accepted=_procedure(capture_terminal(), acquire=True),
    )
    result = _run(fixture, rung=_rung(fixture), sink=_capture_sink(fixture))
    assert result.status == "succeeded"

    acquired = _payloads(fixture, "produced_capture")[0]["capture_digest"]
    emitted = (
        TerminalEgressReceiptV1.model_validate(_payloads(fixture, "terminal_egress")[0]["receipt"])
        .children[0]
        .egress_digest
    )
    assert acquired != emitted
    assert (
        parse_capture_envelope(
            fixture.captures.read(emitted, access=_ACCESS)
        ).run_coordinate.run_kind
        == "procedure"
    )
