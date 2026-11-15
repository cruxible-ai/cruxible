"""A proposal from a Trigger-fed run cites the Capture the run was admitted.

A Line that binds its triggering Capture to a Source input (``trigger_input``)
consumes a retained Capture: the run produces none of its own. Its proposal
items cite the one root of their closure's produced AND admitted Captures
together: one Capture is its own root, zero is missing, and several are
ambiguous unless one derives from all the others. A provider-made source
field never chooses the evidence.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.procedures.windows import CaptureEventNameSelector
from cruxible_client.contracts.shapes import contract_path, render_contract
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.procedures.proposal_delivery import ProposalDeliveryRefused, evidence_by_item
from cruxible_core.procedures.terminal_dependencies import (
    TerminalItemDependencyManifestV1,
    citable_captures,
)
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    TriggerFire,
    service_run_playbill_line,
)
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._light_capture_support import file_contract as capture_contract
from tests.support.lines import line_trigger, trigger_members
from tests.support.procedures import boundary_contract, procedure_members
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    PREDICATE,
    SUBJECT_ID,
    SUBJECT_KIND,
    _claim_type,
    _proposal_refs,
    _subject,
    item_template,
    terminal_procedure,
)

TRIGGER = "advisory-landed"
PROPOSER = "advisory-proposer"


def _trigger_fed_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """The plain Source Procedure produces Captures; a proposing Line consumes them."""

    instance, owner, _procedure, root, policy = fixtures._world(tmp_path, document=True)
    # A second Procedure of the same shape, ending in a proposal terminal; the
    # triggering Capture is bound to its call, which it reads through an extract.
    monkeypatch.setattr(fixtures, "PROCEDURE_NAME", PROPOSER)
    proposer = terminal_procedure(
        fixtures._procedure(
            instance,
            root=root,
            contract=capture_contract(),
        ),
        document=True,
    )
    monkeypatch.undo()
    contract = capture_contract()
    selector = CaptureEventNameSelector(
        capture_contract_identity=contract.identity,
    )
    served = fixtures._served_line(instance, proposer, policy)
    line = type(served).model_validate(
        {
            **served.model_dump(mode="python"),
            "max_authority": "propose",
            "trigger_input": fixtures.SOURCE_CAPTURE,
            "trigger_event": selector,
        }
    )
    claim_type = _claim_type(contract.identity.name)
    mandate = fixtures._line_mandate(line, proposer).model_copy(
        update={"identity": ArtifactIdentity(kind="ProcedureMandate", name="proposer-mandate")}
    )
    fixtures._accept_more(
        instance,
        owner,
        {
            **procedure_members(proposer),
            **{
                contract_path(c.name, c.version): render_contract(c)
                for c in (
                    boundary_contract(
                        "security.delivery-row", {"severity": "string{max_len: 256}"}
                    ),
                    boundary_contract("security.delivery-output", {}),
                )
            },
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            claim_type_path(claim_type.predicate): render_claim_type(claim_type),
            subject_path(SUBJECT_KIND, SUBJECT_ID): render_subject(_subject()),
            **trigger_members(
                line_trigger(
                    TRIGGER,
                    line=line.identity.name,
                    schedule=CaptureLandingSchedule(event=selector),
                )
            ),
        },
        name="trigger-fed-proposals",
    )
    return instance, owner, root, line


def _event(instance, root):  # type: ignore[no-untyped-def]
    state, _spawned = fixtures._run(instance, root)
    assert state.status == "succeeded", state
    event = next(o.capture_event for o in state.outcomes if o.event_kind == "produced_capture")
    return state.source_observations[0].capture_digest, event


def test_a_trigger_fed_run_proposes_a_claim_citing_the_admitted_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner, root, line = _trigger_fed_world(tmp_path, monkeypatch)
    capture_digest, event = _event(instance, root)

    state = service_run_playbill_line(
        instance,
        path_identity_digest=line.identity.name,
        request=LineRunRequest(line=line.identity.name),
        actor_context=fixtures._actor(instance),
        caller_rung=2,
        daemon_clock=fixtures._TestClock(fixtures.NOW + timedelta(seconds=2)),
        # The Trigger's occurrence on this event, as dispatch admits it; no
        # Provider runtime or workspace reader: the input is the retained Capture.
        trigger_fire=TriggerFire(trigger=TRIGGER, event=event),
    )

    assert state.status == "succeeded", state.terminal
    # The run fetched nothing: its only Capture is the admitted one.
    assert state.source_observations == ()
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None
    (child,) = egress.children
    assert len(_proposal_refs(instance)) == 1
    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    assert inspection.proposal.candidate is not None
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[child.path], path=child.path)
    assert claim.statement.predicate == PREDICATE
    assert claim.backing.capture_digests == (capture_digest,)


# --- the selection rule, per item --------------------------------------------

_A, _B = ("sha256:" + digit * 64 for digit in "ab")


def _manifest(*, produced: tuple[str, ...] = (), admitted: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    return TerminalItemDependencyManifestV1(
        run_id="RUN-test",
        terminal_node_id="propose",
        item_key="item-0",
        produced_capture_digests=produced,
        admitted_capture_digests=admitted,
    )


def _request(value: object = None, *, cited: str | None = None):  # type: ignore[no-untyped-def]
    receipts = (
        {}
        if cited is None
        else {"item-0": SimpleNamespace(value=SimpleNamespace(capture_digest=cited))}
    )
    return SimpleNamespace(
        items=(
            SimpleNamespace(
                item_key="item-0", child_index=0, value=item_template() if value is None else value
            ),
        ),
        claims_evidence=receipts,
    )


def _selected(capture: str) -> dict[str, object]:
    """A resolved source-v2 item that names its evidence explicitly."""

    return {
        **item_template(),
        "tag": "playbill-procedure-claim-proposal-item-v2",
        "source": {
            "tag": "playbill-existing-capture-citation-source-v1",
            "capture_digest": capture,
        },
    }


def test_an_item_cites_its_single_admitted_capture_when_its_closure_produced_none() -> None:
    assert evidence_by_item(_request(), {"item-0": _manifest(admitted=(_A,))}) == {"item-0": _A}


def test_two_admitted_captures_are_ambiguous_and_none_is_missing() -> None:
    with pytest.raises(ProposalDeliveryRefused) as ambiguous:
        evidence_by_item(_request(), {"item-0": _manifest(admitted=(_A, _B))})
    assert ambiguous.value.code == "proposal_item_evidence_ambiguous"
    with pytest.raises(ProposalDeliveryRefused) as missing:
        evidence_by_item(_request(), {"item-0": _manifest()})
    assert missing.value.code == "proposal_item_evidence_missing"


def test_an_unrelated_second_capture_refuses_even_with_a_receipt_citing_one() -> None:
    """D-R37 d1: a receipt alone never proves a root; the closure must record one.

    Two Captures with no recorded derivation between them have no root
    (``capture_root((A, B), {})`` is ``None``), so a receipt merely naming one
    of them is refused -- membership in the closure is not a proof.
    """

    manifests = {"item-0": _manifest(produced=(_A, _B))}
    with pytest.raises(ProposalDeliveryRefused) as ambiguous:
        evidence_by_item(_request(cited=_B), manifests)
    assert ambiguous.value.code == "proposal_item_evidence_ambiguous"
    # Also refuses a receipt naming a Capture outside the closure entirely.
    with pytest.raises(ProposalDeliveryRefused) as outside:
        evidence_by_item(_request(cited="sha256:" + "9" * 64), manifests)
    assert outside.value.code == "proposal_item_evidence_ambiguous"


def test_a_real_derived_root_the_claims_receipt_names_succeeds() -> None:
    """D-R37 d1: a receipt citing the closure's actual recorded root is proven, not assumed."""

    manifests = {"item-0": _manifest(produced=(_A, _B))}
    # A's manifest records B as its one input, so A's recorded inputs cover
    # the rest of the closure: A is the root (D-R37 d1).
    derivations = {_A: (_B,)}
    assert evidence_by_item(_request(cited=_A), manifests, derivations=derivations) == {
        "item-0": _A
    }


