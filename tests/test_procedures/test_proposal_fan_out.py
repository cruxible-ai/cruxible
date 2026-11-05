"""Typed records fan out through a declared claims template, never command data."""

from pathlib import Path

import pytest

from cruxible_client.contracts.procedures.nodes import RetiredNodeKind, refuse_retired_kinds
from cruxible_core.procedures.completeness import Completeness
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_claims_terminal import (
    candidate_claims,
    claims_spec,
    claims_world,
    install,
    run,
)

FAN_OUT = fixtures.FAN_OUT


def _element(qualifier=None, **overrides):
    # Hostile data fixture for consumers testing the retired form.
    return {
        "id": qualifier or "one",
        "severity": "high",
        "rationale": f"Severity as the feed reports it ({qualifier or 'one'}).",
        **overrides,
    }


def _write_feed(root: Path, proposals: list[object]) -> None:
    (root / fixtures.RELATIVE_PATH).write_bytes(
        fixtures.canonical_bytes({"severity": "high", "proposals": proposals})
    )


@pytest.mark.parametrize("kind", ["propose_change_set", "settle_change_set"])
def test_command_fan_out_is_refused_at_acceptance(kind):
    with pytest.raises(RetiredNodeKind, match="claims"):
        refuse_retired_kinds([{"kind": kind, "node_id": "write", "candidate_templates": FAN_OUT}])


