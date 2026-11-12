"""The layout on a CaptureKind: stored bytes, the layout law, and grow-only succession.

A kind's ``layout`` names its documents' header fields and record sets. A kind
without one keeps its exact bytes and digest. A live kind's layout is judged
when it is accepted (rows resolve, every row field has a rule, key types name
SubjectKinds here), and a successor may only grow it. Every refusal reaches the
author as a typed diagnostic on the proposal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.capture_layouts import (
    LAYOUT_DECODING_UNSUPPORTED,
    LAYOUT_FIELD_MISSING,
    LAYOUT_REF_UNRESOLVED,
    LAYOUT_ROW_UNRESOLVED,
    LAYOUT_SUCCESSION_BREAK,
    CaptureLayout,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureDecoding,
    CaptureDecodingLimits,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
    validate_contract,
)
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._support import initialize_local

KITS = Path.home() / "Git" / "cruxible-kits" / "kits"
STAMP = "2026-11-12T12:00:00.000000Z"
JSON = CaptureDecoding(
    format="json", limits=CaptureDecodingLimits(decoded_bytes=1 << 20, depth=16, records=1000)
)
TEXT = CaptureDecoding(
    format="text", limits=CaptureDecodingLimits(decoded_bytes=1 << 20, depth=16, records=1000)
)
KIND = "test.feed"


def _row(version: int = 1, **extra: Any) -> Contract:
    fields: dict[str, Any] = {"title": "string{max_len: 256}", **extra}
    data: dict[str, Any] = {"name": "test.feed-row", "version": version, "fields": fields}
    if version > 1:
        data["predecessor_digest"] = contract_digest(_row(version - 1)).tagged
    return validate_contract(data)


def _pinned(row: Contract) -> dict[str, Any]:
    return {"name": row.name, "version": row.version, "digest": contract_digest(row).tagged}


def _layout(row: Contract, **records: Any) -> dict[str, Any]:
    fields = {name: {"at": name} for name in row.fields}
    return {
        "header": {"total": {"at": "/total", "parse": "int"}},
        "records": {
            "items": {
                "at": "/items/*",
                "key": {"id": {"at": "id"}},
                "row": _pinned(row),
                "fields": fields,
                "count_at": "/total",
            },
            **records,
        },
    }


def _kind(
    layout: dict[str, Any] | None, *, decoding: CaptureDecoding = JSON, predecessor: Any = None
) -> Any:
    record: dict[str, Any] = {"name": KIND}
    if predecessor is None:
        record.update(shape="http_response", decoding=decoding)
    if layout is not None:
        record["layout"] = layout
    return lower_capture_contract_input(
        CaptureContractInputRecord.model_validate(record), predecessor=predecessor, named=True
    )


def _members(kind: Any, *rows: Contract) -> dict[str, bytes]:
    return {
        capture_contract_path(KIND): render_capture_contract(kind),
        **{contract_path(row.name, row.version): render_contract(row) for row in rows},
    }


def _submit(instance: Any, members: dict[str, bytes], name: str) -> Any:
    return submit_member_candidate(
        instance,
        members=members,
        actor_id="owner",
        proposal_name=name,
        proposal_family="capture contract",
        timestamp=STAMP,
    )


def _codes(inspection: Any) -> list[str]:
    return [item.code for item in inspection.proposal.evaluation.diagnostics]


# -- a kind without a layout keeps its bytes -------------------------------------------------


def test_builtin_kinds_keep_their_digests() -> None:
    assert (
        capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
        == "sha256:a69e7edb9d8f4fe5e732119bab48722bfee865bd6d0cdc438021015d1af1e00f"
    )
    assert (
        capture_contract_digest(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT).tagged
        == "sha256:2faeab9f17b1b7d775457b5c07bd316f7b5e49b8d69a057c15b82ce6fe828f71"
    )
    assert (
        capture_contract_digest(foreign_source_capture_contract("x.y")).tagged
        == "sha256:f8ffad07562567f57035eb21066a7a903a3c3978b7bf18b538a23365ba934f3a"
    )


def test_an_authored_kind_without_a_layout_renders_no_layout() -> None:
    rendered = render_capture_contract(_kind(None))
    assert b"layout" not in rendered
    assert (
        render_capture_contract(parse_capture_contract(rendered, path=capture_contract_path(KIND)))
        == rendered
    )


@pytest.mark.skipif(not KITS.is_dir(), reason="needs the cruxible-kits checkout")
def test_every_kit_kind_reads_and_renders_to_its_exact_bytes() -> None:
    files = sorted(KITS.glob("*/artifacts/capture-contracts/*.json"))
    assert files
    for path in files:
        content = path.read_bytes()
        kind = parse_capture_contract(content, path=capture_contract_path(path.stem))
        assert render_capture_contract(kind) == content, path
        assert ("layout" in json.loads(content)) == (getattr(kind, "layout", None) is not None)


def test_a_layout_round_trips_on_the_stored_kind() -> None:
    kind = _kind(_layout(_row()))
    rendered = render_capture_contract(kind)
    back = parse_capture_contract(rendered, path=capture_contract_path(KIND))
    assert back.layout == CaptureLayout.model_validate(_layout(_row()))
    assert render_capture_contract(back) == rendered


# -- the layout law at acceptance --------------------------------------------------------------


def test_a_kind_with_a_layout_is_accepted_and_lands(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    row = _row()
    inspection = _submit(instance, _members(_kind(_layout(row)), row), "layout")
    assert inspection.proposal.candidate is not None, _codes(inspection)
    accept_proposal(instance, owner, inspection)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    landed = parse_capture_contract(
        tree[capture_contract_path(KIND)], path=capture_contract_path(KIND)
    )
    assert set(landed.layout.records) == {"items"}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("row_missing", LAYOUT_ROW_UNRESOLVED),
        ("row_digest", LAYOUT_ROW_UNRESOLVED),
        ("field_missing", LAYOUT_FIELD_MISSING),
        ("text_decoding", LAYOUT_DECODING_UNSUPPORTED),
        ("key_kind", LAYOUT_REF_UNRESOLVED),
    ],
)
def test_the_layout_law_refuses_with_a_typed_diagnostic(
    tmp_path: Path, change: str, code: str
) -> None:
    instance, _owner = initialize_local(tmp_path)
    row = _row(summary="string{max_len: 512}")
    layout = _layout(row)
    rows: tuple[Contract, ...] = (row,)
    decoding = JSON
    if change == "row_missing":
        rows = ()
    elif change == "row_digest":
        layout["records"]["items"]["row"]["digest"] = "sha256:" + "0" * 64
    elif change == "field_missing":
        del layout["records"]["items"]["fields"]["summary"]
    elif change == "text_decoding":
        decoding = TEXT
    elif change == "key_kind":
        layout["records"]["items"]["key"]["id"]["type"] = "key_part<test.absent:id>"
    inspection = _submit(instance, _members(_kind(layout, decoding=decoding), *rows), change)
    assert inspection.proposal.candidate is None
    assert code in _codes(inspection), _codes(inspection)


def test_the_refusal_reaches_the_author_through_kind_authoring(tmp_path: Path) -> None:
    from cruxible_core.service.evidence.capture_contracts import service_propose_capture_contract

    instance, _owner = initialize_local(tmp_path)
    row = _row()
    inspection = service_propose_capture_contract(
        instance,
        input=CaptureContractInputRecord.model_validate(
            {"name": KIND, "shape": "http_response", "decoding": JSON, "layout": _layout(row)}
        ),
        actor_id="owner",
        proposal_name="authored-layout",
        timestamp=STAMP,
    )
    assert inspection.proposal.candidate is None
    (diagnostic,) = inspection.proposal.evaluation.diagnostics
    assert diagnostic.code == LAYOUT_ROW_UNRESOLVED
    assert "test.feed-row@1" in diagnostic.message


# -- succession: a layout may only grow ------------------------------------------------------


def _accepted_world(tmp_path: Path) -> tuple[Any, Any, Any, Contract]:
    instance, owner = initialize_local(tmp_path)
    row = _row()
    first = _kind(_layout(row))
    inspection = _submit(instance, _members(first, row), "first")
    accept_proposal(instance, owner, inspection)
    return instance, owner, first, row


def test_a_successor_may_add_a_record_set_and_a_header_field(tmp_path: Path) -> None:
    instance, _owner, first, row = _accepted_world(tmp_path)
    grown = _layout(
        row, more={"at": "/more/*", "row": _pinned(row), "fields": {"title": {"at": "t"}}}
    )
    grown["header"]["source"] = {"at": "/source"}
    inspection = _submit(instance, _members(_kind(grown, predecessor=first)), "grow")
    assert inspection.proposal.candidate is not None, _codes(inspection)


def test_a_successor_may_move_a_row_to_a_version_that_adds_an_optional_field(
    tmp_path: Path,
) -> None:
    instance, _owner, first, row = _accepted_world(tmp_path)
    moved = _row(2, note={"type": "string{max_len: 64}", "optional": True})
    layout = _layout(moved)
    inspection = _submit(instance, _members(_kind(layout, predecessor=first), moved), "move-row")
    assert inspection.proposal.candidate is not None, _codes(inspection)


@pytest.mark.parametrize(
    ("change", "entry"),
    [
        ("path", "records.items.at"),
        ("count", "records.items.count_at"),
        ("key", "records.items.key"),
        ("header", "header.total"),
        ("record_set", "records.items"),
        ("required_row_field", "records.items.row.note"),
    ],
)
def test_a_successor_that_changes_an_entry_is_refused_naming_it(
    tmp_path: Path, change: str, entry: str
) -> None:
    instance, _owner, first, row = _accepted_world(tmp_path)
    layout = _layout(row)
    rows: tuple[Contract, ...] = ()
    items = layout["records"]["items"]
    if change == "path":
        items["at"] = "/entries/*"
    elif change == "count":
        items["count_at"] = "/size"
    elif change == "key":
        items["key"]["id"]["at"] = "uid"
    elif change == "header":
        layout["header"]["total"] = {"at": "/sum", "parse": "int"}
    elif change == "record_set":
        layout["records"]["others"] = layout["records"].pop("items")
    elif change == "required_row_field":
        moved = _row(2, note="string{max_len: 64}")
        layout = _layout(moved)
        rows = (moved,)
    inspection = _submit(instance, _members(_kind(layout, predecessor=first), *rows), change)
    assert inspection.proposal.candidate is None
    diagnostics = inspection.proposal.evaluation.diagnostics
    assert [item.code for item in diagnostics] == [LAYOUT_SUCCESSION_BREAK], diagnostics
    assert f"layout entry {entry} changes" in diagnostics[0].message
    assert "declare a new kind" in diagnostics[0].message


def test_an_accepted_kind_without_a_layout_grows_one_by_kind_authoring(tmp_path: Path) -> None:
    from cruxible_core.service.evidence.capture_contracts import service_propose_capture_contract

    instance, owner = initialize_local(tmp_path)
    row = _row()
    bare = _kind(None)
    accept_proposal(instance, owner, _submit(instance, _members(bare, row), "bare"))

    inspection = service_propose_capture_contract(
        instance,
        input=CaptureContractInputRecord.model_validate({"name": KIND, "layout": _layout(row)}),
        actor_id="owner",
        proposal_name="grow-layout",
        timestamp=STAMP,
    )
    assert inspection.proposal.candidate is not None, _codes(inspection)
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    grown = parse_capture_contract(
        tree[capture_contract_path(KIND)], path=capture_contract_path(KIND)
    )
    assert grown.layout == CaptureLayout.model_validate(_layout(row))
    assert grown.decoding == bare.decoding
    assert grown.lifecycle.predecessor_digest == capture_contract_digest(bare).tagged


# -- key types in a layout follow the Contract key-type rule ---------------------------------


def _keyed_vocabulary(instance: Any, owner: Any) -> None:
    """Live keyed ``test.item`` (slug) and composite ``test.pair`` (slug, region), accepted."""

    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy
    from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
    from tests.core_support._subject_kind_support import keyed_subject_kind
    from tests.support.claim_types import claim_type

    kinds = (
        keyed_subject_kind("test.item", ("test.item.slug",)),
        keyed_subject_kind("test.pair", ("test.pair.slug", "test.pair.region")),
    )
    keys = [
        claim_type(
            predicate=predicate,
            allowed_subject_kinds=[kind],
            value_type={"type": "string"},
            default_role="observation",
            evidence_requirement="none",
            evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
        )
        for kind, predicate in (
            ("test.item", "test.item.slug"),
            ("test.pair", "test.pair.slug"),
            ("test.pair", "test.pair.region"),
        )
    ]
    members = {
        **{subject_kind_path(k.identity.name): render_subject_kind(k) for k in kinds},
        **{claim_type_path(t.predicate): render_claim_type(t) for t in keys},
    }
    accept_proposal(instance, owner, _submit(instance, members, "keyed-vocabulary"))


def _retire_item_kind(instance: Any, owner: Any) -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subject_kinds import (
        parse_subject_kind,
        render_subject_kind,
        subject_kind_digest,
        subject_kind_path,
    )

    path = subject_kind_path("test.item")
    live = parse_subject_kind(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    retired = live.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=subject_kind_digest(live).tagged
            )
        }
    )
    inspection = _submit(instance, {path: render_subject_kind(retired)}, "retire-item")
    accept_proposal(instance, owner, inspection)


@pytest.mark.parametrize(
    ("case", "entry", "problem"),
    [
        ("header_absent_kind", "header.owner", "no live SubjectKind declares test.absent"),
        ("retired_record_key", "records.items.key.id", "no live SubjectKind declares test.item"),
        ("nested_key_type", "header.owners", "'missing' is not a key of test.item"),
    ],
)
def test_every_layout_key_type_names_a_live_keyed_subject_kind(
    tmp_path: Path, case: str, entry: str, problem: str
) -> None:
    instance, owner = initialize_local(tmp_path)
    _keyed_vocabulary(instance, owner)
    row = _row()
    layout = _layout(row)
    if case == "header_absent_kind":
        layout["header"]["owner"] = {"at": "/owner", "type": "key<test.absent>"}
    elif case == "retired_record_key":
        _retire_item_kind(instance, owner)
        layout["records"]["items"]["key"]["id"]["type"] = "key_part<test.item:slug>"
    elif case == "nested_key_type":
        layout["header"]["owners"] = {
            "at": "/owners",
            "type": "list<key_part<test.item:missing>>{max_items: 10}",
        }
    inspection = _submit(instance, _members(_kind(layout), row), case)

    assert inspection.proposal.candidate is None
    diagnostics = inspection.proposal.evaluation.diagnostics
    assert [item.code for item in diagnostics] == [LAYOUT_REF_UNRESOLVED], diagnostics
    assert diagnostics[0].message.startswith(f"{entry}: ")
    assert problem in diagnostics[0].message


def test_a_composite_kind_s_whole_key_is_accepted_and_reads_both_parts(tmp_path: Path) -> None:
    from cruxible_core.indexes.subject_identity import tree_key_rules

    instance, owner = initialize_local(tmp_path)
    _keyed_vocabulary(instance, owner)
    row = _row()
    layout = _layout(row)
    layout["records"]["items"]["key"]["id"]["type"] = "key<test.pair>"
    inspection = _submit(instance, _members(_kind(layout), row), "composite-key")
    assert inspection.proposal.candidate is not None, _codes(inspection)
    accept_proposal(instance, owner, inspection)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    rules = tree_key_rules(tree)
    canonical = rules.canonical("test.pair", None, {"slug": "alpha", "region": "east"})
    assert canonical is not None
