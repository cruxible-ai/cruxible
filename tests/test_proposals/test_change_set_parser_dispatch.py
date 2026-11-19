"""The one change-set receipt parses only from canonical bytes; a parse is one sealed record."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.content_memo import is_sealed
from cruxible_client.contracts.documents import render_document
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_core.proposals.proposals import evaluate_proposal_tree
from cruxible_core.proposals.settlement import (
    ChangeActorBinding,
    build_change_set_record,
    change_set_path,
    parse_change_set_record,
    render_change_set,
)
from tests.core_support._support import initialize_local
from tests.test_proposals.test_proposals import DOCUMENT_PATH, TIMESTAMP, _shell


@pytest.fixture(scope="module")
def record_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    root: Path = tmp_path_factory.mktemp("changeset-parser")
    instance, _ = initialize_local(root)
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    body = instance.store_document_body(b"# Parser version fixture\n")
    evaluation = evaluate_proposal_tree(
        base_tree=tree,
        current_tree=tree,
        proposed_tree={**tree, DOCUMENT_PATH: render_document(_shell(body.digest))},
        current=base,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )
    assert evaluation.candidate is not None and not evaluation.diagnostics
    record = build_change_set_record(
        evaluation.candidate,
        sequence=1,
        approvals=(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
    )
    assert record.tag == "playbill-changeset-v3"
    return render_change_set(record)


def test_a_valid_record_round_trips_exactly(record_bytes: bytes) -> None:
    record = parse_change_set_record(record_bytes, path="record")
    assert parse_change_set_record(record_bytes, path=change_set_path(record)) == record
    assert render_change_set(record) == record_bytes


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_tag",
        "unknown_tag",
        "null_tag",
        "wrong_tag_type",
        "earlier_v1_tag",
        "earlier_v2_tag",
        "self_digest",
        "candidate_digest",
        "extra_field",
        "pretty_json",
        "missing_lf",
        "duplicate_tag",
        "invalid_json",
        "array",
        "invalid_utf8",
    ],
)
def test_every_noncanonical_or_other_version_record_refuses(
    record_bytes: bytes, mutation: str
) -> None:
    raw = json.loads(record_bytes)
    if mutation == "missing_tag":
        del raw["tag"]
    elif mutation == "unknown_tag":
        raw["tag"] = "playbill-changeset-v404"
    elif mutation == "null_tag":
        raw["tag"] = None
    elif mutation == "wrong_tag_type":
        raw["tag"] = [raw["tag"]]
    elif mutation == "earlier_v1_tag":
        raw["tag"] = "playbill-changeset-v1"
    elif mutation == "earlier_v2_tag":
        raw["tag"] = "playbill-changeset-v2"
    elif mutation == "self_digest":
        raw["changeset_digest"] = "sha256:" + "0" * 64
    elif mutation == "candidate_digest":
        raw["candidate_digest"] = "sha256:" + "0" * 64
    elif mutation == "extra_field":
        raw["unexpected"] = True
    content = canonical_bytes(raw) + b"\n"
    if mutation == "pretty_json":
        content = json.dumps(raw, indent=2).encode() + b"\n"
    elif mutation == "missing_lf":
        content = record_bytes.rstrip(b"\n")
    elif mutation == "duplicate_tag":
        content = b'{"tag":"' + raw["tag"].encode() + b'",' + record_bytes[1:]
    elif mutation == "invalid_json":
        content = record_bytes[:-4]
    elif mutation == "array":
        content = b"[]\n"
    elif mutation == "invalid_utf8":
        content = b'"\xff"'
    with pytest.raises(SettlementIntegrityError, match="generation change-set record is"):
        parse_change_set_record(content, path="changesets/cs-00000000000000000001.json")


def test_a_parse_is_one_sealed_record_shared_by_every_caller(record_bytes: bytes) -> None:
    first = parse_change_set_record(record_bytes, path="record")
    second = parse_change_set_record(bytes(bytearray(record_bytes)), path="record")
    assert first is second and is_sealed(first)
    # Sealed at every depth: nothing a caller does can change what the next one reads.
    with pytest.raises(TypeError):
        first.law_digests.clear()
    with pytest.raises(ValidationError):
        first.sequence = 99  # type: ignore[misc]
    assert render_change_set(second) == record_bytes
    # A copy is the caller's own plain record again.
    copied = first.model_copy(deep=True)
    assert not is_sealed(copied) and type(copied.law_digests) is dict
    copied.law_digests.clear()
    assert render_change_set(parse_change_set_record(record_bytes, path="record")) == record_bytes
    # A different path is its own parse.
    assert parse_change_set_record(record_bytes, path="other") is not first
