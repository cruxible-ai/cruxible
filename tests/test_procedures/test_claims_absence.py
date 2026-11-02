"""Complete-source absence is bounded by coverage, precedence and witnessed authority."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.claim_types import ClaimInverse
from tests.support.procedures import procedure_spec
from tests.test_pilot.support import KEV, KIND, kev_spec, pilot_world
from tests.test_procedures.test_claims_terminal import (
    activate,
    candidate_claims,
    claim_type,
    install,
    run,
)


def absence_spec(policy="mirror", *, may_truncate=False):
    raw = kev_spec(may_truncate=may_truncate).model_dump(mode="python", by_alias=True)
    node = raw["steps"][-1]
    node["expect"] = {"$ref": "step", "step": "watched", "path": ["rows"]}
    node["claims"][0]["when_absent"] = policy
    return procedure_spec(base=raw)


def proposed_claims(instance, state):
    if not state.terminal_egress or state.terminal_egress[0].proposal_id is None:
        return []
    return candidate_claims(instance, state)


def delist(root):
    path = root / "data/kev.json"
    document = json.loads(path.read_bytes())
    document["vulnerabilities"] = []
    document["count"] = 0
    path.write_text(json.dumps(document))


@pytest.mark.parametrize("policy", ("keep", "surface", "mirror"))
def test_policies_respect_complete_covered_absence(tmp_path: Path, policy):
    instance, root = pilot_world(tmp_path, "kev.json", spec=absence_spec(policy))
    first = run(instance, root)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    activate(instance, first)
    delist(root)
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    changed = proposed_claims(instance, second)
    if policy == "mirror":
        assert len(changed) == 2
        for claim in changed:
            assert claim.statement.predicate == KEV + "listed"
            assert claim.lifecycle.state == "retired"
            assert claim.retirement.reason == "superseded"
            witness = claim.retirement.absence
            assert witness is not None and witness.complete_when
            assert witness.logical_source == "cisa.kev.catalog"
            assert witness.expect_coordinate == second.bound_coordinate
    else:
        assert not changed
    findings = [f for f in second.findings if f.claim_type == KEV + "listed"]
    assert len(findings) == (2 if policy == "surface" else 0)
    assert all(f.kind == "claim_not_in_source" and f.reason == "record_absent" for f in findings)


@pytest.mark.parametrize("fault", ("count", "may_truncate"))
def test_incomplete_inputs_surface_and_report_completeness(tmp_path: Path, fault):
    instance, root = pilot_world(
        tmp_path, "kev.json", spec=absence_spec("mirror", may_truncate=fault == "may_truncate")
    )
    first = run(instance, root)
    activate(instance, first)
    delist(root)
    if fault == "count":
        path = root / "data/kev.json"
        doc = json.loads(path.read_bytes())
        doc["count"] = 1
        path.write_text(json.dumps(doc))
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    assert not proposed_claims(instance, second)
    assert second.completeness is not None
    assert not second.completeness.complete and second.completeness.skipped == 2
    assert any(
        r.kind == ("may_truncate" if fault == "may_truncate" else "count_mismatch")
        for r in second.completeness.reasons
    )
    findings = [
        f
        for f in second.findings
        if f.kind == "claim_not_in_source" and f.claim_type == KEV + "listed"
    ]
    assert len(findings) == 2 and all(f.source_incomplete for f in findings)


def test_absence_requires_expect():
    raw = absence_spec().model_dump(mode="python", by_alias=True)
    raw["steps"][-1].pop("expect")
    with pytest.raises(ValidationError, match="claims_absence_invalid"):
        procedure_spec(base=raw)


@pytest.mark.parametrize("retired", ("enumeration", "covers"))
def test_the_enumeration_is_never_authored_and_covers_is_gone(retired):
    raw = absence_spec().model_dump(mode="python", by_alias=True)
    raw["steps"][-1][retired] = "$steps.entries"
    with pytest.raises(ValidationError, match=retired):
        procedure_spec(base=raw)


def test_absence_without_expect_is_refused():
    raw = kev_spec().model_dump(mode="python", by_alias=True)
    raw["steps"][-1]["claims"][0]["when_absent"] = "surface"
    with pytest.raises(ValidationError, match="claims_absence_invalid"):
        procedure_spec(base=raw)


def test_inverse_requires_a_declared_inverse_at_authoring(tmp_path: Path):
    instance, root = pilot_world(tmp_path, "kev.json")
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    with pytest.raises(AuthoringLoweringError, match="claims_inverse_missing"):
        install(instance, absence_spec("inverse"), line_name="inverse-missing")


def inverse_world(tmp_path, *, incomplete=False):
    from tests.test_pilot.support import FIXTURES, KEV_ROW, _watched
    from tests.test_procedures.test_claims_terminal import claims_world

    original = json.loads((FIXTURES / "kev.json").read_bytes())
    inverse = ClaimInverse(key={"kind": "slot"}, evidence_rules=("fixture",))
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": row["cveID"]} for row in original["vulnerabilities"]]},
        types=(
            claim_type(KEV + "listed", "bool", inverse=inverse),
            claim_type(KEV + "date_added", "date"),
            claim_type(KEV + "due_date", "date"),
            claim_type(KEV + "ransomware_use", "enum[Known, Unknown]"),
        ),
        rows=KEV_ROW,
    )
    _watched(instance)
    path = root / "data/kev.json"
    path.write_text(
        json.dumps({**original, "vulnerabilities": [], "count": 1 if incomplete else 0})
    )
    install(
        instance,
        absence_spec("inverse"),
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
    )
    return instance, root, original


@pytest.mark.parametrize("protection", ("precedence", "other_source"))
def test_mirror_surfaces_a_protected_holder(tmp_path: Path, protection):
    spec = absence_spec()
    raw = spec.model_dump(mode="python", by_alias=True)
    if protection == "precedence":
        raw["steps"][-1]["claims"][0]["role"] = "normative"
    instance, root = pilot_world(tmp_path, "kev.json", spec=procedure_spec(base=raw))
    first = run(instance, root)
    activate(instance, first)
    # A separate governed Line uses observation authority and optionally another source.
    raw["procedure"] = "security.absence-observer"
    raw["steps"][-1]["claims"][0]["role"] = "observation"
    install(
        instance,
        procedure_spec(base=raw),
        line_name="absence-observer",
        parameters={
            "logical_source": "other.catalog"
            if protection == "other_source"
            else "cisa.kev.catalog",
            "relative_path": "data/kev.json",
        },
    )
    delist(root)
    second = run(instance, root, minute=1, line="absence-observer")
    assert second.status == "succeeded", second.terminal
    assert not proposed_claims(instance, second)
    findings = [f for f in second.findings if f.claim_type == KEV + "listed"]
    assert len(findings) == 2 and all(
        f.kind == "claim_not_in_source" and not f.source_incomplete for f in findings
    )


@pytest.mark.parametrize(
    "case", ("field_absent", "invalid_field", "duplicate", "when_false", "when_present")
)
def test_only_true_field_absence_mirrors(tmp_path: Path, case):
    raw = absence_spec().model_dump(mode="python", by_alias=True)
    raw["steps"][-1]["claims"][0]["when_absent"] = "keep"
    raw["steps"][-1]["claims"][1]["when_absent"] = "mirror"
    instance, root = pilot_world(tmp_path, "kev.json", spec=procedure_spec(base=raw))
    first = run(instance, root)
    activate(instance, first)
    if case in {"when_false", "when_present"}:
        raw["procedure"] = "security.false-absence"
        raw["steps"][-1]["claims"][1]["when"] = {
            "op": "eq",
            "left": {"literal": 0},
            "right": {"literal": 1},
        }
        if case == "when_present":
            raw["steps"][-1]["claims"][1]["when"] = {
                "op": "present",
                "of": {"$ref": "item", "path": ["fields", "date_added"]},
            }
        install(
            instance,
            procedure_spec(base=raw),
            line_name="false-absence",
            parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
        )
    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    if case == "duplicate":
        doc["vulnerabilities"].append(doc["vulnerabilities"][0].copy())
        doc["count"] += 1
    elif case == "invalid_field":
        doc["vulnerabilities"][0]["dateAdded"] = "not-a-date"
    else:
        doc["vulnerabilities"][0].pop("dateAdded")
    path.write_text(json.dumps(doc))
    second = run(
        instance,
        root,
        minute=1,
        line="false-absence" if case in {"when_false", "when_present"} else "test.claims-line",
    )
    assert second.status == "succeeded", second.terminal
    changed = proposed_claims(instance, second)
    if case == "field_absent":
        assert len(changed) == 1 and changed[0].lifecycle.state == "retired"
        assert changed[0].statement.predicate == KEV + "date_added"
    else:
        assert not changed
        assert not any(
            f.kind == "claim_not_in_source" and f.claim_type == KEV + "date_added"
            for f in second.findings
        )


def test_line_routes_absence_findings_once_by_claim(tmp_path: Path):
    instance, root = pilot_world(tmp_path, "kev.json", spec=absence_spec("surface"))
    first = run(instance, root)
    activate(instance, first)
    raw = absence_spec("surface").model_dump(mode="python", by_alias=True)
    raw["procedure"] = "security.absence-inbox"
    install(
        instance,
        procedure_spec(base=raw),
        line_name="absence-inbox",
        findings=("claim_not_in_source",),
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
    )
    delist(root)
    second = run(instance, root, minute=1, line="absence-inbox")
    assert second.status == "succeeded", second.terminal
    from cruxible_core.exhaust.line_inbox import LineInboxStore

    store = LineInboxStore(instance)
    before, _ = store.page(line=None, status=None, limit=50, cursor=None)
    assert len(before) == 8
    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["catalogVersion"] = "another-capture"
    path.write_text(json.dumps(doc))
    again = run(instance, root, minute=2, line="absence-inbox")
    assert again.status == "succeeded", again.terminal
    after, _ = store.page(line=None, status=None, limit=50, cursor=None)
    assert {r.envelope.item_id for r in after} == {r.envelope.item_id for r in before}


def test_run_card_and_activity_show_the_same_completeness(tmp_path: Path):
    instance, root, _ = inverse_world(tmp_path, incomplete=True)
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.discovery.orient import service_playbill_orient
    from cruxible_core.storage.cas import BodyAccessContext

    card = service_playbill_get(
        instance,
        request=GetRequest(ref=f"ProcedureRun:{state.run_id}"),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    ).card
    assert card.completeness == state.completeness
    activity = service_playbill_orient(instance, section="activity", limit=100).activity
    finished = next(
        row for row in activity if row.event == "run_finished" and row.run == state.run_id
    )
    assert finished.completeness == state.completeness


@pytest.mark.parametrize("variant", ("qualifiers", "many", "mirror_with_inverse"))
def test_absence_tracks_each_qualifier_and_each_many_valued_unit(tmp_path: Path, variant):
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.subjects import subject_path
    from tests.test_procedures import test_procedure_proposal_delivery as delivery
    from tests.test_procedures import test_procedure_settlement_delivery as settle
    from tests.test_procedures import test_procedure_source_runs as sources

    instance, root, line = settle.settle_world(
        tmp_path,
        claims_terminal=True,
        claims_fixed_subject=True,
        claims_absence="mirror",
        claims_many=variant != "qualifiers",
        claims_declares_inverse=variant == "mirror_with_inverse",
        claims_within=(subject_path(delivery.SUBJECT_KIND, delivery.SUBJECT_ID),),
        change_kinds=("create", "revise", "retire"),
    )
    path = root / sources.RELATIVE_PATH
    path.write_bytes(
        canonical_bytes(
            {
                "proposals": [{"id": "a", "severity": "high"}, {"id": "b", "severity": "low"}],
                "count": 2,
            }
        )
    )
    first = settle.run_settle(instance, root, line)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    assert first.terminal_egress[0].settle_outcome == "settled"
    path.write_bytes(canonical_bytes({"proposals": [{"id": "a", "severity": "high"}], "count": 1}))
    from datetime import timedelta

    second = settle.run_settle(instance, root, line, now=sources.NOW + timedelta(hours=1))
    assert second.status == "succeeded", second.terminal
    assert second.terminal_egress[0].settle_outcome == "proposed"
    assert second.terminal_egress[0].outside_scope_claim_types == (delivery.PREDICATE,)
    changes = proposed_claims(instance, second)
    retired = [c for c in changes if c.lifecycle.state == "retired"]
    assert len(retired) == 1 and retired[0].statement.object.value == "low"
    assert retired[0].retirement.absence is not None
    if variant == "qualifiers":
        assert retired[0].statement.qualifier == "b"
    if variant == "mirror_with_inverse":
        inverse = [c for c in changes if c.statement.object.kind == "none"]
        assert len(inverse) == 1 and inverse[0].statement.object.of.value == "low"


def test_expect_must_be_a_subset_of_only_keys_before_any_write(tmp_path: Path):
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.subjects import subject_path
    from tests.test_procedures import test_procedure_proposal_delivery as delivery
    from tests.test_procedures import test_procedure_settlement_delivery as settle
    from tests.test_procedures import test_procedure_source_runs as sources
    from tests.test_scale.test_settle_per_item import _create_subjects

    instance, root, line = settle.settle_world(
        tmp_path,
        claims_terminal=True,
        claims_absence="mirror",
        claims_within=tuple(subject_path(delivery.SUBJECT_KIND, f"adv-{i:05d}") for i in range(2)),
        claims_only_keys=[{"slug": "adv-00000"}],
    )
    _create_subjects(instance, settle.OWNERS[instance.root], 2)
    before = instance.accepted_coordinate()
    (root / sources.RELATIVE_PATH).write_bytes(
        canonical_bytes({"proposals": [{"id": "adv-00000", "severity": "high"}]})
    )
    state = settle.run_settle(instance, root, line)
    assert state.status == "node_refused"
    assert state.terminal.code == "claims_expect_outside_only_keys"
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize("policy", ("mirror", "inverse"))
def test_retained_absence_refuses_a_changed_holder(tmp_path: Path, policy):
    from cruxible_client.contracts.claims import claim_path, render_claim
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.procedures.claims_context import reselect_retained_claims
    from cruxible_core.procedures.egress import TerminalEgressRequestV2
    from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused
    from tests.core_support._identity_support import accept_tree
    from tests.test_procedures.test_procedure_settlement_delivery import _egress_records
    from tests.test_procedures.test_procedure_source_runs import _admission

    if policy == "inverse":
        instance, root, original = inverse_world(tmp_path)
        (root / "data/kev.json").write_text(json.dumps(original))
    else:
        instance, root = pilot_world(tmp_path, "kev.json", spec=absence_spec("mirror"))
    first = run(instance, root)
    activate(instance, first)
    delist(root)
    state = run(instance, root, minute=1)
    assert state.status == "succeeded", state.terminal
    payload = next(p for p in _egress_records(instance, state) if p.get("verdict") == "prepared")
    request = TerminalEgressRequestV2.model_validate(payload["request"])
    admission = _admission(instance, state)
    # The exact original slots remain eligible at the original head.
    reselect_retained_claims(instance, request, admission)
    claim = next(
        c for c in candidate_claims(instance, first) if c.statement.predicate == KEV + "listed"
    )
    current = claim.model_copy(
        update={"statement": claim.statement.model_copy(update={"role": "normative"})}
    )
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claims import claim_artifact_digest

    current = current.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(claim).tagged)
        }
    )
    accept_tree(
        instance, {claim_path(claim.identity.name): render_claim(current)}, name="protect-held"
    )
    moved = request.model_copy(
        update={
            "accepted_coordinate": AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        }
    )
    with pytest.raises(ProposalDeliveryRefused, match="changed|higher-precedence"):
        reselect_retained_claims(instance, moved, admission)


def test_mirror_publication_requires_retained_witness_bytes(tmp_path: Path, monkeypatch):
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    instance, root = pilot_world(tmp_path, "kev.json", spec=absence_spec())
    first = run(instance, root)
    activate(instance, first)
    delist(root)
    state = run(instance, root, minute=1)
    witness = next(
        c.retirement.absence for c in proposed_claims(instance, state) if c.retirement is not None
    )
    from cruxible_client.contracts.captures import (
        capture_retained_body_digests,
        parse_capture_envelope,
    )
    from cruxible_core.storage.cas import BodyAccessContext

    envelope = parse_capture_envelope(
        instance.body_store().read(
            witness.capture, access=BodyAccessContext(principal_id="test", can_read_body=True)
        )
    )
    retained = set(capture_retained_body_digests(envelope))
    original = ContentAddressedBodyStore.verify
    monkeypatch.setattr(
        ContentAddressedBodyStore,
        "verify",
        lambda store, digest: False if digest in retained else original(store, digest),
    )
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.service.proposals.proposals import _proposal_result

    held = _proposal_result(instance, state.terminal_egress[0].proposal_id)
    result = instance.proposal_service().preview(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/retention-check",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=instance.proposal_tree(
            held.evaluation.evaluated_tree_oid, proposal_id=held.admission.proposal_id
        ),
        timestamp="2026-10-09T12:02:00.000000Z",
    )
    assert result.evaluation.verdict == "refused"
    assert any(
        d.code == "cruxible.claim.evidence_unavailable" for d in result.evaluation.diagnostics
    )


@pytest.mark.parametrize("unit", ("object", "object_key"))
def test_subject_valued_inverse_tracks_its_declared_unit(tmp_path: Path, unit):
    from cruxible_client.contracts.policies import ClaimResolutionPolicy
    from tests.test_pilot.support import FIXTURES, KEV_ROW, _watched
    from tests.test_procedures.test_claims_terminal import claims_world

    original = json.loads((FIXTURES / "kev.json").read_bytes())
    key = {"kind": unit, **({"predicate": KIND + ".cve_id"} if unit == "object_key" else {})}
    predicate = KIND + ".listed_in"
    ct = claim_type(
        predicate,
        f"subject<{KIND}>",
        cardinality="many",
        inverse=ClaimInverse(key=key, evidence_rules=("fixture",)),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": row["cveID"]} for row in original["vulnerabilities"]]},
        types=(ct, claim_type(KEV + "ransomware_use", "enum[Known, Unknown]")),
        rows=KEV_ROW,
    )
    _watched(instance)
    object_key = original["vulnerabilities"][0]["cveID"]
    raw = absence_spec("inverse").model_dump(mode="python", by_alias=True)
    raw["steps"][-1]["claims"] = [
        {
            "type": predicate,
            "value": {"kind": KIND, "key": {"cve_id": object_key}},
            "when_absent": "inverse",
        }
    ]
    install(
        instance,
        procedure_spec(base=raw),
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
    )
    path = root / "data/kev.json"
    path.write_text(json.dumps(original))
    first = run(instance, root)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    positives = candidate_claims(instance, first)
    activate(instance, first)
    delist(root)
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    inverses = proposed_claims(instance, second)
    assert len(inverses) == 2
    assert {c.identity.name for c in inverses} == {c.identity.name for c in positives}
    assert all(c.statement.object.kind == "none" for c in inverses)
    if unit == "object_key":
        assert all(
            c.statement.object.of.kind == "literal" and c.statement.object.of.value == object_key
            for c in inverses
        )
    else:
        assert all(c.statement.object.of.kind == "subject" for c in inverses)
    activate(instance, second)
    path.write_text(json.dumps(original))
    third = run(instance, root, minute=2)
    assert third.status == "succeeded", third.terminal
    restored = proposed_claims(instance, third)
    assert {c.identity.name for c in restored} == {c.identity.name for c in positives}
    assert all(c.statement.object.kind == "subject" for c in restored)
