"""The November 1 value, provenance and findings rulings, through served runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.procedures.line_specs import LineFindings
from cruxible_client.contracts.refs import step_ref
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationStore,
    ReobservationKey,
    ReobservationReader,
)
from cruxible_core.procedures.execution import _resolve_reference
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.support.procedures import procedure_spec
from tests.test_pilot.support import pilot_world
from tests.test_procedures.test_claims_fix import _inbox
from tests.test_procedures.test_claims_terminal import (
    PREDICATE,
    activate,
    candidate_claims,
    claims_spec,
    claims_world,
    install,
    run,
)


def test_same_kev_values_in_changed_catalog_reobserve_without_a_proposal(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev.json")
    first = run(instance, root)
    claims = candidate_claims(instance, first)
    activate(instance, first)
    before = instance.accepted_coordinate()
    path = root / "data/kev.json"
    document = json.loads(path.read_text())
    document["catalogVersion"] = "2026.11.01"
    path.write_text(json.dumps(document))
    again = run(instance, root, minute=1)
    assert again.status == "succeeded", again.terminal
    assert again.terminal_egress[0].proposal_id is None
    assert not again.terminal_egress[0].target_paths
    assert instance.accepted_coordinate() == before
    store = ClaimReobservationStore(instance)
    from datetime import timedelta

    from cruxible_client.contracts.captures import observed_content, parse_capture_envelope

    for claim in claims:
        (observation,) = store.history(claim.identity.name)
        assert observation.capture_digest not in claim.backing.capture_digests
        original = parse_capture_envelope(
            instance.body_store().read(
                claim.backing.procedure_evidence.value.capture_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        reading = ReobservationReader.of(instance).latest(
            claim.identity.name,
            (
                ReobservationKey(
                    claim.backing.procedure_evidence.value.capture_digest,
                    original.capture_contract_digest,
                    observed_content(original).digest,
                ),
            ),
            observation.recorded_at + timedelta(seconds=1),
        )
        assert reading.evidence[0].observed_at > original.observed_at
        assert reading.evidence[0].reobserving_capture_digest == observation.capture_digest
    assert store.head_sequence() == len(claims)


@pytest.mark.parametrize("selected", [(), ("extract_refused",)])
def test_findings_route_only_when_the_line_declares_them(
    tmp_path: Path, selected: tuple[str, ...]
) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "Maybe"}]}
    )
    install(instance, claims_spec(), findings=selected)
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    assert first.findings_count == 1 and first.findings[0].kind == "extract_refused"
    assert len(_inbox(instance)) == len(selected)
    second = run(instance, root, minute=1)
    assert second.findings_count == 1
    assert len(_inbox(instance)) == len(selected)


def test_duplicate_finding_names_the_key_and_every_locator(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev-duplicates.json")
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (finding,) = [f for f in state.findings if f.kind == "duplicate_key"]
    assert finding.key == {"cve_id": "CVE-2026-0001"}
    assert finding.record_locators == ("/vulnerabilities/0", "/vulnerabilities/2")
    assert any(item.payload["kind"] == "duplicate_key" for item in _inbox(instance))


def test_unknown_finding_kind_is_refused_at_authoring() -> None:
    with pytest.raises(ValidationError):
        LineFindings.model_validate({"inbox": ["anything"]})


@pytest.mark.parametrize(
    "value,source",
    [("open", "literal"), ("$item.fields.status", "field"), ("$params.status", "param")],
)
def test_get_derives_value_source_from_the_pinned_template(
    tmp_path: Path, value: str, source: str
) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    spec = claims_spec(mappings=[{"type": PREDICATE, "value": value}])
    if source == "param":
        spec = procedure_spec(
            base=spec.model_dump(mode="python", by_alias=True),
            params={"status": "enum[open, closed, unknown]"},
        )
    install(instance, spec, parameters={"status": "open"} if source == "param" else {})
    state = run(instance, root)
    (claim,) = candidate_claims(instance, state)
    receipt = claim.backing.procedure_evidence
    assert receipt.template == 0 and receipt.node_id == "write"
    assert "value_source" not in receipt.model_dump(mode="json")
    activate(instance, state)
    card = service_playbill_get(
        instance,
        request=GetRequest(ref=claim.identity.name),
        access=BodyAccessContext(principal_id="test", can_read_body=True),
    ).card
    assert card.value_source == source


def test_list_projection_requires_an_explicit_segment() -> None:
    rows = {"rows": [{"name": "a"}, {"name": "b"}]}
    ref = step_ref("read", "rows", "[*]", "name")
    assert _resolve_reference(
        ref.graph_text(), input_payload={}, parameters={}, outputs={"read": rows}
    ) == ["a", "b"]
    from cruxible_core.procedures.execution import ExecutionError

    with pytest.raises(ExecutionError):
        _resolve_reference(
            "$steps.read.rows.name", input_payload={}, parameters={}, outputs={"read": rows}
        )


@pytest.mark.parametrize("role", ["observation", "derivation"])
def test_computing_extracted_values_requires_a_derivation_role(tmp_path: Path, role: str) -> None:
    from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy
    from tests.test_procedures.test_claims_terminal import claim_type

    ct = claim_type()
    ct = ct.model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=tuple(
                    rule.model_copy(
                        update={"admission": "derivational" if role == "derivation" else "direct"}
                    )
                    for rule in ct.evidence_admission_policy.rules
                )
            )
        }
    )
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(ct,)
    )
    base = claims_spec().model_dump(mode="json", by_alias=True)
    from tests.support.procedures import refs

    base["steps"].insert(
        -1,
        refs(
            {
                "transform": "computed",
                "kind": "derive",
                "spec": {
                    "input": {"rows": "$steps.entries.records"},
                    "cases": [
                        {
                            "when": {"op": "eq", "left": {"literal": 1}, "right": {"literal": 1}},
                            "value": "closed",
                        }
                    ],
                    "else": "open",
                },
            }
        ),
    )
    base["steps"][-1]["claims"][0].update(
        value=step_ref("computed", "result").model_dump(mode="json", by_alias=True), role=role
    )
    install(instance, procedure_spec(base=base))
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    if role == "derivation":
        (claim,) = candidate_claims(instance, state)
        assert claim.statement.object.value == "closed"
        assert claim.backing.procedure_evidence.value.basis == "derived"
        assert claim.backing.procedure_evidence.value.transform_node_ids == ("computed",)
    else:
        assert state.terminal_egress[0].proposal_id is None
        assert state.findings[0].problem == "derived_role_required"


def test_static_projection_preserves_the_projected_element_type() -> None:
    from cruxible_client.contracts.procedures.authored import (
        ProcedureSpecError,
        lower_procedure_spec,
    )
    from cruxible_client.contracts.shapes import Contract, ContractRef

    row = Contract.model_validate(
        {"name": "test.projection-row", "version": 1, "fields": {"name": "string"}}
    )
    spec = procedure_spec(
        "test.projection",
        input={"rows": "list<test.projection-row@1>{max_items: 3}"},
        steps=[
            {
                "project": "out",
                "fields": {"names": {"$ref": "input", "path": ["rows", "[*]", "name"]}},
            }
        ],
        returns="out",
    )

    def resolve(kind: str, ref: object) -> object:
        return (
            row
            if kind == "contract" and isinstance(ref, ContractRef) and ref.name == row.name
            else None
        )

    lowered = lower_procedure_spec(spec, resolve=resolve)
    assert lowered.generated["output"]["names"].type.item.kind == "string"
    assert lowered.generated["output"]["names"].type.max_items == 3
    bad = spec.model_dump(mode="json", by_alias=True)
    bad["steps"][0]["fields"]["names"]["path"] = ["rows", "name"]
    with pytest.raises(ProcedureSpecError, match="requires"):
        lower_procedure_spec(procedure_spec(base=bad), resolve=resolve)


def test_reobservation_recovery_replays_the_core_changes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after one journal append resumes without losing or duplicating observations."""
    from datetime import timedelta

    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
    from tests.support.operation_world import NOW

    instance, root = pilot_world(tmp_path, "kev.json")
    first = run(instance, root)
    claims = candidate_claims(instance, first)
    activate(instance, first)
    before = instance.accepted_coordinate()
    path = root / "data/kev.json"
    document = json.loads(path.read_text())
    document["catalogVersion"] = "2026.11.01"
    path.write_text(json.dumps(document))

    class Crash(BaseException):
        pass

    original = ClaimReobservationStore.record

    def crash_after_append(self, **kwargs):
        original(self, **kwargs)
        raise Crash()

    monkeypatch.setattr(ClaimReobservationStore, "record", crash_after_append)
    with pytest.raises(Crash):
        run(instance, root, minute=1)
    monkeypatch.undo()
    recovered = service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(minutes=2))
    assert set(recovered.values()) == {"delivered"}
    assert instance.accepted_coordinate() == before
    store = ClaimReobservationStore(instance)
    for claim in claims:
        assert len(store.history(claim.identity.name)) == 1
    assert store.head_sequence() == len(claims)
