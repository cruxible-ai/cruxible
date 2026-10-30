"""Kits carry immutable Contract versions through ordinary governed services."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitArtifactBytes,
    KitBuildRequest,
    KitBundle,
    KitRemoveRequest,
)
from cruxible_client.contracts.shapes import (
    Contract,
    all_field_refs,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_core.claims.artifact_references import move_references, referenced_digests
from cruxible_core.errors import RequestRefusedError
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.kits import build_kit, service_add_kit, service_remove_kit
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import initialize_local

STAMP = "2026-10-29T12:00:00.000000Z"


def _world(root: Path) -> PlaybillInstance:
    root.mkdir()
    instance, _ = initialize_local(root)
    return instance


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))


def _files(*contracts: Contract) -> dict[str, bytes]:
    return {contract_path(c.name, c.version): render_contract(c) for c in contracts}


def _scalar(name: str = "acme.value", **updates: object) -> Contract:
    return Contract.model_validate(
        {"name": name, "version": 1, "fields": {"n": "int{0..100}"}, **updates}
    )


def _build(tree: dict[str, bytes], version: str = "1.0.0") -> KitBundle:
    return build_kit(tree, KitBuildRequest(kit_id="acme", version=version, owns=("acme.",)))


def _add(instance: PlaybillInstance, bundle: KitBundle, **options: object):
    return service_add_kit(
        instance,
        KitAddRequest(bundle=bundle, dry_run=False, **options),
        actor_id="owner",
        timestamp=STAMP,
    )


def test_scalar_export_add_readd_remove_preserves_exact_bytes(tmp_path: Path) -> None:
    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    literal = "sha256:" + "a" * 64
    contract = _scalar(
        description=literal,
        fields={"value": {"type": {"kind": "enum", "members": [literal]}, "description": literal}},
    )
    files = _files(contract)
    accept_tree(publisher, files, name="scalar")
    bundle = _build(_tree(publisher))
    assert bundle.contents() == files
    path, content = next(iter(files.items()))
    assert "lifecycle" not in json.loads(content)
    payload = json.loads(content)
    assert list(referenced_digests(path, payload)) == []
    assert move_references(path, payload, {literal: "sha256:" + "b" * 64}) == payload
    # Ordinary inventory read initializes the fresh local proposal index before preview.
    service_list_playbill_proposals(consumer)
    before = _tree(consumer)
    preview = service_add_kit(
        consumer, KitAddRequest(bundle=bundle), actor_id="owner", timestamp=STAMP
    )
    assert preview.status == "would_propose"
    assert _tree(consumer) == before
    assert _add(consumer, bundle).status == "accepted"
    assert _add(consumer, bundle).status == "unchanged"
    removed = service_remove_kit(
        consumer, KitRemoveRequest(kit_id="acme", dry_run=False), actor_id="owner", timestamp=STAMP
    )
    assert removed.status == "accepted"
    assert [(p.path, p.action) for p in removed.plan] == [(path, "keep")]
    assert _tree(consumer)[path] == content
    readded = _add(consumer, bundle)
    assert readded.status == "accepted"
    assert [(p.path, p.action) for p in readded.plan] == [(path, "unchanged")]
    assert _tree(consumer)[path] == content


def _nested() -> tuple[Contract, ...]:
    leaves = tuple(_scalar(f"other.leaf{i}") for i in range(8))
    refs = [
        c.ref.model_copy(update={"digest": contract_digest(c).tagged}).model_dump(mode="json")
        for c in leaves
    ]
    types = {
        "nested": {"kind": "contract", "contract": refs[0]},
        "record": {"kind": "record", "contract": refs[1]},
        "result": {"kind": "result", "contract": refs[2]},
        "refusal": {"kind": "refusal", "contract": refs[3]},
        "variant": {"kind": "variant", "arms": {"one": refs[4], "two": refs[5]}},
        "list": {
            "kind": "list",
            "item": {"kind": "contract", "contract": refs[6]},
            "max_items": 10,
        },
        "field_result": {
            "kind": "field_result",
            "value": {
                "kind": "list",
                "item": {"kind": "contract", "contract": refs[7]},
                "max_items": 2,
            },
        },
    }
    root = Contract.model_validate(
        {
            "name": "acme.bundle",
            "version": 1,
            "fields": {name: {"type": t} for name, t in types.items()},
        }
    )
    return (*leaves, root)


def test_all_typed_nested_pins_travel_with_real_build_and_add(tmp_path: Path) -> None:
    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    contracts = _nested()
    files = _files(*contracts)
    accept_tree(publisher, files, name="nested")
    bundle = _build(_tree(publisher))
    assert bundle.contents() == files
    root = contracts[-1]
    path = contract_path(root.name, root.version)
    digests = [ref.digest for ref in all_field_refs(root)]
    assert len(digests) == len(set(digests)) == 8
    assert sorted(referenced_digests(path, json.loads(files[path]))) == sorted(digests)
    for digest in digests:
        with pytest.raises(RequestRefusedError) as caught:
            move_references(path, json.loads(files[path]), {digest: "sha256:" + "f" * 64})
        assert caught.value.error_code == "cruxible.contract.immutable"
        assert caught.value.repair is not None
    assert _add(consumer, bundle).status == "accepted"
    assert all(_tree(consumer)[p] == raw for p, raw in files.items())
    assert _add(consumer, bundle).status == "unchanged"


def test_predecessor_versions_remain_exact_and_cannot_be_remapped(tmp_path: Path) -> None:
    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    # None of these historical versions is an owned root: only the wrapper is.
    versions = [_scalar("other.history")]
    for version in (2, 3):
        versions.append(
            _scalar(
                "other.history",
                version=version,
                predecessor_digest=contract_digest(versions[-1]).tagged,
            )
        )
    for c in versions:
        accept_tree(publisher, _files(c), name=f"v{c.version}")
    wrapper = Contract.model_validate(
        {
            "name": "acme.wrapper",
            "version": 1,
            "fields": {
                "history": {
                    "type": {
                        "kind": "contract",
                        "contract": {
                            "name": "other.history",
                            "version": 3,
                            "digest": contract_digest(versions[-1]).tagged,
                        },
                    }
                }
            },
        }
    )
    accept_tree(publisher, _files(wrapper), name="wrapper")
    files = _files(*versions, wrapper)
    bundle = _build(_tree(publisher))
    assert bundle.contents() == files
    path = contract_path(versions[-1].name, 3)
    predecessor = versions[-1].predecessor_digest
    assert list(referenced_digests(path, json.loads(files[path]))) == [predecessor]
    with pytest.raises(RequestRefusedError, match="cruxible.contract.immutable"):
        move_references(path, json.loads(files[path]), {predecessor: "sha256:" + "f" * 64})
    assert _add(consumer, bundle).status == "accepted"
    assert all(_tree(consumer)[p] == raw for p, raw in files.items())


@pytest.mark.parametrize("predecessor", [False, True])
def test_missing_exact_closure_refuses_export_and_add(tmp_path: Path, predecessor: bool) -> None:
    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    first = _scalar()
    contracts = (
        (first, _scalar(version=2, predecessor_digest=contract_digest(first).tagged))
        if predecessor
        else _nested()
    )
    for c in contracts:
        accept_tree(publisher, _files(c), name=f"missing-{c.name}-{c.version}")
    complete = _build(_tree(publisher))
    missing = contract_path(contracts[0].name, contracts[0].version)
    broken = _tree(publisher)
    del broken[missing]
    code = (
        "cruxible.contract.predecessor_missing"
        if predecessor
        else "cruxible.contract.pin_unresolved"
    )
    with pytest.raises(RequestRefusedError) as caught:
        _build(broken)
    assert caught.value.error_code == code
    contents = {p: c for p, c in complete.contents().items() if p != missing}
    bundle = complete.model_copy(
        update={
            "manifest": complete.manifest.model_copy(
                update={
                    "artifacts": tuple(a for a in complete.manifest.artifacts if a.path != missing)
                }
            ),
            "artifacts": tuple(KitArtifactBytes.of(p, c) for p, c in contents.items()),
        }
    )
    before = _tree(consumer)
    with pytest.raises(RequestRefusedError) as caught:
        _add(consumer, bundle)
    assert caught.value.error_code == code
    assert _tree(consumer) == before


@pytest.mark.parametrize(
    "options", [{}, {"keep_local_edits": True}, {"keep": ("Contract:acme.value/v1",)}]
)
def test_same_version_collision_cannot_be_overwritten_or_kept(
    tmp_path: Path, options: dict
) -> None:
    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    accept_tree(publisher, _files(_scalar()), name="publisher")
    held = _files(_scalar(description="different immutable version"))
    accept_tree(consumer, held, name="consumer")
    before = _tree(consumer)
    with pytest.raises(RequestRefusedError, match="cruxible.contract.immutable"):
        _add(consumer, _build(_tree(publisher)), **options)
    assert _tree(consumer) == before


def test_dropped_contract_stays_even_when_retire_dependents_is_requested(tmp_path: Path) -> None:
    publisher = _world(tmp_path / "publisher")
    consumer = _world(tmp_path / "consumer")
    files = _files(_scalar(), _scalar("acme.other"))
    accept_tree(publisher, files, name="both")
    assert _add(consumer, _build(_tree(publisher))).status == "accepted"
    reduced = _tree(publisher)
    path = contract_path("acme.value", 1)
    del reduced[path]
    result = _add(consumer, _build(reduced, "1.1.0"), retire_dependents=("Contract:acme.value/v1",))
    assert result.status == "accepted"
    assert next(p for p in result.plan if p.path == path).action == "keep"
    assert _tree(consumer)[path] == files[path]