@pytest.mark.parametrize("n", [10, 100])
def test_evidence_by_item_is_free_for_every_item_after_the_first_sharing_a_chain(n: int) -> None:
    """D-R37 cost: delivery proves each item's root (proposal_delivery.py ``evidence_by_item``).

    Many items in one terminal citing the SAME closure -- the whole cumulative
    derive chain, as the executor records it -- share one ``CaptureRootCache``:
    the root is proven once for the whole call with linear edge reads, and the
    per-item cost stays constant as items are added.
    """

    from cruxible_core.procedures.terminal_dependencies import CaptureRootCache
    from tests.test_procedures.test_terminal_dependencies import (
        _CountingDerivations,
        _cumulative_chain,
    )

    digests, derivations = _cumulative_chain(n)
    root = digests[-1]
    items = 50
    request = SimpleNamespace(
        items=tuple(
            SimpleNamespace(item_key=f"item-{i}", child_index=i, value=item_template())
            for i in range(items)
        ),
        claims_evidence={
            f"item-{i}": SimpleNamespace(value=SimpleNamespace(capture_digest=root))
            for i in range(items)
        },
    )
    manifests = {f"item-{i}": _manifest(produced=tuple(digests)) for i in range(items)}
    counted = _CountingDerivations(derivations)
    cache = CaptureRootCache()

    evidence = evidence_by_item(request, manifests, derivations=counted, cache=cache)  # type: ignore[arg-type]

    assert evidence == {f"item-{i}": root for i in range(items)}
    # Linear in the chain length and independent of the item count: one
    # proof for the whole call, not one per item; one retained entry.
    assert counted.edges <= 3 * n
    assert counted.lookups <= 2 * n + 1
    assert len(cache.roots) == 1


