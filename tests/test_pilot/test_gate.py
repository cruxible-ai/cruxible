"""Pilot stage 1 exercises real served Lines and proposal/activation, on recorded bytes."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.procedures import procedure_spec
from tests.test_pilot.support import EPSS, KEV, KIND, kev_spec, pilot_world
from tests.test_procedures.test_claims_terminal import (
    activate,
    candidate_claims,
    claim_type,
    install,
    run,
)

# Executor-only fixtures: these tests author Procedure step kinds the template
# closes at authoring (D-R37 h1) to cover their executor handlers (h3).
pytestmark = pytest.mark.executor_only


def values(instance, state):  # type: ignore[no-untyped-def]
    return {
        (c.statement.subject.artifact_path, c.statement.predicate): c.statement.object.value
        for c in candidate_claims(instance, state)
    }


@pytest.mark.parametrize("fixture", ["kev.json", "epss.json"])
def test_g1_identical_run_twice_writes_nothing(tmp_path: Path, fixture: str) -> None:
    instance, root = pilot_world(tmp_path, fixture)
    first = run(instance, root)
    claims = candidate_claims(instance, first)
    assert len(claims) == (8 if fixture.startswith("kev") else 6)
    assert all(c.backing.procedure_evidence.law == "claim-evidence-v2" for c in claims)
    activate(instance, first)
    base = instance.accepted_coordinate()
    again = run(instance, root, minute=1)
    assert again.status == "succeeded", again.terminal
    (egress,) = again.terminal_egress
    assert egress.proposal_id is None
    assert all(c.unchanged for c in egress.children)
    assert instance.accepted_coordinate() == base


def test_g6_maybe_is_inadmissible_and_siblings_write(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev-invalid.json")
    state = run(instance, root)
    claims = candidate_claims(instance, state)
    assert len(claims) == 7
    first = [c for c in claims if "cve-2026-0001" in c.statement.subject.artifact_path]
    assert {c.statement.predicate for c in first} == {
        KEV + "listed",
        KEV + "date_added",
        KEV + "due_date",
    }
    (diagnostic,) = state.terminal_egress[0].claims_diagnostics
    assert diagnostic.kind == "extract_refused" and diagnostic.lexeme == "Maybe"
    from cruxible_core.exhaust.line_inbox import LineInboxStore

    store = LineInboxStore(instance)
    page, _ = store.page(line=None, status=None, limit=20, cursor=None)
    assert len(page) == 1
    assert store.get(page[0].envelope.item_id).payload["kind"] == "extract_refused"


def test_g7_duplicate_keys_write_nothing_for_that_key(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev-duplicates.json")
    state = run(instance, root)
    claims = candidate_claims(instance, state)
    assert len(claims) == 4
    assert all("cve-2026-0002" in c.statement.subject.artifact_path for c in claims)


def test_g10_g14_normative_holder_and_date_are_held_by_precedence(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev.json")
    normative = procedure_spec(
        base=kev_spec(role="normative").model_dump(mode="python", by_alias=True),
        changes={"procedure": "security.normative-kev"},
    )
    install(
        instance,
        normative,
        line_name="test.normative-line",
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
    )
    first = run(instance, root, line="test.normative-line")
    activate(instance, first)
    before = instance.accepted_coordinate()
    observed = run(instance, root, minute=1)
    assert observed.status == "succeeded", observed.terminal
    egress = observed.terminal_egress[0]
    assert egress.proposal_id is None
    assert not egress.claims_diagnostics
    assert egress.claims_same_count == 8
    assert instance.accepted_coordinate() == before


def test_g11_epss_na_is_inadmissible_and_percentile_writes(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "epss-invalid.json")
    state = run(instance, root)
    claims = candidate_claims(instance, state)
    assert len(claims) == 5
    first = [c for c in claims if "cve-2026-0001" in c.statement.subject.artifact_path]
    assert {c.statement.predicate for c in first} == {EPSS + "percentile", EPSS + "score_date"}
    (diagnostic,) = state.terminal_egress[0].claims_diagnostics
    assert diagnostic.kind == "extract_refused" and diagnostic.lexeme == "n/a"
    percentile = next(c for c in first if c.statement.predicate == EPSS + "percentile")
    receipt = percentile.backing.procedure_evidence.value
    # The decimal cast is the extract's parse, not a shape (D-R23).
    assert receipt.basis == "extracted" and receipt.transform_node_ids == ()
    assert receipt.field_path.endswith("/percentile")


def test_g18_invalid_status_filter_count_derive_cannot_launder_a_negative(tmp_path: Path) -> None:
    base = kev_spec().model_dump(mode="python", by_alias=True)
    from tests.support.procedures import refs

    base["steps"] = list(base["steps"][:-1]) + refs(
        [
            {
                "transform": "kept",
                "kind": "filter",
                "spec": {
                    "input": "$steps.entries.records",
                    "where": {
                        "op": "eq",
                        "left": {"$ref": "item", "path": ["fields", "ransomware_use"]},
                        "right": {"literal": "Known"},
                    },
                },
            },
            {
                "transform": "count",
                "kind": "aggregate",
                "spec": {"input": "$steps.kept", "op": "count"},
            },
            {
                "transform": "verdict",
                "kind": "derive",
                "spec": {
                    "input": {"open": "$steps.count"},
                    "cases": [
                        {
                            "when": {
                                "op": "eq",
                                "left": {"$ref": "item", "path": ["fields", "open"]},
                                "right": {"literal": 0},
                            },
                            "value": "fixed",
                        }
                    ],
                    "else": "affected",
                },
            },
            {
                "id": "write",
                "for_each": "$steps.entries.records",
                "subject": {"kind": KIND, "key": {"cve_id": "$item.key.cve_id"}},
                "claims": [
                    {
                        "type": KIND + ".closure",
                        "value": "$steps.verdict.result",
                        "role": "derivation",
                    }
                ],
            },
        ]
    )
    spec = procedure_spec(base=base)
    # Add the negative type before authoring the Procedure that names it.
    import json

    from tests.test_pilot.support import FIXTURES, KEV_ROW, _watched
    from tests.test_procedures.test_claims_terminal import claims_world

    document = json.loads((FIXTURES / "kev-invalid.json").read_bytes())
    closure = claim_type(
        KIND + ".closure",
        "enum[affected, fixed, unknown]",
        requires_complete_input=("fixed",),
        incomplete_fallback="unknown",
    )
    from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy

    closure = closure.model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=tuple(
                    rule.model_copy(update={"admission": "derivational"})
                    for rule in closure.evidence_admission_policy.rules
                )
            )
        }
    )
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": r["cveID"]} for r in document["vulnerabilities"]]},
        types=(closure, claim_type(KEV + "ransomware_use", "enum[Known, Unknown]")),
        rows=KEV_ROW,
    )
    (root / "data/kev-invalid.json").write_bytes((FIXTURES / "kev-invalid.json").read_bytes())
    _watched(instance)
    install(
        instance,
        spec,
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev-invalid.json"},
    )
    state = run(instance, root)
    claims = candidate_claims(instance, state)
    assert {c.statement.object.value for c in claims} == {"unknown"}
    for claim in claims:
        receipt = claim.backing.procedure_evidence.value
        assert receipt.basis == "derived"
        assert receipt.transform_node_ids == ("kept", "count", "verdict")
        assert len(receipt.extractions) == 1
        assert receipt.extractions[0].capture_digest in claim.backing.capture_digests

    assert {d.kind for d in state.terminal_egress[0].claims_diagnostics} == {"incomplete_source"}
    assert all(d.input == "verdict" for d in state.terminal_egress[0].claims_diagnostics)


def test_g20_epss_header_datetime_is_projected_to_utc_date(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "epss.json")
    state = run(instance, root)
    dates = [
        c for c in candidate_claims(instance, state) if c.statement.predicate == EPSS + "score_date"
    ]
    assert len(dates) == 2
    assert {c.statement.object.value for c in dates} == {"2026-10-30"}
    assert all(c.backing.procedure_evidence.value.field_path == "/created" for c in dates)


def test_g4_count_mismatch_never_mirrors(tmp_path: Path) -> None:
    import json

    from tests.test_procedures.test_claims_absence import absence_spec, proposed_claims

    instance, root = pilot_world(tmp_path, "kev.json", spec=absence_spec("mirror"))
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)
    before = instance.accepted_coordinate()
    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["vulnerabilities"] = []
    doc["count"] = 1
    path.write_text(json.dumps(doc))
    state = run(instance, root, minute=1)
    assert state.status == "succeeded", state.terminal
    assert not proposed_claims(instance, state)
    assert instance.accepted_coordinate() == before
    findings = [
        f
        for f in state.findings
        if f.kind == "claim_not_in_source" and f.claim_type == KEV + "listed"
    ]
    assert len(findings) == 2 and all(f.source_incomplete for f in findings)
    assert state.completeness is not None and state.completeness.skipped == 2
    assert any(r.kind == "count_mismatch" for r in state.completeness.reasons)


def kev_mirror_spec():  # type: ignore[no-untyped-def]
    """§11.8: listed and both dates mirror; no present() guards (D-R20)."""
    from tests.test_procedures.test_claims_absence import absence_spec

    base = absence_spec("mirror").model_dump(mode="python", by_alias=True)
    for mapping in base["steps"][-1]["claims"][:3]:
        mapping["when_absent"] = "mirror"
        assert "when" not in mapping
    return procedure_spec(base=base)


def test_g5_a_vanished_date_field_mirrors_while_the_record_stays_listed(tmp_path: Path) -> None:
    import json

    from tests.test_procedures.test_claims_absence import proposed_claims

    instance, root = pilot_world(tmp_path, "kev.json", spec=kev_mirror_spec())
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)
    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    vanished = doc["vulnerabilities"][0]
    vanished.pop("dueDate")
    path.write_text(json.dumps(doc))
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    (retired,) = proposed_claims(instance, second)
    assert retired.lifecycle.state == "retired"
    assert retired.statement.predicate == KEV + "due_date"
    assert vanished["cveID"].lower() in retired.statement.subject.artifact_path
    assert retired.retirement.absence is not None
    assert not any(f.kind == "claim_not_in_source" for f in second.findings)


def test_g5_delisting_has_a_mirror_witness(tmp_path: Path) -> None:
    import json

    from cruxible_client.contracts.canonical import Sha256Value, typed_digest
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.extract import EXTRACTOR_VERSIONS
    from cruxible_core.procedures.claims_terminal import _receipt_extract_digest
    from tests.test_procedures.test_procedure_source_runs import _admission

    instance, root = pilot_world(tmp_path, "kev.json", spec=kev_mirror_spec())
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)
    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["vulnerabilities"] = []
    doc["count"] = 0
    path.write_text(json.dumps(doc))
    second = run(instance, root, minute=1)
    retired = candidate_claims(instance, second)
    assert len(retired) == 6 and all(c.lifecycle.state == "retired" for c in retired)
    assert {c.statement.predicate for c in retired} == {
        KEV + field for field in ("listed", "date_added", "due_date")
    }
    admitted = _admission(instance, second)
    procedure = parse_procedure(
        instance.blob_at(admitted.bound_coordinate.git_oid, admitted.procedure_path),
        path=admitted.procedure_path,
    )
    extracting = next(n for n in procedure.definition.nodes if n.kind == "extract")
    for claim in retired:
        witness = claim.retirement.absence
        assert witness is not None
        assert witness.run_id == second.run_id
        assert witness.procedure_digest == admitted.procedure_artifact_digest
        assert witness.node_id == "write"
        assert witness.capture == second.source_observations[0].capture_digest
        assert witness.extract_spec == _receipt_extract_digest(extracting)
        assert witness.extractor == EXTRACTOR_VERSIONS["json"]
        assert witness.logical_source == "cisa.kev.catalog"
        assert witness.complete_when.model_dump() == {
            "count_at": "/count",
            "expected": 0,
            "observed": 0,
            "held": True,
        }
        assert (
            witness.enumerated_key_digest
            == typed_digest(Sha256Value, "cruxible-enumerated-keys-v1", {"keys": []}).tagged
        )
        assert witness.expect_coordinate == second.bound_coordinate


def _delist(root: Path) -> None:
    import json

    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["vulnerabilities"] = []
    doc["count"] = 0
    path.write_text(json.dumps(doc))


@pytest.mark.parametrize("retire_listed", (True, False), ids=("settled", "proposed"))
def test_g5_delisting_settles_or_proposes_per_mandate_scope_all_or_nothing(
    tmp_path: Path, retire_listed: bool
) -> None:
    """§8.1 grant: creates settle on extracted, mirror retirements on absence.

    Without ``retire`` for ``kev_listed`` the whole delisting change set (all six
    retirements, the dates included) proposes, naming that one gap.
    """

    from tests.test_pilot.support import KEV_SETTLE_SCOPE, grant_kev_settle

    instance, root = pilot_world(tmp_path, "kev.json", spec=kev_mirror_spec(), authority="settle")
    scope = dict(KEV_SETTLE_SCOPE)
    if not retire_listed:
        scope["listed"] = {"create": {"basis": ["extracted"]}}
    grant_kev_settle(instance, scope=scope)
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    assert first.terminal_egress[0].settle_outcome == "settled"
    assert first.terminal_egress[0].outside_scope == ()
    settled = instance.accepted_coordinate()
    _delist(root)
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    (egress,) = second.terminal_egress
    retired = candidate_claims(instance, second)
    assert len(retired) == 6 and all(c.lifecycle.state == "retired" for c in retired)
    assert all(c.retirement.absence is not None for c in retired)
    if retire_listed:
        assert egress.settle_outcome == "settled" and egress.outside_scope == ()
        assert instance.accepted_coordinate() != settled
        return
    assert egress.settle_outcome == "proposed"
    assert egress.fallback_reason == "cruxible.settle.outside_mandate_scope"
    assert len(egress.target_paths) == 6  # all or nothing: the in-scope dates propose too
    (gap,) = egress.outside_scope
    assert (gap.claim_type, gap.change, gap.basis, gap.term) == (
        f"ClaimType:{KEV}listed",
        "retire",
        "absence",
        "change_kind",
    )
    assert instance.accepted_coordinate() == settled


@pytest.mark.parametrize("to", (("Known",), ("Known", "Unknown")))
def test_value_condition_on_an_enum_type_settles_only_listed_moves(
    tmp_path: Path, to: tuple[str, ...]
) -> None:
    from tests.test_pilot.support import grant_kev_settle

    instance, root = pilot_world(tmp_path, "kev.json", authority="settle")
    grant_kev_settle(instance, values={"ransomware_use": {"to": list(to)}})
    before = instance.accepted_coordinate()
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    if len(to) == 2:
        assert egress.settle_outcome == "settled"
        return
    # CVE-2026-0002 writes Unknown: outside value.to, so the whole run proposes.
    assert egress.settle_outcome == "proposed"
    (gap,) = egress.outside_scope
    assert (gap.claim_type, gap.change, gap.term) == (
        f"ClaimType:{KEV}ransomware_use",
        "create",
        "value",
    )
    assert len(candidate_claims(instance, state)) == 8
    assert instance.accepted_coordinate() == before


def test_g6_an_inadmissible_value_is_excluded_before_the_settle_decision(
    tmp_path: Path,
) -> None:
    """ "Maybe" never reaches the change set, so its siblings settle under the grant."""

    from tests.test_pilot.support import grant_kev_settle

    instance, root = pilot_world(tmp_path, "kev-invalid.json", authority="settle")
    grant_kev_settle(instance, values={"ransomware_use": {"to": ["Known", "Unknown"]}})
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled" and egress.outside_scope == ()
    assert len(candidate_claims(instance, state)) == 7
    (diagnostic,) = egress.claims_diagnostics
    assert diagnostic.kind == "extract_refused" and diagnostic.lexeme == "Maybe"


def test_g9_a_new_build_changes_nothing_until_provider_move_then_proposes(
    tmp_path: Path,
) -> None:
    """C2/C3: a Line pins its build; ``provider move`` makes its Mandate propose-only."""

    from cruxible_client.contracts.provider_installation import ProviderMoveRequest
    from cruxible_client.contracts.providers import provider_digest
    from cruxible_core.service.procedures.provider_builds import service_move_provider
    from tests.support.mandates import scope_changes
    from tests.support.operation_world import operation_build
    from tests.test_procedures.test_b2_operations import install as install_builds
    from tests.test_procedures.test_claims_fix import _grant_settle, measured_line, run_measured

    instance, root, score = measured_line(tmp_path)
    grant = _grant_settle(
        instance,
        "test.claims-line",
        score.predicate,
        changes=scope_changes("create", "revise", basis=("measured",)),
    )
    first = run_measured(instance, root, value=1)
    assert first.terminal_egress[0].settle_outcome == "settled"

    def accepted(path: str) -> bytes:
        return instance.tree_at(instance.accepted_coordinate().git_oid)[path]

    line_path = "lines/test.claims-line.json"
    mandate_path = "procedure-mandates/test.claims-line-settle.json"
    line_bytes, mandate_bytes = accepted(line_path), accepted(mandate_path)
    newer = operation_build(build="d")
    install_builds(instance, newer)
    # A new build changes nothing: the Line keeps its binding, the grant keeps settling.
    assert (accepted(line_path), accepted(mandate_path)) == (line_bytes, mandate_bytes)
    again = run_measured(instance, root, minute=2, value=2)
    assert again.terminal_egress[0].settle_outcome == "settled"
    assert again.terminal_egress[0].procedure_mandate_digest == grant
    measured = candidate_claims(instance, again)[0].backing.procedure_evidence.value
    assert measured.build.artifact_digest != provider_digest(newer).tagged

    moved = service_move_provider(
        instance,
        ProviderMoveRequest(provider="demo-provider", to=newer.identity.name, dry_run=False),
        actor_id="owner",
        timestamp="2026-10-29T11:30:00.000000Z",
    )
    assert moved.status == "accepted", moved.detail
    # Both of the Line's grants (its propose grant and the settle grant) carry propose-only.
    carries = {item.identity: item for item in moved.automation_carries}
    assert all(item.kind == "mandate" and item.new_grant == "propose" for item in carries.values())
    assert carries["ProcedureMandate:test.claims-line-settle"].loses_settle
    before = instance.accepted_coordinate()
    after = run_measured(instance, root, minute=3, value=3)
    assert after.status == "succeeded", after.terminal
    (egress,) = after.terminal_egress
    # The successor Line's Mandate is propose-only: the change proposes, nothing settles.
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "proposed")
    assert egress.capped_by == "mandate_grant"
    assert egress.proposal_id is not None
    assert instance.accepted_coordinate() == before
    measured = candidate_claims(instance, after)[0].backing.procedure_evidence.value
    assert measured.build.artifact_digest == provider_digest(newer).tagged


def test_g8_watched_read_ceiling_refuses_before_writes(tmp_path: Path) -> None:
    from tests.support.procedures import procedure_limits

    spec = procedure_spec(
        base=kev_spec().model_dump(mode="python", by_alias=True),
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=65536, read_rows=1),
    )
    instance, root = pilot_world(tmp_path, "kev.json", spec=spec)
    before = instance.accepted_coordinate()
    state = run(instance, root)
    assert state.status == "admission_refused"
    assert state.terminal.code == "read_ceiling_exceeded"
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize("incomplete", (False, True))
def test_pilot_inverse_complete_catalog_or_surface_only(tmp_path: Path, incomplete):
    import json

    from tests.test_procedures.test_claims_absence import inverse_world, proposed_claims

    instance, root, original = inverse_world(tmp_path, incomplete=incomplete)
    first = run(instance, root)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    claims = proposed_claims(instance, first)
    if incomplete:
        assert not claims
        assert first.completeness is not None
        assert any(f.source_incomplete for f in first.findings)
        return
    assert len(claims) == 2
    assert all(
        c.statement.object.kind == "none" and c.backing.procedure_evidence.absence is not None
        for c in claims
    )
    activate(instance, first)
    before = instance.accepted_coordinate()
    # Different bytes, re-confirmed absence: no Claim revision or proposal.
    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["catalogVersion"] = "next"
    path.write_text(json.dumps(doc))
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    assert second.terminal_egress[0].proposal_id is None
    assert instance.accepted_coordinate() == before
    assert all(child.unchanged for child in second.terminal_egress[0].children)
    path.write_text(json.dumps(original))
    positive = run(instance, root, minute=2)
    assert positive.status == "succeeded", positive.terminal
    listed = [
        c for c in candidate_claims(instance, positive) if c.statement.predicate == KEV + "listed"
    ]
    assert len(listed) == 2 and all(
        c.statement.object.kind == "literal" and c.statement.object.value is True for c in listed
    )
    assert {c.identity.name for c in listed} == {c.identity.name for c in claims}
