"""A propose_change_set terminal fans out over data: one Claim per element, one proposal.

``candidate_templates={"items": "$steps.<alias>.<list>"}`` turns each element a
provider produced into one Claim proposal item, each with its own dependency
closure and the evidence that closure reached. The fixed-list form is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.procedures.models import ProposalItemsFanOut, ProposeChangeSetNode
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    _proposal_refs,
    _refusal,
    item_template,
    proposal_world,
    run_line,
)

FAN_OUT = fixtures.FAN_OUT


def _element(qualifier: str | None, **overrides: object) -> dict[str, object]:
    """One proposal item as the provider emits it: data, not a template."""

    statement = {
        **item_template()["statement"],  # type: ignore[dict-item]
        "object": {"kind": "literal", "value": "high"},
        "qualifier": qualifier,
    }
    return item_template(
        statement=statement,
        rationale=f"Severity as the feed reports it ({qualifier}).",
        **overrides,
    )


def _write_feed(root: Path, proposals: list[object]) -> None:
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({"severity": "high", "proposals": proposals})
    )


def test_the_fan_out_form_parses_and_the_fixed_list_form_is_unchanged() -> None:
    node = ProposeChangeSetNode(node_id="p", candidate_templates=FAN_OUT, result="$steps.result")
    assert isinstance(node.candidate_templates, ProposalItemsFanOut)
    assert node.model_dump(mode="json")["candidate_templates"] == FAN_OUT
    fixed = ProposeChangeSetNode(
        node_id="p", candidate_templates=[item_template()], result="$steps.result"
    )
    assert fixed.candidate_templates == (item_template(),)


def test_n_records_a_provider_emits_become_n_claims_of_one_proposal_each_citing_evidence(
    tmp_path: Path,
) -> None:
    instance, owner, root, line, _procedure = proposal_world(tmp_path, templates=FAN_OUT)  # type: ignore[arg-type]
    _write_feed(root, [_element(None), _element("reported"), _element("vendor")])

    state = run_line(instance, root, line)

    assert state.status == "succeeded", state.terminal
    (observation,) = state.source_observations
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None
    assert len(egress.children) == 3
    paths = {child.path for child in egress.children}
    assert len(paths) == 3 and paths <= set(egress.target_paths)
    assert len(_proposal_refs(instance)) == 1
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    assert inspection.proposal.candidate is not None
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claims = [parse_claim(tree[path], path=path) for path in sorted(paths)]
    assert {claim.statement.qualifier for claim in claims} == {None, "reported", "vendor"}
    # Each Claim cites the observation its own element came from.
    assert all(claim.backing.capture_digests == (observation.capture_digest,) for claim in claims)


def test_an_empty_list_proposes_nothing_and_the_run_succeeds(
    tmp_path: Path,
) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path, templates=FAN_OUT)  # type: ignore[arg-type]
    _write_feed(root, [])

    state = run_line(instance, root, line)

    # Nothing to propose is not a failure: no egress is prepared, no proposal exists.
    assert state.status == "succeeded", state.terminal
    assert state.terminal_egress == ()
    assert _proposal_refs(instance) == []


def test_a_malformed_element_refuses_typed_to_its_index(tmp_path: Path) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path, templates=FAN_OUT)  # type: ignore[arg-type]
    _write_feed(root, [_element(None), {"severity": "high"}, _element("vendor")])

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details["child_index"] == 1
    assert _proposal_refs(instance) == []


# --- data decides the shape: typed refusals (S3 review a F-004) --------------


@pytest.mark.parametrize(
    "items,reason",
    [
        (f"$steps.{fixtures.PROPOSALS_ALIAS}.nothing", "items_unresolved"),
        ("$steps.result.severity", "items_not_a_list"),
        (f"$steps.{fixtures.PROPOSALS_ALIAS}.complete", "items_not_a_list"),
        (f"$steps.{fixtures.PROPOSALS_ALIAS}.header", "items_not_a_list"),
    ],
    ids=["missing", "string", "bool", "object"],
)
def test_a_fan_out_whose_items_are_not_a_list_refuses_typed(
    tmp_path: Path, items: str, reason: str
) -> None:
    """Data decides the shape: a reference to anything but a list refuses, typed."""

    instance, _owner, root, line, _procedure = proposal_world(
        tmp_path,
        templates={"items": items},  # type: ignore[arg-type]
    )
    _write_feed(root, [])

    state = run_line(instance, root, line)

    refusal = _refusal(state)
    assert refusal.code == "proposal_item_invalid", refusal
    assert refusal.details == {"field": "candidate_templates.items", "reason": reason}
    assert _proposal_refs(instance) == []


# --- per-element evidence (S3 review a F-006) --------------------------------

_FEED_A, _FEED_B = ("sha256:" + digit * 64 for digit in "ab")


def _two_capture_state(items):  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from cruxible_core.procedures.terminal_dependencies import (
        AliasProvenanceV1,
        produced_capture_token,
    )

    a, b = produced_capture_token(_FEED_A), produced_capture_token(_FEED_B)
    provenance = AliasProvenanceV1(whole=frozenset({a, b}), items=items(a, b))
    return SimpleNamespace(
        input_payload={},
        outputs={"joined": {"items": [_element(None), _element("vendor")]}},
        provenance={"joined": provenance},
        alias_tokens=lambda aliases: provenance.whole if "joined" in aliases else frozenset(),
    )


def test_each_element_of_a_two_capture_fan_out_cites_only_its_own_capture() -> None:
    from cruxible_client.contracts.procedures.models import ProposalItemsFanOut
    from cruxible_core.procedures.execution import _fan_out_items
    from cruxible_core.procedures.proposal_delivery import evidence_by_item
    from cruxible_core.procedures.terminal_dependencies import build_terminal_item_manifest

    state = _two_capture_state(lambda a, b: (frozenset({a}), frozenset({b})))

    values, tokens = _fan_out_items(
        ProposalItemsFanOut(items="$steps.joined.items"), state=state, node_id="propose"
    )

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
    from cruxible_client.contracts.procedures.models import ProposalItemsFanOut
    from cruxible_core.procedures.execution import _fan_out_items, _RunRefusal

    state = _two_capture_state(lambda a, b: None)

    with pytest.raises(_RunRefusal) as refused:
        _fan_out_items(
            ProposalItemsFanOut(items="$steps.joined.items"), state=state, node_id="propose"
        )

    assert refused.value.refusal.code == "proposal_item_evidence_ambiguous"


# --- lineage is keyed to its exact path (S3 delta review F-005) --------------


class _State:
    """The slice of a run state the fan-out and projection provenance read."""

    def __init__(self, outputs, provenance):  # type: ignore[no-untyped-def]
        self.input_payload = {}
        self.outputs = outputs
        self.provenance = provenance

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

    from cruxible_client.contracts.procedures.models import ProposalItemsFanOut
    from cruxible_core.procedures.execution import _fan_out_items
    from cruxible_core.procedures.proposal_delivery import evidence_by_item
    from cruxible_core.procedures.terminal_dependencies import build_terminal_item_manifest

    values, tokens = _fan_out_items(
        ProposalItemsFanOut(items=reference), state=state, node_id="propose"
    )
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
    assert [value["statement"]["qualifier"] for value in values] == ["from-a"]
    assert evidence == {"item-0": _FEED_A}

    # `other` keeps no lineage of its own and must not borrow `items`'s: the
    # bundle was built from both Captures, so it refuses instead of citing A.
    from cruxible_core.procedures.execution import _RunRefusal

    with pytest.raises(_RunRefusal) as refused:
        _cited(state, "$steps.bundle.other")
    assert refused.value.refusal.code == "proposal_item_evidence_ambiguous"

    # Through its own source, B's element cites B.
    values, evidence = _cited(state, "$steps.b.items")
    assert [value["statement"]["qualifier"] for value in values] == ["from-b"]
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

    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_client.contracts.proposal_models import PROPOSAL_ITEM_REASON_MAX_LENGTH
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.proposals.review import service_review_playbill_proposal
    from cruxible_core.storage.cas import BodyAccessContext

    count = 400
    long_reason = "Reported by the vendor feed. " * 300  # past the old 4096-character cap
    overlong = "x" * (PROPOSAL_ITEM_REASON_MAX_LENGTH + 10)
    templates = tuple(
        item_template(
            statement={
                **item_template()["statement"],  # type: ignore[dict-item]
                "qualifier": f"q{index:03d}",
            },
            rationale=(
                long_reason
                if index == 7
                else overlong
                if index == 8
                else f"Reason for item {index}."
            ),
        )
        for index in range(count)
    )
    # Lowering 400 items takes about 4.5 s on a fast workstation and passed the
    # Line run's default 10 s budget on a loaded CI runner; this test is about
    # reasons, not time.
    instance, owner, root, line, _procedure = proposal_world(
        tmp_path, templates=templates, wall_clock_seconds=300
    )

    state = run_line(instance, root, line)

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
    assert seen[by_index[7]] == long_reason.strip()
    # A reason past the per-item bound is kept to the bound and marked.
    assert truncated == {by_index[8]}
    assert len(seen[by_index[8]]) == PROPOSAL_ITEM_REASON_MAX_LENGTH
    assert seen[by_index[399]] == "Reason for item 399."
    assert review.rationale is not None and "400 items;" in review.rationale

    from tests.core_support._retirement_support import activate_proposal

    activate_proposal(instance, owner, egress.proposal_id)
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
    assert why.reason.run == state.run_id and why.reason.terminal == "propose"
    # Reading one Claim's reason reads one record, whatever the batch size.
    assert len(reads) == 1
