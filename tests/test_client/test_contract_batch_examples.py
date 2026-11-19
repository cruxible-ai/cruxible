"""The contract batch's example artifacts are valid against the wave 0 models.

``tests/pilot_contract_batch/examples`` holds what the maintainer reads: the
capabilities and their Contracts (``base/`` and ``security/``, laid out as kit
artifacts), CaptureKind layouts, a schema 3 registration for the OSV provider,
the thin extract nodes of the OSV and SBOM Procedures with the full specs they
resolve to, and a kit manifest fragment that requires ``base``.

The ``*.resolved.json`` files are the stored nodes of today's security kit,
copied unchanged from ``kits/security/artifacts/procedures/security.osv-query.json``
(definition digest ``sha256:3ea98153…``) and ``security.sbom-import.json``
(``sha256:1a215cc2…``) in cruxible-kits ``rc/1.3.0``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from cruxible_client.contracts.canonical import pretty_canonical_bytes
from cruxible_client.contracts.capabilities import (
    Capability,
    capability_digest,
    capability_path_parts,
    evaluate_capability_law,
    parse_capability,
)
from cruxible_client.contracts.capture_layouts import (
    DeclaredCaptureKind,
    declared_capture_kind_digest,
    evaluate_layout_law,
)
from cruxible_client.contracts.kit_requires import KitRequires
from cruxible_client.contracts.kits import KitManifest
from cruxible_client.contracts.procedures.extract import ExtractNode
from cruxible_client.contracts.procedures.layout_extract import (
    LayoutExtractNode,
    resolve_layout_extract,
)
from cruxible_client.contracts.provider_registration import (
    BuiltAgainstCapability,
    PackageRegistration,
    evaluate_registration_law,
)
from cruxible_client.contracts.shapes import (
    CaptureType,
    IntType,
    ListType,
    contract_digest,
    contract_path_parts,
    parse_contract,
    render_contract,
    tree_contract_resolver,
)
from cruxible_client.contracts.workspace_file import MAX_WORKSPACE_GLOB_FILES

EXAMPLES = Path(__file__).resolve().parents[1] / "pilot_contract_batch" / "examples"
KITS = ("base", "security")
BUILTIN = frozenset({"workspace.file", "workspace.files"})


def _tree(kit: str) -> dict[str, bytes]:
    root = EXAMPLES / kit
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.json")}


def _capabilities(kit: str) -> dict[str, Capability]:
    return {
        path: parse_capability(content, path=path)
        for path, content in _tree(kit).items()
        if capability_path_parts(path) is not None
    }


def _kind(kit: str, name: str) -> DeclaredCaptureKind:
    return DeclaredCaptureKind.model_validate_json(
        (EXAMPLES / kit / "capture-kinds" / f"{name}.json").read_bytes()
    )


def _stored_node(raw: dict[str, Any]) -> dict[str, Any]:
    """A stored-wire node as the typed validator reads it (``id`` -> ``node_id``, ``as``)."""

    node = dict(raw)
    node["node_id"] = node.pop("id")
    node.setdefault("as", node["node_id"])
    return node


def test_the_examples_hold_the_capabilities_the_batch_names() -> None:
    names = {capability.ref.spelling for kit in KITS for capability in _capabilities(kit).values()}

    assert names == {
        "security.osv.match@1",
        "security.osv.query@1",
        "security.sbom.components@1",
        "web.fetch@1",
        "web.search@1",
        "web.search.results@1",
        "workspace.file@1",
        "workspace.files@1",
    }


def test_file_reading_is_one_path_or_one_bounded_glob() -> None:
    """Ruling O2: ``workspace.file`` reads one path; ``workspace.files`` reads a glob's matches."""

    resolve = tree_contract_resolver(_tree("base"))
    capabilities = {item.name: item for item in _capabilities("base").values()}
    one, many = capabilities["workspace.file"], capabilities["workspace.files"]
    one_in, one_out = resolve(one.in_), resolve(one.out)
    many_in, many_out = resolve(many.in_), resolve(many.out)
    assert one_in and one_out and many_in and many_out
    assert (one.effect, many.effect) == ("pure", "pure")

    assert "glob" not in one_in.fields and "max_files" not in one_in.fields
    assert not one_in.fields["relative_path"].optional
    assert list(one_out.fields) == ["capture"]
    assert one_out.fields["capture"].type == CaptureType(capture_kind="*")

    assert "relative_path" not in many_in.fields
    assert not many_in.fields["glob"].optional
    cap = many_in.fields["max_files"].type
    assert isinstance(cap, IntType) and cap.max == MAX_WORKSPACE_GLOB_FILES
    assert list(many_out.fields) == ["captures"]
    assert many_out.fields["captures"].type == ListType(
        item=CaptureType(capture_kind="*"), max_items=MAX_WORKSPACE_GLOB_FILES
    )


