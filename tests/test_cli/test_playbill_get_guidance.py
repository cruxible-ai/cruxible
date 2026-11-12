"""`cruxible get` cards show guidance and links (D-R17): bounded, absent when none."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.documents import DocumentLink
from cruxible_client.contracts.get_reads import GET_CARD_LINKS, GetRequest, GetResult
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_cli.test_playbill_write import (  # noqa: F401  (autouse fixture)
    _isolated_context,
    _run,
)
from tests.test_service.test_backlinks import STATUS, propose_document

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


class _Reads:
    """Answers ``get`` from the real service over one instance."""

    def __init__(self, instance: Any) -> None:
        self.instance = instance

    def get(self, instance_id: str, *, request: GetRequest) -> GetResult:
        assert instance_id == "inst_write"
        return service_playbill_get(self.instance, request=request, access=_ACCESS)


def _serve(monkeypatch: pytest.MonkeyPatch, instance: Any) -> None:
    client = _Reads(instance)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)


@pytest.fixture
def instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    (tmp_path / "instance").mkdir()
    seeded, _owner = seed_write_surface(tmp_path / "instance")
    _serve(monkeypatch, seeded)
    return seeded


def _guides(target: str) -> DocumentLink:
    return DocumentLink(relation="guides", target_identity=target)


def _text(*args: str) -> str:
    result = _run("get", *args)
    assert result.exit_code == 0, result.output
    return str(result.output)


def _json(*args: str) -> dict[str, Any]:
    card: dict[str, Any] = json.loads(_text(*args, "--json"))["card"]
    return card


def _assert_guidance_text(ref: str, documents: list[str], *, cut: int | None) -> None:
    text = _text(ref)
    assert "guidance:\n" + "".join(f"  {name}  Guidance {name[9:]}\n" for name in documents) in text
    card = _json(ref)
    assert [item["document"] for item in card["guidance"]] == documents
    if cut is None:
        assert "guidance_total" not in card and "next: cruxible backlinks" not in text
    else:
        assert card["guidance_total"] == cut
        assert f"guidance_total: {cut}\n" in text
        assert f"next: cruxible backlinks {ref}\n" in text


def test_get_claim_type_prints_its_guidance_bounded(instance: Any) -> None:
    ref = f"ClaimType:{STATUS}"
    bare = _text(ref)
    assert "guidance" not in bare and "backlinks" not in bare
    assert "guidance" not in _json(ref)

    propose_document(instance, "status-review", _guides(ref))
    _assert_guidance_text(ref, ["Document:status-review"], cut=None)

    for index in range(GET_CARD_LINKS):
        propose_document(instance, f"status-review-{index}", _guides(ref))
    shown = sorted(
        ["status-review", *(f"status-review-{index}" for index in range(GET_CARD_LINKS))],
        key=lambda name: f"document:{name}".encode(),
    )[:GET_CARD_LINKS]
    _assert_guidance_text(ref, [f"Document:{name}" for name in shown], cut=GET_CARD_LINKS + 1)


def test_get_document_prints_its_links_bounded(instance: Any) -> None:
    propose_document(instance, "plain")
    bare = _text("Document:plain")
    assert "links" not in bare and "--detail proof" not in bare
    assert "links" not in _json("Document:plain")

    propose_document(
        instance,
        "status-review",
        _guides(f"ClaimType:{STATUS}"),
        DocumentLink(relation="about", target_identity=f"Subject:{KIND}/wi-1"),
    )
    text = _text("Document:status-review")
    assert f"links:\n  about  Subject:{KIND}/wi-1\n  guides  ClaimType:{STATUS}\n" in text
    assert "links_total" not in text and "--detail proof" not in text
    assert _json("Document:status-review")["links"] == [
        {"relation": "about", "target": f"Subject:{KIND}/wi-1"},
        {"relation": "guides", "target": f"ClaimType:{STATUS}"},
    ]

    propose_document(
        instance,
        "many",
        *(
            DocumentLink(relation="see-also", target_identity=f"document:other-{index}")
            for index in range(GET_CARD_LINKS + 1)
        ),
    )
    many = _text("Document:many")
    assert (
        "links:\n"
        + "".join(f"  see-also  document:other-{index}\n" for index in range(GET_CARD_LINKS))
        in many
    )
    assert f"links_total: {GET_CARD_LINKS + 1}\n" in many
    assert "next: cruxible get Document:many --detail proof\n" in many
    assert len(_json("Document:many")["links"]) == GET_CARD_LINKS


def test_get_procedure_prints_its_guidance_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.test_procedures.test_procedure_run_surface import _world

    world, _owner, procedure = _world(tmp_path)
    _serve(monkeypatch, world)
    ref = f"Procedure:{procedure.identity.name}"
    bare = _text(ref)
    assert "guidance" not in bare and "backlinks" not in bare

    propose_document(world, "run-review", _guides(ref))
    _assert_guidance_text(ref, ["Document:run-review"], cut=None)

    for index in range(GET_CARD_LINKS):
        propose_document(world, f"run-review-{index}", _guides(ref))
    shown = sorted(
        ["run-review", *(f"run-review-{index}" for index in range(GET_CARD_LINKS))],
        key=lambda name: f"document:{name}".encode(),
    )[:GET_CARD_LINKS]
    _assert_guidance_text(ref, [f"Document:{name}" for name in shown], cut=GET_CARD_LINKS + 1)
