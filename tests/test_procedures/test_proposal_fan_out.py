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

FAN_OUT = {"items": f"$steps.{fixtures.SOURCE_ALIAS}.content.json.proposals"}


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
        canonical_bytes({**fixtures.ADVISORY, "proposals": proposals})
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


def _feed_without_list(root: Path, value: object, *, omit: bool = False) -> None:
    body = dict(fixtures.ADVISORY)
    if not omit:
        body["proposals"] = value
    (root / fixtures.RELATIVE_PATH).write_bytes(canonical_bytes(body))


@pytest.mark.parametrize(
    "value,omit,reason",
    [
        (None, True, "items_unresolved"),
        ("not a list", False, "items_not_a_list"),
        (None, False, "items_not_a_list"),
        ({"items": []}, False, "items_not_a_list"),
    ],
    ids=["missing", "string", "null", "object-holding-items"],
)
def test_a_fan_out_whose_items_are_not_a_list_refuses_typed(
    tmp_path: Path, value: object, omit: bool, reason: str
) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path, templates=FAN_OUT)  # type: ignore[arg-type]
    _feed_without_list(root, value, omit=omit)

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