@pytest.mark.parametrize("kit", KITS)
def test_every_contract_is_canonical_at_its_path(kit: str) -> None:
    contracts = {path: content for path, content in _tree(kit).items() if contract_path_parts(path)}

    assert contracts
    for path, content in contracts.items():
        assert render_contract(parse_contract(content, path=path)) == content


@pytest.mark.parametrize("kit", KITS)
def test_every_capability_passes_the_law_in_its_kit(kit: str) -> None:
    tree = _tree(kit)
    for path, capability in _capabilities(kit).items():
        assert capability.description[0].isupper()
        assert (
            evaluate_capability_law(
                capability,
                path=path,
                lookup=tree.get,
                parent_content=None,
                content=tree[path],
                builtin_capabilities=BUILTIN,
            )
            == ()
        ), path


@pytest.mark.parametrize(
    ("kit", "name"),
    [
        ("security", "security.osv-matches"),
        ("security", "security.sbom-components"),
        ("base", "web.search-results"),
    ],
)
def test_every_layout_is_canonical_and_passes_the_layout_law(kit: str, name: str) -> None:
    content = (EXAMPLES / kit / "capture-kinds" / f"{name}.json").read_bytes()
    kind = _kind(kit, name)

    assert kind.name == name and kind.layout is not None
    assert pretty_canonical_bytes(kind.model_dump(mode="json")) == content
    resolve = tree_contract_resolver(_tree(kit))
    assert evaluate_layout_law(kind.layout, decoding=kind.decoding, resolve=resolve) == ()


@pytest.mark.parametrize(
    ("thin", "kind"),
    [
        ("security.osv-query.affected", "security.osv-matches"),
        ("security.sbom-import.listed", "security.sbom-components"),
    ],
)
def test_the_thin_extract_resolves_to_today_s_stored_node(thin: str, kind: str) -> None:
    extracts = EXAMPLES / "security" / "extracts"
    node = LayoutExtractNode.model_validate(
        _stored_node(json.loads((extracts / f"{thin}.json").read_bytes()))
    )
    stored = json.loads((extracts / f"{thin}.resolved.json").read_bytes())

    resolved = resolve_layout_extract(node, _kind("security", kind).layout)

    assert resolved == ExtractNode.model_validate(_stored_node(stored))


def test_the_registration_is_schema_3_and_passes_the_law() -> None:
    registration = PackageRegistration.model_validate_json(
        (EXAMPLES / "providers" / "security-osv-provider" / "registration.json").read_bytes()
    )

    assert evaluate_registration_law(registration) == ()
    assert [item.name for item in registration.operations] == [
        "osv.match",
        "osv.query",
        "sbom.components",
    ]


def test_the_registration_carries_byte_copies_and_records_what_it_copied() -> None:
    registration = PackageRegistration.model_validate_json(
        (EXAMPLES / "providers" / "security-osv-provider" / "registration.json").read_bytes()
    )
    security = _tree("security")
    capabilities = {item.name: item for item in _capabilities("security").values()}

    for contract in registration.declares.contracts:
        path = f"contracts/{contract.name}/v{contract.version}.json"
        if path in security:
            assert render_contract(contract) == security[path], path
    for kind in registration.declares.capture_kinds:
        if kind.layout is not None:
            assert kind == _kind("security", kind.name)
    recorded = {item.name: item for item in registration.declares.built_against}
    for name, capability in capabilities.items():
        entry = recorded[name]
        assert isinstance(entry, BuiltAgainstCapability)
        assert entry.digest == capability_digest(capability).tagged
    for kind in registration.declares.capture_kinds:
        assert recorded[kind.name].digest == declared_capture_kind_digest(kind).tagged

    # A copy byte for byte is the fast path: the operation's own Contracts are the
    # capability's. The SBOM derive accepts more, under its own request Contract.
    operations = {item.name: item for item in registration.operations}
    match = capabilities["security.osv.match"]
    resolve = registration.declares.lookup()
    for side in ("in_", "out"):
        carried = resolve(getattr(operations["osv.match"], side))
        assert carried is not None
        assert contract_digest(carried).tagged == getattr(match, side).digest
    assert operations["sbom.components"].in_.name == "sbom.components.request"


def test_the_manifest_fragment_requires_base() -> None:
    fragment = json.loads((EXAMPLES / "security" / "cruxible-kit.fragment.json").read_bytes())
    requires = TypeAdapter(KitRequires).validate_python(fragment.pop("requires"))

    assert [(item.kit_id, item.min_version) for item in requires] == [("base", "1.0.0")]
    manifest = KitManifest.model_validate({**fragment, "artifacts": []})
    assert (manifest.kit_id, manifest.version, manifest.owns) == (
        "security",
        "1.3.0",
        ("security.",),
    )