def test_rows_become_one_proposal_with_per_row_evidence(tmp_path):
    entries = [{"id": f"CVE-2026-{i:04d}", "status": "open"} for i in range(1, 4)]
    instance, root = claims_world(tmp_path, {"entries": entries})
    install(instance, claims_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    claims = candidate_claims(instance, state)
    assert len(claims) == 3
    assert len({c.statement.subject.artifact_path for c in claims}) == 3
    assert {c.backing.procedure_evidence.value.record_locator for c in claims} == {
        f"/entries/{i}" for i in range(3)
    }
    assert {c.backing.procedure_evidence.value.capture_digest for c in claims} == {
        state.source_observations[0].capture_digest
    }
    assert len({c.backing.procedure_evidence.template for c in claims}) == 1


def test_empty_records_propose_nothing(tmp_path):
    instance, root = claims_world(tmp_path, {"entries": []})
    install(instance, claims_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert not any(e.proposal_id or e.children for e in state.terminal_egress)


def test_invalid_field_is_excluded_without_dropping_other_records(tmp_path):
    instance, root = claims_world(
        tmp_path,
        {
            "entries": [
                {"id": "CVE-2026-0001", "status": "open"},
                {"id": "CVE-2026-0002", "status": "Maybe"},
            ]
        },
    )
    install(instance, claims_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert len(candidate_claims(instance, state)) == 1
    assert state.terminal_egress[0].claims_diagnostics[0].kind == "extract_refused"


def _claim_rows(reference, *, state, node_id):
    from cruxible_core.procedures.execution import _fan_out_list

    return _fan_out_list(
        reference,
        state=state,
        node_id=node_id,
        field="for_each",
        noun="Claim rows",
        invalid_code="proposal_item_invalid",
        ambiguous_code="proposal_item_evidence_ambiguous",
    )


_FEED_A, _FEED_B = ("sha256:" + digit * 64 for digit in "ab")


def _two_capture_state(items):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        CaptureRootCache,
        produced_capture_token,
    )

    a, b = produced_capture_token(_FEED_A), produced_capture_token(_FEED_B)
    provenance = AliasProvenanceV1(whole=frozenset({a, b}), items=items(a, b))
    return SimpleNamespace(
        input_payload={},
        parameters={},
        outputs={"joined": {"items": [_element(None), _element("vendor")]}},
        provenance={"joined": provenance},
        completeness=Completeness(),
        derivations={},
        capture_root_cache=CaptureRootCache(),
        alias_tokens=lambda aliases: provenance.whole if "joined" in aliases else frozenset(),
    )


def test_each_element_of_a_two_capture_fan_out_cites_only_its_own_capture() -> None:
    from cruxible_core.procedures.proposal_delivery import evidence_by_item
    from cruxible_core.procedures.terminal_dependencies import build_terminal_item_manifest

    state = _two_capture_state(lambda a, b: (frozenset({a}), frozenset({b})))

    values, tokens = _claim_rows("$steps.joined.items", state=state, node_id="propose")

    assert len(values) == 2
    manifests = {
        f"item-{index}": build_terminal_item_manifest(
            closure, run_id="RUN-test", terminal_node_id="propose", item_key=f"item-{index}"
        )
        for index, closure in enumerate(tokens)
    }
    from types import SimpleNamespace

    request = SimpleNamespace(
        items=tuple(
            SimpleNamespace(item_key=f"item-{index}", child_index=index, value=value)
            for index, value in enumerate(values)
        )
    )
    assert evidence_by_item(request, manifests) == {"item-0": _FEED_A, "item-1": _FEED_B}  # type: ignore[arg-type]


def test_a_two_capture_fan_out_without_lineage_refuses_instead_of_widening() -> None:
    from cruxible_core.procedures.execution import _RunRefusal

    state = _two_capture_state(lambda a, b: None)

    with pytest.raises(_RunRefusal) as refused:
        _claim_rows("$steps.joined.items", state=state, node_id="propose")

    assert refused.value.refusal.code == "proposal_item_evidence_ambiguous"


# --- lineage is keyed to its exact path (S3 delta review F-005) --------------


class _State:
    """The slice of a run state the fan-out and projection provenance read."""

    def __init__(self, outputs, provenance):  # type: ignore[no-untyped-def]
        from cruxible_core.procedures.terminal_dependencies import CaptureRootCache

        self.input_payload = {}
        self.parameters = {}
        self.outputs = outputs
        self.provenance = provenance
        self.completeness = Completeness()
        self.derivations = {}
        self.capture_root_cache = CaptureRootCache()

    def alias_tokens(self, aliases):  # type: ignore[no-untyped-def]
        tokens = frozenset()
        for alias in aliases:
            found = self.provenance.get(alias)
            if found is not None:
                tokens |= found.whole
        return tokens

    def item_tokens(self, alias, index):  # type: ignore[no-untyped-def]
        found = self.provenance.get(alias)
        return frozenset() if found is None else found.item(index)


def _two_lists():  # type: ignore[no-untyped-def]
    """Lists `a` and `b`, each element carrying the Capture it came from."""

    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        produced_capture_token,
    )

    a, b = produced_capture_token(_FEED_A), produced_capture_token(_FEED_B)
    outputs = {
        "a": {"items": [_element("from-a")]},
        "b": {"items": [_element("from-b")]},
    }
    provenance = {
        "a": AliasProvenanceV1(whole=frozenset({a}), items=(frozenset({a}),)),
        "b": AliasProvenanceV1(whole=frozenset({b}), items=(frozenset({b}),)),
    }
    return _State(outputs, provenance)


def _cited(state, reference):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from cruxible_core.procedures.proposal_delivery import evidence_by_item
    from cruxible_core.procedures.terminal_dependencies import build_terminal_item_manifest

    values, tokens = _claim_rows(reference, state=state, node_id="propose")
    manifests = {
        f"item-{index}": build_terminal_item_manifest(
            closure, run_id="RUN-test", terminal_node_id="propose", item_key=f"item-{index}"
        )
        for index, closure in enumerate(tokens)
    }
    request = SimpleNamespace(
        items=tuple(
            SimpleNamespace(item_key=f"item-{index}", child_index=index, value=value)
            for index, value in enumerate(values)
        )
    )
    return values, evidence_by_item(request, manifests)  # type: ignore[arg-type]


def _bundle(state):  # type: ignore[no-untyped-def]
    """Project {"items": a.items, "other": b.items}: lineage is recorded for `items` only."""

    from cruxible_core.procedures.execution import _projected_provenance

    fields = {"items": "$steps.a.items", "other": "$steps.b.items"}
    value = {"items": state.outputs["a"]["items"], "other": state.outputs["b"]["items"]}
    state.outputs["bundle"] = value
    state.provenance["bundle"] = _projected_provenance(
        fields, state=state, base=state.alias_tokens({"a", "b"}), value=value
    )
    return state


def test_each_projected_list_cites_only_its_own_sources_capture() -> None:
    state = _bundle(_two_lists())

    values, evidence = _cited(state, "$steps.bundle.items")
    assert [value["id"] for value in values] == ["from-a"]
    assert evidence == {"item-0": _FEED_A}

    # `other` keeps no lineage of its own and must not borrow `items`'s: the
    # bundle was built from both Captures, so it refuses instead of citing A.
    from cruxible_core.procedures.execution import _RunRefusal

    with pytest.raises(_RunRefusal) as refused:
        _cited(state, "$steps.bundle.other")
    assert refused.value.refusal.code == "proposal_item_evidence_ambiguous"

    # Through its own source, B's element cites B.
    values, evidence = _cited(state, "$steps.b.items")
    assert [value["id"] for value in values] == ["from-b"]
    assert evidence == {"item-0": _FEED_B}


def test_a_nested_path_does_not_borrow_the_top_level_lineage() -> None:
    from cruxible_core.procedures.execution import _lineage_alias, _RunRefusal

    assert _lineage_alias("$steps.bundle.items") == "bundle"
    assert _lineage_alias("$steps.bundle.items.0.items") is None
    assert _lineage_alias("$steps.bundle.other") is None
    state = _bundle(_two_lists())
    state.outputs["bundle"]["items"][0]["nested"] = [_element("nested")]

    with pytest.raises(_RunRefusal) as refused:
        _cited(state, "$steps.bundle.items.0.nested")
    assert refused.value.refusal.code == "proposal_item_evidence_ambiguous"


def test_a_field_without_lineage_from_one_capture_cites_that_capture() -> None:
    from cruxible_core.procedures.terminal_dependencies import AliasProvenanceV1

    state = _two_lists()
    state.outputs["only_a"] = {"other": state.outputs["a"]["items"]}
    state.provenance["only_a"] = AliasProvenanceV1(whole=state.provenance["a"].whole)

    _values, evidence = _cited(state, "$steps.only_a.other")

    assert evidence == {"item-0": _FEED_A}


# --- per-Claim reasons over a large terminal -----------------------------------


def test_a_400_item_terminal_keeps_every_reason_and_reads_one_in_constant_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No item's reason is lost to the change-set bound; one Claim's reason is one read."""

    import json

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.proposals.review import service_review_playbill_proposal
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.test_procedures import test_claims_terminal as native

    original_contract = native.record_contract

    def roomy_contract():
        contract = original_contract()
        return contract.model_copy(
            update={
                "selection_budget": contract.selection_budget.model_copy(
                    update={"max_bytes": 1 << 20}
                )
            }
        )

    monkeypatch.setattr(native, "record_contract", roomy_contract)
    count = 400
    # Each rationale is within D-R13's one-line bound; together they exceed
    # the change-set summary's old 4096-character bound.
    long_reason = "Reported by the vendor feed. " * 8
    entries = [
        {
            "id": "CVE-2026-0001",
            "status": "open",
            "qualifier": f"q{i:03d}",
            "note": long_reason if i == 7 else "x" * 255 if i == 8 else str(i),
        }
        for i in range(count)
    ]
    from cruxible_client.contracts.shapes import ContractField, StringType

    rows = native.ROW.model_copy(
        update={
            "fields": {**native.ROW.fields, "qualifier": ContractField(type=StringType(max_len=64))}
        }
    )
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, rows=rows
    )
    (root / "data/doc.json").write_text(json.dumps({"entries": entries}))
    spec = claims_spec(
        mappings=[
            {
                "type": "security.vulnerability.status",
                "value": "$item.fields.status",
                "qualifier": "$item.fields.qualifier",
                "rationale": "Reason for item {$item.fields.note}.",
            }
        ]
    ).model_dump(mode="python", by_alias=True)
    spec["steps"][1]["key"] = {}
    spec["steps"][1]["fields"]["qualifier"] = {"at": "qualifier"}
    spec["steps"][-1]["subject"]["key"] = {"cve_id": "CVE-2026-0001"}
    from tests.support.procedures import procedure_limits, procedure_spec

    spec["hard_caps"] = procedure_limits(
        wall_clock="PT300S", provider_calls=1, capture_bytes=1048576, items=1000
    ).model_dump(mode="json")
    install(instance, procedure_spec(base=spec, reference_text=True))
    state = run(instance, root)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.proposal_id is not None and len(egress.children) == count
    access = BodyAccessContext(principal_id="unit-test", can_read_body=True)
    seen: dict[str, str] = {}
    truncated: set[str] = set()
    after: str | None = None
    while True:
        review = service_review_playbill_proposal(
            instance,
            proposal_id=egress.proposal_id,
            access=access,
            item_reasons_after=after,
            item_reasons_limit=150,
            detail=True,
        )
        assert review.item_reason_count == count
        assert len(review.item_reasons) <= 150
        seen.update((item.path, item.reason) for item in review.item_reasons)
        truncated.update(item.path for item in review.item_reasons if item.truncated)
        after = review.item_reasons_next_cursor
        if after is None:
            break
    by_index = {child.child_index: child.path for child in egress.children}
    assert len(seen) == count
    assert seen[by_index[7]] == "Reason for item " + long_reason + "."
    # D-R13 bounds each Claim rationale before publication.
    assert truncated == set()
    assert len(seen[by_index[8]]) <= 280
    assert seen[by_index[399]] == "Reason for item 399."
    assert review.rationale is not None and "400 items;" in review.rationale

    from tests.test_procedures.test_claims_terminal import activate

    activate(instance, state)
    claim_id = by_index[123].rsplit("/", 1)[-1].removesuffix(".json")
    reads: list[str] = []
    original = ProposalEvidenceStore.read_record_bytes

    def counted(path: Path) -> bytes:
        if "item-reasons" in path.parts:
            reads.append(str(path))
        return original(path)

    monkeypatch.setattr(ProposalEvidenceStore, "read_record_bytes", staticmethod(counted))
    why = service_playbill_get(
        instance, request=GetRequest(ref=claim_id, detail="why"), access=access
    )
    assert why.reason is not None and why.reason.reason == "Reason for item 123."
    assert why.reason.run == state.run_id and why.reason.terminal == "write"
    # Reading one Claim's reason reads one record, whatever the batch size.
    assert len(reads) == 1