def test_an_unrelated_admitted_capture_refuses_beside_a_produced_one() -> None:
    """D-R37 d1: the root is proven over produced AND admitted Captures together.

    A produced B beside an admitted, unrelated A has no root
    (``capture_root((A, B), {})`` is None). The admitted Capture is never
    dropped in favour of the produced one, so no receipt or provider-made
    source can make either the item's evidence.
    """

    manifest = _manifest(produced=(_B,), admitted=(_A,))
    assert citable_captures(manifest) == (_B, _A)
    for request in (
        _request(),
        _request(cited=_B),
        _request(cited=_A),
        _request(_selected(_A), cited=_B),
    ):
        with pytest.raises(ProposalDeliveryRefused) as refused:
            evidence_by_item(request, {"item-0": manifest})
        assert refused.value.code == "proposal_item_evidence_ambiguous"
        assert refused.value.details["capture_digests"] == [_B, _A]


def test_a_produced_root_derived_from_the_admitted_capture_succeeds() -> None:
    """D-R37 d1: a derive over the trigger's admitted Capture roots the mixed closure."""

    manifests = {"item-0": _manifest(produced=(_B,), admitted=(_A,))}
    # B's manifest records the admitted A as its input: B is the root.
    derivations = {_B: (_A,)}
    assert evidence_by_item(_request(cited=_B), manifests, derivations=derivations) == {
        "item-0": _B
    }
    # A provider-made source naming A stays inert; the proven root decides.
    assert evidence_by_item(
        _request(_selected(_A), cited=_B), manifests, derivations=derivations
    ) == {"item-0": _B}
    # A receipt citing the admitted input, not the root, is refused.
    with pytest.raises(ProposalDeliveryRefused) as refused:
        evidence_by_item(_request(cited=_A), manifests, derivations=derivations)
    assert refused.value.code == "proposal_item_evidence_ambiguous"


@pytest.mark.parametrize("n", [10, 100])
def test_mixed_closure_proof_is_linear_for_either_outcome(n: int) -> None:
    """D-R37 cost: the union proof over an admitted base plus a produced cumulative chain.

    Every derive records the admitted trigger Capture and every prior derive
    (the executor's cumulative manifests). 50 items share the closure: the
    related case proves the root once, reading only the root's own inputs
    (linear in n); an unrelated admitted Capture refuses once per closure,
    reading each recorded edge at most once (linear in the graph's edges --
    proving no member reaches it needs every reachable edge read).
    """

    from cruxible_core.procedures.terminal_dependencies import CaptureRootCache
    from tests.test_procedures.test_terminal_dependencies import (
        _CountingDerivations,
        _cumulative_chain,
    )

    chain, derivations = _cumulative_chain(n + 1)
    admitted, produced = chain[0], tuple(chain[1:])
    root = chain[-1]
    items = 50
    request = SimpleNamespace(
        items=tuple(
            SimpleNamespace(item_key=f"item-{i}", child_index=i, value=item_template())
            for i in range(items)
        ),
        claims_evidence={
            f"item-{i}": SimpleNamespace(value=SimpleNamespace(capture_digest=root))
            for i in range(items)
        },
    )

    def manifests(base: str):  # type: ignore[no-untyped-def]
        return {f"item-{i}": _manifest(produced=produced, admitted=(base,)) for i in range(items)}

    counted = _CountingDerivations(derivations)
    cache = CaptureRootCache()
    evidence = evidence_by_item(request, manifests(admitted), derivations=counted, cache=cache)  # type: ignore[arg-type]
    assert evidence == {f"item-{i}": root for i in range(items)}
    assert counted.edges <= 3 * n
    assert counted.lookups <= 2 * n + 3
    assert len(cache.roots) == 1

    unrelated = "sha256:" + "f" * 64
    counted = _CountingDerivations(derivations)
    with pytest.raises(ProposalDeliveryRefused) as refused:
        evidence_by_item(request, manifests(unrelated), derivations=counted, cache=cache)  # type: ignore[arg-type]
    assert refused.value.code == "proposal_item_evidence_ambiguous"
    assert counted.edges <= sum(len(inputs) for inputs in derivations.values())
    assert counted.lookups <= 3 * n + 3
    assert len(cache.roots) == 2


def test_an_explicitly_selected_capture_never_chooses_the_evidence() -> None:
    # Provider-made source fields are inert; only the executor's closure chooses evidence.
    assert evidence_by_item(_request(_selected(_A)), {"item-0": _manifest(produced=(_B,))}) == {
        "item-0": _B
    }
    with pytest.raises(ProposalDeliveryRefused) as outside:
        evidence_by_item(_request(_selected(_A)), {"item-0": _manifest()})
    assert outside.value.code == "proposal_item_evidence_missing"
