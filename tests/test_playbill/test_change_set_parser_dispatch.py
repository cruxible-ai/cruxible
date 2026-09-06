"""Version dispatch retains ordinary-union bytes, refusals, and fresh outputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.documents import render_document
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_core.playbill import settlement
from cruxible_core.playbill.proposals import evaluate_proposal_tree
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    ChangeSetRecordAnyVersion,
    build_change_set_record,
    change_set_path,
    parse_change_set_record,
    render_change_set,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_proposals import DOCUMENT_PATH, TIMESTAMP, _shell


@pytest.fixture(scope="module", params=(1, 2, 3))
def record_bytes(tmp_path_factory: pytest.TempPathFactory, request) -> bytes:
    root: Path = tmp_path_factory.mktemp(f"changeset-parser-v{request.param}")
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
        wire_version=f"playbill-validated-candidate-v{request.param}",
    )
    assert evaluation.candidate is not None and not evaluation.diagnostics
    record = build_change_set_record(
        evaluation.candidate,
        sequence=1,
        approvals=(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
    )
    assert record.tag == f"playbill-changeset-v{request.param}"
    return render_change_set(record)


def _ordinary_parse(content: bytes, *, path: str) -> ChangeSetRecordAnyVersion:
    """The prior parser, kept as an independent outcome/refusal oracle."""
    adapter: TypeAdapter[ChangeSetRecordAnyVersion] = TypeAdapter(ChangeSetRecordAnyVersion)
    try:
        record = adapter.validate_json(content)
    except (ValueError, ValidationError) as exc:
        raise SettlementIntegrityError(f"generation change-set record is invalid: {path}") from exc
    if render_change_set(record) != content:
        raise SettlementIntegrityError(f"generation change-set record is not canonical: {path}")
    return record


def _refusal(parser, content: bytes):
    with pytest.raises(SettlementIntegrityError) as caught:
        parser(content, path="changesets/cs-00000000000000000001.json")
    cause = caught.value.__cause__
    return (
        type(caught.value),
        str(caught.value),
        type(cause),
        # repr retains invalid input bytes and normalizes ValueError objects in
        # validator context without trying to serialize malformed UTF-8 as JSON.
        repr(cause.errors(include_url=False)) if isinstance(cause, ValidationError) else str(cause),
    )


def test_valid_tag_uses_only_selected_version_and_matches_ordinary_parser(
    record_bytes: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = _ordinary_parse(record_bytes, path="record")

    def unexpected(*args, **kwargs):
        pytest.fail("a valid tagged record must not validate the ordinary union")

    monkeypatch.setattr(settlement._CHANGE_SET_RECORD_ADAPTER, "validate_json", unexpected)
    actual = parse_change_set_record(record_bytes, path=change_set_path(expected))
    assert type(actual) is type(expected)
    assert actual == expected
    assert render_change_set(actual) == record_bytes


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_tag",
        "unknown_tag",
        "null_tag",
        "wrong_tag_type",
        "other_known_tag",
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
def test_refusals_match_original_union(record_bytes: bytes, mutation: str) -> None:
    raw = json.loads(record_bytes)
    if mutation == "missing_tag":
        del raw["tag"]
    elif mutation == "unknown_tag":
        raw["tag"] = "playbill-changeset-v404"
    elif mutation == "null_tag":
        raw["tag"] = None
    elif mutation == "wrong_tag_type":
        raw["tag"] = [raw["tag"]]
    elif mutation == "other_known_tag":
        raw["tag"] = (
            "playbill-changeset-v1" if raw["tag"].endswith("v3") else "playbill-changeset-v3"
        )
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
    assert _refusal(parse_change_set_record, content) == _refusal(_ordinary_parse, content)


def test_outputs_are_independent_mutable_models(record_bytes: bytes) -> None:
    first = parse_change_set_record(record_bytes, path="record")
    second = parse_change_set_record(record_bytes, path="record")
    assert first is not second and first.law_digests is not second.law_digests
    first.law_digests.clear()
    assert render_change_set(second) == record_bytes
    assert render_change_set(parse_change_set_record(record_bytes, path="record")) == record_bytes
