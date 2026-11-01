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
    assert receipt.basis == "extracted" and receipt.transform_node_ids == ("typed",)
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


@pytest.mark.xfail(
    strict=True,
    reason="needs B4b: count mismatch must degrade absence to surface with source_incomplete",
)
def test_g4_count_mismatch_never_mirrors(tmp_path: Path) -> None:
    base = kev_spec().model_dump(mode="python", by_alias=True)
    base["steps"][-1]["covers"] = {"$ref": "step", "step": "watched", "path": ["rows"]}
    base["steps"][-1]["enumeration"] = {"$ref": "step", "step": "entries", "path": []}
    base["steps"][-1]["claims"][0]["when_absent"] = "mirror"
    spec = procedure_spec(base=base)
    instance, root = pilot_world(tmp_path, "kev.json", spec=spec)
    import json

    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["count"] += 1
    path.write_text(json.dumps(doc))
    state = run(instance, root)
    assert state.status == "succeeded"
    assert state.terminal_egress[0].claims_diagnostics[0].problem == "source_incomplete"


@pytest.mark.xfail(strict=True, reason="needs B4b: delisting retires with an absence witness")
def test_g5_delisting_has_a_mirror_witness(tmp_path: Path) -> None:
    base = kev_spec().model_dump(mode="python", by_alias=True)
    base["steps"][-1]["claims"][0]["when_absent"] = "mirror"
    spec = procedure_spec(base=base)
    instance, root = pilot_world(tmp_path, "kev.json", spec=spec)
    first = run(instance, root)
    activate(instance, first)
    import json

    path = root / "data/kev.json"
    doc = json.loads(path.read_bytes())
    doc["vulnerabilities"] = []
    doc["count"] = 0
    path.write_text(json.dumps(doc))
    second = run(instance, root, minute=1)
    assert all(c.lifecycle.state == "retired" for c in candidate_claims(instance, second))


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
