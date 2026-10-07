"""A propose_change_set terminal fans out over data: one Claim per element, one proposal.

``candidate_templates={"items": "$steps.<alias>.<list>"}`` turns each element a
provider produced into one Claim proposal item, each with its own dependency
closure and the evidence that closure reached. The fixed-list form is unchanged.
"""

from __future__ import annotations

from pathlib import Path

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
