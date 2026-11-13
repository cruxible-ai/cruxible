"""Extract on the served run lane: an accepted CaptureContract's decoding, or a refusal.

The workspace read commits the file's exact bytes; an ``extract`` decodes them
by the accepted CaptureContract's ``decoding``. A Procedure whose extract reads
a CaptureContract that declares no decoding is refused when it is accepted.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
from cruxible_client.contracts.shapes import contract_path, render_contract
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._light_capture_support import record_contract
from tests.support.procedures import boundary_contract
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import _claim_type, terminal_procedure


def test_an_extract_over_a_contract_without_decoding_is_refused_at_acceptance(
    tmp_path: Path,
) -> None:
    instance, _owner, procedure, _root, _policy = fixtures._world(
        tmp_path, accept_procedure=False, contract=record_contract(decoding=None)
    )
    extracting = terminal_procedure(procedure, templates=fixtures.FAN_OUT)  # type: ignore[arg-type]

    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import HaltNode
    from tests.support.procedures import procedure_definition

    definition = procedure_definition(
        base=extracting.definition,
        nodes=(*extracting.definition.nodes[:-1], HaltNode(node_id="propose", reason="extracted")),
    )
    extracting = extracting.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )

    inspection = submit_member_candidate(
        instance,
        members={
            procedure_path(extracting.identity.name): render_procedure(extracting),
            claim_type_path("security.advisory.severity"): render_claim_type(
                _claim_type(record_contract().identity.name)
            ),
            **{
                contract_path(c.name, c.version): render_contract(c)
                for c in (
                    boundary_contract(
                        "security.delivery-row", {"severity": "string{max_len: 256}"}
                    ),
                    boundary_contract("security.delivery-output", {}),
                )
            },
        },
        actor_id="owner",
        proposal_name="extract-without-decoding",
        proposal_family="procedure",
        timestamp=fixtures.ACCEPT_STAMP,
    )

    assert inspection.proposal.candidate is None
    (diagnostic,) = inspection.proposal.evaluation.diagnostics
    assert diagnostic.code == "cruxible.procedure.extract_decoding_undeclared", diagnostic


def test_the_same_extract_over_a_contract_with_decoding_is_accepted_and_reads_the_feed(
    tmp_path: Path,
) -> None:
    instance, _owner, _procedure, root, _policy = fixtures._world(tmp_path, document=True)
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({"severity": "critical", "advisory_id": "OSV-2026-0009"})
    )

    state, _spawned = fixtures._run(instance, root)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"severity": "critical"}
