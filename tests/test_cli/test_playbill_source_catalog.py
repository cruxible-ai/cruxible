"""PB-E source-catalog CLI keeps paths local and submits frozen bytes."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_core.cli.main import cli
from cruxible_core.playbill.projection import AcceptedCoordinate
from tests.test_service.test_playbill_documents import _instance


def test_cli_compile_and_propose_never_send_a_client_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    repository = tmp_path / "authoring"
    source = repository / "specs" / "design.md"
    source.parent.mkdir(parents=True)
    original = b"# Playbill from a declared source\n"
    source.write_bytes(original)
    catalog = tmp_path / "playbill-sources.yaml"
    catalog.write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: playbill-design
    locator: specs/design.md
    document_id: design
    document_kind: design
    title: Playbill design
    media_type: text/markdown
    governance_scope: [project:playbill]
"""
    )
    bundle_path = tmp_path / "compiled.json"
    submitted: list[dict[str, Any]] = []

    class StubClient:
        def playbill_source_context(self, instance_id: str) -> contracts.PlaybillSourceContext:
            assert instance_id == "inst_cli"
            return contracts.PlaybillSourceContext(
                accepted_coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                    coordinate.model_dump(mode="json")
                ),
                documents=[],
            )

        def propose_playbill_source_bundle(
            self,
            instance_id: str,
            *,
            bundle: dict[str, Any],
            source_name: str,
            proposal_name: str,
        ) -> contracts.PlaybillProposalInspection:
            assert instance_id == "inst_cli"
            assert source_name == "playbill-design"
            assert proposal_name == "compiled-design"
            submitted.append(bundle)
            return contracts.PlaybillProposalInspection(
                proposal={"admission": {"proposal_id": "sha256:" + "1" * 64}},
                accepted_coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                    coordinate.model_dump(mode="json")
                ),
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    runner = CliRunner()
    compiled = runner.invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "playbill",
            "sources",
            "compile",
            "--catalog",
            str(catalog),
            "--root",
            str(repository),
            "--output",
            str(bundle_path),
            "--json",
        ],
    )
    assert compiled.exit_code == 0, compiled.output
    frozen = SourceCompilationBundle.model_validate_json(bundle_path.read_bytes())
    assert base64.b64decode(frozen.documents[0].body_base64) == original
    assert "locator" not in frozen.model_dump_json()
    assert str(repository) not in frozen.model_dump_json()

    source.write_bytes(b"# Changed after compilation\n")
    proposed = runner.invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "playbill",
            "sources",
            "propose",
            "--bundle",
            str(bundle_path),
            "--source",
            "playbill-design",
            "--name",
            "compiled-design",
            "--json",
        ],
    )
    assert proposed.exit_code == 0, proposed.output
    assert len(submitted) == 1
    transmitted = json.dumps(submitted[0])
    assert str(source) not in transmitted
    assert "specs/design.md" not in transmitted
    assert base64.b64decode(submitted[0]["documents"][0]["body_base64"]) == original
