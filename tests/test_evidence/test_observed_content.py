"""Observed content identity on Captures: what was seen, by content, whatever the envelope.

A Capture's bytes are its producer's payload, verbatim (D-R25), so what it
observed is its commitment digest: the bytes a workspace.file read, the body an
origin sent, the document a provider made. No operation is decoded to find it,
and the envelope records no second digest.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.captures import (
    CaptureEnvelope,
    build_provider_external_capture_v2,
    observed_content,
)
from cruxible_client.contracts.provider_execution import (
    ProviderInvocationOutputDigest,
    provider_invocation_output_digest,
)
from tests.core_support.p2b4_unit1._support import provider_capture_fixture
from tests.test_integration.p2b4_unit2.test_workspace_capture_receipt import _workspace_fixture


def _build(fixture: Any, **extra: Any) -> Any:
    return build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
        **extra,
    )


def test_a_workspace_capture_observed_the_bytes_it_read(tmp_path: Path) -> None:
    fixture, source_read, output = _workspace_fixture(tmp_path)
    built = _build(fixture, source_read_receipt=source_read, provider_output=output)
    assert built.envelope.commitment.digest == source_read.bytes_digest
    assert observed_content(built.envelope).digest == source_read.bytes_digest


def test_a_provider_capture_observed_its_payload(tmp_path: Path) -> None:
    fixture = provider_capture_fixture(tmp_path)
    built = _build(fixture)
    assert observed_content(built.envelope).digest == built.envelope.commitment.digest
    assert built.envelope.commitment.digest == fixture.result.bytes_digest


def test_the_envelope_records_no_second_content_digest(tmp_path: Path) -> None:
    built = _build(provider_capture_fixture(tmp_path))
    document = built.envelope.model_dump(mode="json")
    assert "observed_content_digest" not in document
    with pytest.raises(ValidationError):
        CaptureEnvelope.model_validate({**document, "observed_content_digest": "sha256:" + "1"})


def test_coverage_groups_two_captures_of_the_same_content_as_one_row(tmp_path: Path) -> None:
    import sqlite3

    from cruxible_client.contracts.captures import capture_contract_digest, capture_contract_path
    from cruxible_client.contracts.captures import render_capture_contract as render_contract
    from cruxible_core.indexes.evidence.citation_coverage import coverage_rows
    from cruxible_core.indexes.evidence.citation_sql import SCHEMA_SQL
    from tests.test_evidence.test_citation_sql import AT, World

    fixture, source_read, output = _workspace_fixture(tmp_path)
    first = _build(fixture, source_read_receipt=source_read, provider_output=output)
    # The same file bytes read by another run: its envelope differs, and both
    # commit the bytes core read exactly, so their content is one.
    second, _reread_receipt = _reread(fixture, source_read, output)
    assert first.capture_digest != second.capture_digest
    assert first.envelope.commitment.digest == second.envelope.commitment.digest
    assert observed_content(first.envelope) == observed_content(second.envelope)

    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE claims(identity TEXT PRIMARY KEY,path TEXT,"
        "artifact_digest TEXT,lifecycle TEXT) STRICT;"
        "CREATE TABLE capture_contracts(identity TEXT PRIMARY KEY,path TEXT,"
        "artifact_digest TEXT) STRICT;" + SCHEMA_SQL
    )
    world = World(connection, fixture.store)
    path = capture_contract_path(fixture.contract.identity.name)
    world.sources[path] = render_contract(fixture.contract)
    connection.execute(
        "INSERT INTO capture_contracts VALUES (?,?,?)",
        (
            fixture.contract.identity.qualified,
            path,
            capture_contract_digest(fixture.contract).tagged,
        ),
    )
    world.publish(world.claim(1, [first.capture_digest]), world.claim(2, [second.capture_digest]))
    by_content, _ = coverage_rows(world.reader, bodies=fixture.store, at=AT, version=2)
    (row,) = by_content.citations
    assert set(row.capture_digests) == {first.capture_digest, second.capture_digest}
    assert row.commitment_digest == source_read.bytes_digest
    by_commitment, _ = coverage_rows(world.reader, bodies=fixture.store, at=AT, version=1)
    assert len(by_commitment.citations) == 1


# -- refusal matching keys a Capture by its content -----------------------------


def _reread(fixture: Any, source_read: Any, output: Any) -> tuple[Any, Any]:
    """The same file bytes read by another run: another envelope, the same bytes."""

    reread = source_read.model_copy(
        update={"derived_request_digest": "sha256:" + hashlib.sha256(b"rerun").hexdigest()}
    )
    structured = {
        **output,
        "source": {**output["source"], "commitment_digest": reread.derived_request_digest},
    }
    receipt = fixture.receipt.model_copy(
        update={
            "output": ProviderInvocationOutputDigest(
                output_digest=provider_invocation_output_digest(structured)
            ).model_dump(mode="json")
        }
    )
    return _build(
        replace(fixture, receipt=receipt),
        source_read_receipt=reread,
        provider_output=structured,
    ), reread


def test_two_captures_of_the_same_bytes_are_one_piece_of_evidence(tmp_path: Path) -> None:
    from cruxible_core.proposals.refusal_index import capture_evidence_content

    fixture, source_read, output = _workspace_fixture(tmp_path)
    built = _build(fixture, source_read_receipt=source_read, provider_output=output)
    again, _reread_receipt = _reread(fixture, source_read, output)
    assert again.capture_digest != built.capture_digest
    assert capture_evidence_content(fixture.store, built.capture_digest) == (
        source_read.bytes_digest
    )
    assert capture_evidence_content(fixture.store, again.capture_digest) == (
        capture_evidence_content(fixture.store, built.capture_digest)
    )


def test_a_provider_capture_keys_by_its_commitment(tmp_path: Path) -> None:
    from cruxible_core.proposals.refusal_index import capture_evidence_content

    fixture = provider_capture_fixture(tmp_path)
    other = _build(fixture)
    assert capture_evidence_content(fixture.store, other.capture_digest) == (
        other.envelope.commitment.digest
    )
