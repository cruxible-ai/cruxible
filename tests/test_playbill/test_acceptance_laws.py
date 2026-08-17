"""Family-neutral, historical acceptance-law registry tests."""

from __future__ import annotations

import pytest

from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.laws import (
    DOCUMENT_LAW,
    LINE_LAW,
    PLAYBILL_ACCEPTANCE_LAWS,
    PROCEDURE_LAW,
)


def test_document_law_resolves_from_artifact_tag_and_replays_by_exact_digest() -> None:
    resolved = PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-document-v1")

    assert resolved.coordinate == DOCUMENT_LAW
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=DOCUMENT_LAW.identifier,
            digest=DOCUMENT_LAW.digest,
        )
        == resolved
    )


def test_unknown_or_substituted_acceptance_law_refuses() -> None:
    with pytest.raises(ProposalIntegrityError, match="no acceptance law"):
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-rail-v1")
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced"):
        PLAYBILL_ACCEPTANCE_LAWS.require_historical(
            identifier=DOCUMENT_LAW.identifier,
            digest="sha256:" + "00" * 32,
        )


def test_pc_d_procedure_and_line_laws_are_exact_historical_coordinates() -> None:
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-procedure-v1").coordinate
        == PROCEDURE_LAW
    )
    assert (
        PLAYBILL_ACCEPTANCE_LAWS.resolve_member(artifact_tag="playbill-line-v1").coordinate
        == LINE_LAW
    )
