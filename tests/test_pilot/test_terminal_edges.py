"""Additional first-terminal safety: creation, many values, contested slots, real derivation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.policies import ClaimResolutionPolicy
from tests.support.procedures import procedure_spec
from tests.test_procedures.test_claims_terminal import (
    KIND,
    PREDICATE,
    activate,
    candidate_claims,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)


def test_subject_if_absent_creates_once_and_never_does_not_create(tmp_path: Path) -> None:
    instance, root = claims_world(tmp_path, {"entries": []})
    doc = {"entries": [{"id": "CVE-2026-9999", "status": "open"}]}
    (root / "data/doc.json").write_text(json.dumps(doc))
    base = claims_spec().model_dump(mode="python", by_alias=True)
    install(instance, procedure_spec(base=base))
    none = run(instance, root)
    assert none.status == "succeeded", none.terminal
    assert none.terminal_egress[0].proposal_id is None
    base["procedure"] = "test.create"
    base["steps"][-1]["subject"]["create"] = "if_absent"
    spec = procedure_spec(base=base)
    install(instance, spec, line_name="test.create-line")
    created = run(instance, root, line="test.create-line", minute=1)
    assert created.status == "succeeded", created.terminal
    assert (
        len([p for p in created.terminal_egress[0].target_paths if p.startswith("subjects/")]) == 1
    )
    activate(instance, created)
    again = run(instance, root, line="test.create-line", minute=2)
    assert again.status == "succeeded", again.terminal
    assert again.terminal_egress[0].proposal_id is None


def test_many_valued_slot_adds_without_retiring_or_attesting(tmp_path: Path) -> None:
    ct = claim_type(
        cardinality="many",
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(ct,)
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (prior,) = candidate_claims(instance, first)
    activate(instance, first)
    (root / "data/doc.json").write_text(
        json.dumps({"entries": [{"id": "CVE-2026-0001", "status": "closed"}]})
    )
    second = run(instance, root, minute=1)
    (added,) = candidate_claims(instance, second)
    assert added.identity != prior.identity
    assert added.lifecycle.predecessor_digest is None
    assert len(second.terminal_egress[0].target_paths) == 1
    activate(instance, second)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert (
        parse_claim(
            tree[claim_path(prior.identity.name)], path=claim_path(prior.identity.name)
        ).lifecycle.state
        == "live"
    )


@pytest.mark.parametrize(
    ("settle", "listed"),
    [(False, False), (True, False), (True, True)],
    ids=["propose", "settle-fails-closed", "settle-derived-listed"],
)
def test_derived_claim_receipt_binds_a_real_admitted_read(
    tmp_path: Path, settle: bool, listed: bool
) -> None:
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )

    ct = claim_type(
        KIND + ".derived",
        "enum[open, closed, unknown]",
        permitted_roles=("derivation",),
        default_role=None,
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="derived",
                    claim_roles=("derivation",),
                    capture_contracts=("test.orders-v1",),
                    admission="derivational",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(claim_type(), ct)
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (basis,) = candidate_claims(instance, first)
    activate(instance, first)
    spec = procedure_spec(
        "test.derive",
        reference_text=True,
        input={},
        output={},
        steps=[
            {
                "read": "basis",
                "claim_type": PREDICATE,
                "subject_kind": KIND,
                "subject": "cve-2026-0001",
            },
            {
                "project": "rows",
                "fields": {
                    "rows": [
                        {
                            "key": "CVE-2026-0001",
                            "value": "$steps.basis.value",
                            "input": "$steps.basis",
                        }
                    ]
                },
            },
            {
                "id": "write",
                "for_each": "$steps.rows.rows",
                "subject": {"kind": KIND, "key": {"cve_id": "$item.key"}},
                "claims": [
                    {
                        "type": ct.predicate,
                        "value": "$item.value",
                        "role": "derivation",
                        "inputs": ["$item.input"],
                    }
                ],
            },
        ],
    )
    install(
        instance, spec, line_name="test.derive-line", authority="settle" if settle else "propose"
    )
    if settle:
        from tests.support.mandates import scope_changes
        from tests.test_procedures.test_claims_fix import _grant_settle

        _grant_settle(
            instance,
            "test.derive-line",
            ct.predicate,
            changes=scope_changes(
                "create", basis=("extracted", "derived") if listed else ("extracted",)
            ),
        )
    before = instance.accepted_coordinate()
    state = run(instance, root, line="test.derive-line", minute=1)
    if listed:
        # A derived value settles only under a scope that lists derived (§6.4).
        assert state.terminal_egress[0].settle_outcome == "settled"
        assert instance.accepted_coordinate() != before
    elif settle:
        assert state.terminal_egress[0].settle_outcome == "proposed"
        (gap,) = state.terminal_egress[0].outside_scope
        assert (gap.claim_type, gap.basis, gap.term) == (
            f"ClaimType:{ct.predicate}",
            "derived",
            "basis",
        )
        assert instance.accepted_coordinate() == before
    (derived,) = candidate_claims(instance, state)
    receipt = derived.backing.procedure_evidence.value
    assert receipt.basis == "derived"
    assert receipt.inputs[0].target == basis.identity
    assert receipt.inputs[0].artifact_digest == claim_artifact_digest(basis).tagged
    assert derived.backing.input_claim_digests == (claim_artifact_digest(basis).tagged,)


def test_slot_contested_is_excluded_before_any_proposal(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (prior,) = candidate_claims(instance, first)
    activate(instance, first)
    sibling = prior.model_copy(
        update={"identity": ArtifactIdentity(kind="Claim", name="CLM-" + "f" * 32)}
    )
    from cruxible_core.procedures.claims_context import AcceptedClaimsContext

    monkeypatch.setattr(AcceptedClaimsContext, "slot", lambda *args: (prior, sibling))
    before = instance.accepted_coordinate()
    state = run(instance, root, minute=1)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].proposal_id is None
    assert state.terminal_egress[0].claims_diagnostics[0].kind == "slot_contested"
    assert instance.accepted_coordinate() == before


def test_evidence_refresh_obeys_the_same_replaces_policy(tmp_path: Path) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (prior,) = candidate_claims(instance, first)
    activate(instance, first)
    # The value is the same, but the source bytes changed: an evidence refresh.
    (root / "data/doc.json").write_text(
        json.dumps({"entries": [{"id": "CVE-2026-0001", "status": "open", "note": "new context"}]})
    )
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    assert second.terminal_egress[0].proposal_id is None
    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore

    assert len(ClaimReobservationStore(instance).history(prior.identity.name)) == 1
    # An explicit empty replaces list cannot refresh even an observation.
    spec = procedure_spec(
        base=claims_spec(
            mappings=[{"type": PREDICATE, "value": "$item.fields.status", "replaces": []}]
        ).model_dump(mode="python", by_alias=True),
        changes={"procedure": "test.protected-refresh"},
    )
    install(instance, spec, line_name="test.protected-refresh-line")
    state = run(instance, root, line="test.protected-refresh-line", minute=2)
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress[0].proposal_id is None
    assert not state.terminal_egress[0].claims_diagnostics
    assert state.terminal_egress[0].claims_same_count == 1


def test_subject_valued_template_resolves_the_objects_declared_key(tmp_path: Path) -> None:
    related = claim_type(KIND + ".related", f"subject<{KIND}>", value_type=(KIND,))
    instance, root = claims_world(
        tmp_path,
        {
            "entries": [
                {"id": "CVE-2026-0001", "status": "open"},
                {"id": "CVE-2026-0002", "status": "closed"},
            ]
        },
        types=(related,),
    )
    spec = claims_spec(
        mappings=[
            {"type": related.predicate, "value": {"kind": KIND, "key": {"cve_id": "CVE-2026-0002"}}}
        ]
    )
    install(instance, spec)
    claims = candidate_claims(instance, run(instance, root))
    assert len(claims) == 2
    assert all(
        c.statement.object.kind == "subject"
        and c.statement.object.address.artifact_path.endswith("cve-2026-0002.json")
        for c in claims
    )


def test_publication_refuses_when_cited_capture_bytes_are_no_longer_retained(
    tmp_path: Path,
) -> None:
    import pytest

    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_client.contracts.errors import SettlementIntegrityError
    from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
    from cruxible_core.proposals.proposals import AuthenticatedActor

    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(instance, claims_spec())
    state = run(instance, root)
    (claim,) = candidate_claims(instance, state)
    egress = state.terminal_egress[0]
    evidence = instance.proposal_evidence()
    evaluation = evidence.read_evaluation(egress.proposal_id)
    tree = instance.proposal_tree(evaluation.evaluated_tree_oid, proposal_id=egress.proposal_id)
    body = parse_capture_envelope(
        instance.body_store().read(
            claim.backing.capture_digests[0],
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    ).commitment.digest
    before = instance.accepted_coordinate()
    assert instance.body_store().erase(body)
    preview = instance.proposal_service().preview(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/erased-check", proposed_base_oid=before.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-29T12:30:00.000000Z",
    )
    assert preview.candidate is None
    assert any(
        d.code == "cruxible.claim.evidence_unavailable" for d in preview.evaluation.diagnostics
    )
    with pytest.raises(SettlementIntegrityError):
        activate(instance, state)
    assert instance.accepted_coordinate() == before


def test_within_projects_watched_subject_ids_and_rationale_reads_the_same_row(
    tmp_path: Path,
) -> None:
    from tests.test_pilot.support import kev_spec, pilot_world

    base = kev_spec().model_dump(mode="python", by_alias=True)
    base["steps"][-1]["subject"]["within"]["path"] = ["rows", "[*]", "subject_id"]
    base["steps"][-1]["claims"][0]["rationale"] = (
        "Catalog {$steps.entries.header.catalog_version} lists {$item.key.cve_id}."
    )
    instance, root = pilot_world(tmp_path, "kev.json", spec=procedure_spec(base=base))
    state = run(instance, root)
    claims = candidate_claims(instance, state)
    assert len(claims) == 8
    evidence = instance.proposal_evidence()
    admission = evidence.read_admission(state.terminal_egress[0].proposal_id)
    operation = evidence.item_reason_operation(admission)
    reason = evidence.read_item_reason(
        operation,
        claim_path(
            next(c.identity.name for c in claims if c.statement.predicate.endswith(".kev_listed"))
        ),
    )
    assert "2026.10.29" in reason.reason
    assert "CVE-2026-" in reason.reason


def test_inline_contract_roundtrip_preserves_negative_safety_metadata() -> None:
    spec = procedure_spec(
        "test.requires_complete_input-shape",
        output={
            "status": {
                "type": "enum[closed, unknown]",
                "requires_complete_input": ["closed"],
                "incomplete_fallback": "unknown",
            }
        },
    )
    wire = spec.model_dump(mode="json", by_alias=True)
    again = procedure_spec(base=wire)
    assert again.output["status"].requires_complete_input
    assert again.output["status"].incomplete_fallback == "unknown"


def test_claims_terminal_infers_empty_output_and_honors_explicit_summary(tmp_path: Path) -> None:
    for summary in (False, True):
        base = claims_spec().model_dump(mode="python", by_alias=True)
        base.pop("output")
        if summary:
            base["returns"] = {"fields": {"message": "Claims reviewed."}}
        case_root = tmp_path / str(summary)
        case_root.mkdir()
        instance, root = claims_world(
            case_root, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
        )
        install(instance, procedure_spec(base=base))
        state = run(instance, root)
        assert len(candidate_claims(instance, state)) == 1
        assert state.result == ({"message": "Claims reviewed."} if summary else {})
