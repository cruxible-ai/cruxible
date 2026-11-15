"""The SDK reference's worked examples run against a scratch instance."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.transport.http import CruxibleClient

README = Path(__file__).resolve().parents[2] / "packages" / "cruxible-client" / "README.md"


def _example(heading: str) -> str:
    """The first python block under one README heading."""

    text = README.read_text(encoding="utf-8")
    start = text.index(f"\n{heading}\n")
    found = re.search(r"^```python\n(?P<body>.*?)^```$", text[start:], re.MULTILINE | re.DOTALL)
    assert found is not None, heading
    return found["body"]


def _connect(client: TestClient, instance_id: str, workspace: Path) -> Cruxible:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    return Cruxible._from_client(  # noqa: SLF001
        transport, instance_id=instance_id, workspace=workspace, clock=lambda: datetime.now(UTC)
    )


def test_the_vocabulary_example_defines_a_keyed_kind_and_admits_own_words(
    playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    client, instance_id, _key = playbill_http
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    namespace: dict[str, Any] = {"cx": _connect(client, instance_id, workspace)}

    exec(compile(_example("### Defining a vocabulary"), "README.md", "exec"), namespace)  # noqa: S102

    assert namespace["intent"].outcome.status == "landed", namespace["intent"]
    outcome = namespace["outcome"]
    assert outcome.status == "accepted", outcome
    assert {change.verdict for change in outcome.changes} == {"supported"}, outcome
