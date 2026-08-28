"""Opt-in PB-E dogfood over the ratified Playbill design and program bytes."""

from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_dereference_playbill_document,
    service_playbill_document_history,
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.review import service_review_playbill_proposal
from tests.test_playbill.test_activation import _sign
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance

pytestmark = pytest.mark.skipif(
    os.environ.get("CRUXIBLE_RUN_PLAYBILL_DOGFOOD") != "1",
    reason="set CRUXIBLE_RUN_PLAYBILL_DOGFOOD=1 to govern the external ratified specs",
)

_DEFAULT_DESIGN = Path("/Users/robertmalone/tmp-workspaces/playbill-design-v6.md")
_DEFAULT_PROGRAM = Path("/Users/robertmalone/tmp-workspaces/playbill-implementation-program-v1.md")


def _external_path(variable: str, default: Path) -> Path:
    path = Path(os.environ.get(variable, str(default))).expanduser()
    if not path.is_file():
        pytest.fail(f"PB-E dogfood input is missing: {path}")
    return path


def _shell(
    *,
    document_id: str,
    title: str,
    body_digest: str,
    revision: int,
    predecessor_digest: str | None = None,
) -> DocumentShell:
    return DocumentShell(
        identity=f"document:{document_id}",
        document_kind="implementation-program" if document_id == "program" else "design",
        title=title,
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
            approval_roles=("owner",),
        ),
        governance_scope=("project:playbill",),
        predecessor_digest=predecessor_digest,
        lifecycle=DocumentLifecycle(revision=revision),
    )


def _accept(instance, owner, shell: DocumentShell, *, name: str):
    proposed = service_propose_playbill_document(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name=name,
        timestamp=TIMESTAMP,
    ).proposal
    assert proposed.candidate is not None
    approval = _sign(
        owner,
        proposed.candidate.candidate_digest,
        proposed.candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposed.admission.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"
    return proposed


def test_ratified_specs_survive_supersession_and_projection_rebuild(tmp_path: Path) -> None:
    """Read external bytes only; all ledger, CAS, and projection writes stay in tmp_path."""

    design_bytes = _external_path("CRUXIBLE_PLAYBILL_DOGFOOD_DESIGN", _DEFAULT_DESIGN).read_bytes()
    program_bytes = _external_path(
        "CRUXIBLE_PLAYBILL_DOGFOOD_PROGRAM", _DEFAULT_PROGRAM
    ).read_bytes()
    instance, owner, _reviewer = _instance(tmp_path)

    design_body = service_store_playbill_body(instance, content=design_bytes)
    design_v1 = _shell(
        document_id="design",
        title="Playbill design v6",
        body_digest=design_body.digest,
        revision=1,
    )
    _accept(instance, owner, design_v1, name="dogfood-design")

    program_body = service_store_playbill_body(instance, content=program_bytes)
    program_v1 = _shell(
        document_id="program",
        title="Playbill implementation program v1",
        body_digest=program_body.digest,
        revision=1,
    )
    _accept(instance, owner, program_v1, name="dogfood-program")

    superseding_bytes = design_bytes + b"\n<!-- PB-E dogfood successor -->\n"
    design_v2_body = service_store_playbill_body(instance, content=superseding_bytes)
    design_v2 = _shell(
        document_id="design",
        title="Playbill design v6",
        body_digest=design_v2_body.digest,
        revision=2,
        predecessor_digest=document_digest(design_v1).tagged,
    )
    proposed = service_propose_playbill_document(
        instance,
        shell=design_v2,
        actor_id="owner",
        proposal_name="dogfood-design-successor",
        timestamp=TIMESTAMP,
    ).proposal
    assert proposed.candidate is not None
    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposed.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert "PB-E dogfood successor" in (review.documents[0].readable_diff or "")
    approval = _sign(
        owner,
        proposed.candidate.candidate_digest,
        proposed.candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=proposed.admission.proposal_id,
        activated_by="owner",
    )
    accepted = instance.accepted_coordinate()

    projection_directory = Path(instance.inspect().storage_directories["projections"])
    for path in projection_directory.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened.accepted_coordinate() == accepted
    recovered_design = service_dereference_playbill_document(
        reopened,
        identity="document:design",
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    recovered_program = service_dereference_playbill_document(
        reopened,
        identity="document:program",
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert base64.b64decode(recovered_design.content_base64) == superseding_bytes
    assert base64.b64decode(recovered_program.content_base64) == program_bytes
    history = service_playbill_document_history(reopened, identity="document:design")
    assert [item.revision for item in history.entries] == [1, 2]
    assert [item.body_digest for item in history.entries] == [
        design_body.digest,
        design_v2_body.digest,
    ]
