"""One resolution of a stored extract node into the spec the extract engine reads.

A thin extract node names a record set and header fields of its CaptureKind's
layout; core resolves it against the accepted kind, in one place
(``cruxible_core.procedures.extract.extract_spec``), into today's full spec. For
the security kit's four extracts (KEV, EPSS, OSV, SBOM) the resolution is the
node the kit stores today. The OSV and SBOM layouts are wave 0's examples; the
KEV and EPSS layouts here are written from the kit checkout (cruxible-kits
``rc/1.3.0``), and their ``*.resolved.json`` files are its stored nodes.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.capture_layouts import evaluate_layout_law
from cruxible_client.contracts.content_memo import seal
from cruxible_client.contracts.procedures.extract import ExtractNode
from cruxible_client.contracts.procedures.layout_extract import (
    EXTRACT_FORMAT_UNSUPPORTED,
    EXTRACT_LAYOUT_UNDECLARED,
    LayoutExtractNode,
    LayoutExtractRefused,
    resolve_layout_extract,
)
from cruxible_client.contracts.shapes import tree_contract_resolver
from cruxible_core.procedures import extract as core_extract
from cruxible_core.procedures.extract import extract_spec, extract_specs

HERE = Path(__file__).resolve().parent / "layout_extract_examples"
PILOT = Path(__file__).resolve().parents[1] / "pilot_contract_batch" / "examples" / "security"
KIT = Path.home() / "Git" / "cruxible-kits" / "kits" / "security" / "artifacts"

#: (thin node, its kind, the folder holding both, the kit Procedure, reads a list)
CASES = [
    ("security.kev-triage.entries", "security.kev-catalog", HERE, "security.kev-triage", False),
    ("security.epss-triage.scores", "security.epss-scores", HERE, "security.epss-triage", True),
    ("security.osv-query.affected", "security.osv-matches", PILOT, "security.osv-query", False),
    (
        "security.sbom-import.listed",
        "security.sbom-components",
        PILOT,
        "security.sbom-import",
        False,
    ),
]
IDS = [case[0] for case in CASES]


def _wire(raw: dict[str, Any]) -> dict[str, Any]:
    """A stored-wire node as the typed validator reads it (``id`` -> ``node_id``, ``as``)."""

    node = dict(raw)
    node["node_id"] = node.pop("id")
    node.setdefault("as", node["node_id"])
    return node


def _read(folder: Path, name: str, *, kind: bool = False) -> dict[str, Any]:
    """A fixture: wave 0 keeps kinds and extracts in two folders, this test in one."""

    if folder == PILOT:
        folder = folder / ("capture-kinds" if kind else "extracts")
    return json.loads((folder / f"{name}.json").read_bytes())


def _thin(folder: Path, name: str) -> LayoutExtractNode:
    return LayoutExtractNode.model_validate(_wire(_read(folder, name)))


def _stored_kind(folder: Path, name: str, *, layout: Any = None) -> Any:
    declared = _read(folder, name, kind=True)
    return lower_capture_contract_input(
        CaptureContractInputRecord.model_validate(
            {
                "name": declared["name"],
                "shape": "http_response",
                "decoding": declared["decoding"],
                "layout": declared["layout"] if layout is None else layout,
            }
        ),
        named=True,
    )


@pytest.mark.parametrize(("thin", "kind", "folder", "procedure", "reads_list"), CASES, ids=IDS)
def test_core_resolution_is_today_s_stored_node(
    thin: str, kind: str, folder: Path, procedure: str, reads_list: bool
) -> None:
    node = _thin(folder, thin)
    stored_kind = _stored_kind(folder, kind)
    expected = _read(folder, f"{thin}.resolved")

    resolved = extract_spec(node, stored_kind)

    assert resolved == ExtractNode.model_validate(_wire(expected))
    # A list read resolves the same spec; the list rule only refuses (a5).
    assert resolve_layout_extract(node, stored_kind.layout, reads_list=reads_list) == resolved


@pytest.mark.skipif(not KIT.is_dir(), reason="needs the cruxible-kits checkout")
@pytest.mark.parametrize(("thin", "kind", "folder", "procedure", "reads_list"), CASES, ids=IDS)
def test_the_resolved_spec_is_the_kit_s_current_node(
    thin: str, kind: str, folder: Path, procedure: str, reads_list: bool
) -> None:
    definition = json.loads((KIT / "procedures" / f"{procedure}.json").read_bytes())["definition"]
    node_id = _read(folder, thin)["id"]
    (current,) = [n for n in definition["nodes"] if n.get("id") == node_id]
    assert current == _read(folder, f"{thin}.resolved")


@pytest.mark.skipif(not KIT.is_dir(), reason="needs the cruxible-kits checkout")
@pytest.mark.parametrize("kind", ["security.kev-catalog", "security.epss-scores"])
def test_the_kev_and_epss_layouts_pass_the_layout_law_against_the_kit_s_contracts(
    kind: str,
) -> None:
    tree = {
        path.relative_to(KIT).as_posix(): path.read_bytes()
        for path in (KIT / "contracts").rglob("*.json")
    }
    stored = _stored_kind(HERE, kind)
    problems = evaluate_layout_law(
        stored.layout, decoding=stored.decoding, resolve=tree_contract_resolver(tree)
    )
    assert problems == ()


def test_the_list_rule_refuses_a_header_or_a_count_over_a_list() -> None:
    kev = _thin(HERE, "security.kev-triage.entries")
    layout = _stored_kind(HERE, "security.kev-catalog").layout
    with pytest.raises(LayoutExtractRefused) as refused:
        resolve_layout_extract(kev, layout, reads_list=True)
    assert refused.value.code == EXTRACT_FORMAT_UNSUPPORTED
    # Without the header, the record set's count still speaks for one document only.
    with pytest.raises(LayoutExtractRefused) as counted:
        resolve_layout_extract(kev.model_copy(update={"header": ()}), layout, reads_list=True)
    assert counted.value.code == EXTRACT_FORMAT_UNSUPPORTED


def test_a_thin_node_over_a_kind_without_a_layout_is_refused() -> None:
    node = _thin(HERE, "security.kev-triage.entries")
    bare = lower_capture_contract_input(
        CaptureContractInputRecord.model_validate(
            {"name": "security.kev-catalog", "shape": "http_response"}
        ),
        named=True,
    )
    with pytest.raises(LayoutExtractRefused) as refused:
        extract_spec(node, bare)
    assert refused.value.code == EXTRACT_LAYOUT_UNDECLARED
    with pytest.raises(LayoutExtractRefused):
        extract_spec(node, None)


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    original = core_extract.resolve_layout_extract

    def counted(node: Any, layout: Any, **kwargs: Any) -> Any:
        calls.append(node.node_id)
        return original(node, layout, **kwargs)

    monkeypatch.setattr(core_extract, "resolve_layout_extract", counted)
    return calls


def test_a_sealed_node_resolves_once_per_kind_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting(monkeypatch)
    node = _thin(HERE, "security.kev-triage.entries")
    assert seal(node)
    kind = _stored_kind(HERE, "security.kev-catalog")
    call = SimpleNamespace(
        as_="catalog",
        captures={
            "capture": ArtifactNameRef(
                target=ArtifactIdentity(kind="CaptureContract", name="security.kev-catalog")
            )
        },
    )
    looked_up: list[str] = []

    def kind_of(ref: ArtifactNameRef) -> Any:
        looked_up.append(ref.target.name)
        return kind

    # Two runs of the unchanged Procedure: one resolution.
    first = extract_specs((call, node), kind_of)(node)
    second = extract_specs((call, node), kind_of)(node)
    assert first is second
    assert calls == ["entries"]
    assert looked_up == ["security.kev-catalog", "security.kev-catalog"]

    # A successor kind (another digest) is resolved again, once.
    grown = kind.layout.model_dump(mode="json")
    grown["header"]["source"] = {"at": "/source"}
    successor = _stored_kind(HERE, "security.kev-catalog", layout=grown)
    assert extract_spec(node, successor) == first
    assert extract_spec(node, successor) is extract_spec(node, successor)
    assert calls == ["entries", "entries"]


def test_a_sealed_node_remembers_one_resolution_however_many_kinds_it_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.captures import capture_contract_digest
    from cruxible_client.contracts.content_memo import sealed_derive

    calls = _counting(monkeypatch)
    node = _thin(HERE, "security.kev-triage.entries")
    assert seal(node)
    base = _stored_kind(HERE, "security.kev-catalog")
    kinds = []
    for index in range(128):
        grown = base.layout.model_dump(mode="json")
        grown["header"][f"extra_{index}"] = {"at": f"/extra/{index}"}
        kinds.append(_stored_kind(HERE, "security.kev-catalog", layout=grown))
    for kind in kinds:
        extract_spec(node, kind)
    assert len(calls) == 128

    # The node's memo holds one entry: the last kind it read.
    slot = sealed_derive(node, core_extract._SPEC_SLOT_KEY, core_extract._ResolvedSpecSlot)
    assert slot.entry is not None
    assert slot.entry[0] == capture_contract_digest(kinds[-1]).tagged
    assert extract_spec(node, kinds[-1]) is slot.entry[1]
    assert len(calls) == 128
    # An earlier kind is resolved again and replaces the entry.
    extract_spec(node, kinds[0])
    assert len(calls) == 129
    assert slot.entry[0] == capture_contract_digest(kinds[0]).tagged


def test_an_unsealed_node_resolves_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting(monkeypatch)
    node = _thin(HERE, "security.kev-triage.entries")
    kind = _stored_kind(HERE, "security.kev-catalog")
    assert extract_spec(node, kind) == extract_spec(node, kind)
    assert calls == ["entries", "entries"]
