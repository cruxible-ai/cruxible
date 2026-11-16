"""`cruxible link`: a section's note `about` Subjects, or a registered page's `guides` link."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from cruxible_client import contracts
from cruxible_client.contracts.backlinks import BacklinksRequest, BacklinksResult
from cruxible_client.contracts.file_catalog import FileCompilationBundle
from cruxible_core.service.authoring.landing import land_direct_proposal
from cruxible_core.service.discovery.backlinks import service_playbill_backlinks
from cruxible_core.service.evidence.file_catalog import (
    service_playbill_file_context,
    service_propose_playbill_file_bundle,
)
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_cli.test_playbill_write import (  # noqa: F401  (autouse fixture)
    WI1,
    _isolated_context,
    _run,
    _ServiceClient,
)

STATUS = f"ClaimType:{KIND}.status"
TITLE = f"ClaimType:{KIND}.title"


class _Served(_ServiceClient):
    """The write verbs, backlinks and the files lane, answered by the real services."""

    def backlinks(self, instance_id: str, *, request: BacklinksRequest) -> BacklinksResult:
        return service_playbill_backlinks(self.instance, request=request)

    def file_context(self, instance_id: str) -> contracts.FileContext:
        return contracts.FileContext.model_validate(
            service_playbill_file_context(self.instance).model_dump(mode="json")
        )

    def propose_file_bundle(
        self,
        instance_id: str,
        *,
        bundle: dict[str, Any],
        file: str,
        proposal_name: str,
        dry_run: bool | None = None,
        at: str | None = None,
        accept: Any = "if_allowed",
    ) -> contracts.ProposalInspection:
        self.requests.append(("propose", file, proposal_name, dry_run))
        result = service_propose_playbill_file_bundle(
            self.instance,
            bundle=FileCompilationBundle.model_validate(bundle),
            file=file,
            actor_id="owner",
            proposal_name=proposal_name,
            timestamp="2026-11-16T12:00:00.000000Z",
            dry_run=dry_run,
            at=at,
        )
        result = land_direct_proposal(
            self.instance, result, actor_id="owner", accept=accept, may_activate=True
        )
        return contracts.ProposalInspection.model_validate(result.model_dump(mode="json"))


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Served:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    client = _Served(instance)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    monkeypatch.chdir(tmp_path)
    return client


def _entries(tmp_path: Path) -> dict[str, dict[str, Any]]:
    catalog = yaml.safe_load((tmp_path / ".cruxible" / "files.yaml").read_text())
    return {entry["name"]: entry for entry in catalog["entries"]}


def _accepted_links(client: _Served, document_id: str) -> list[dict[str, str]]:
    context = client.file_context("inst_write")
    (shell,) = [item for item in context.documents if item["identity"] == f"document:{document_id}"]
    return shell["links"]


def test_link_about_links_an_existing_section_by_path_or_name_and_unlinks(
    served: _Served, tmp_path: Path
) -> None:
    noted = _run(
        "note", "notes.md", "--text", "# Triage\nBlocked.\n\n## Later\nMore.\n", "--about", WI1,
        "--because", "Triage context",
    )  # fmt: skip
    assert noted.exit_code == 0, noted.output
    linked = _run(
        "link", "doc-notes.md#Triage/Later", "--about", f"{KIND}/wi-2", "--because", "Also wi-2"
    )
    assert linked.exit_code == 0, linked.output
    shown = json.loads(_run("backlinks", f"{KIND}/wi-2", "--json").stdout)
    (row,) = shown["links"]
    assert (row["relation"], row["file"]) == ("about", "doc-notes.md")
    unlinked = _run(
        "link", "notes.md#Triage/Later", "--about", f"{KIND}/wi-2", "--unlink",
        "--because", "Not about wi-2",
    )  # fmt: skip
    assert unlinked.exit_code == 0, unlinked.output
    assert json.loads(_run("backlinks", f"{KIND}/wi-2", "--json").stdout)["links"] == []


def test_link_guides_records_the_link_and_files_propose_keeps_it(
    served: _Served, tmp_path: Path
) -> None:
    (tmp_path / "guide.md").write_text("# Status guide\nHow to judge status.\n")
    added = _run(
        "files", "add", "status.guide", "guide.md", "--document-id", "status-guide",
        "--document-kind", "guidance", "--title", "Status guide", "--scope", "project",
        "--root", str(tmp_path),
    )  # fmt: skip
    assert added.exit_code == 0, added.output

    preview = _run("link", "status.guide", "--guides", STATUS, "--dry-run")
    assert preview.exit_code == 0, preview.output
    assert "would_propose" in preview.output and "nothing was written" in preview.output
    assert "links" not in _entries(tmp_path)["status.guide"]

    linked = _run("link", "guide.md", "--guides", STATUS, "--guides", TITLE, "--json")
    assert linked.exit_code == 0, linked.output
    outcome = json.loads(linked.stdout)
    assert outcome["catalog_written"] and outcome["proposal"]["outcome"]["status"] == "landed"
    expected = [
        {"relation": "guides", "target_identity": STATUS},
        {"relation": "guides", "target_identity": TITLE},
    ]
    assert _entries(tmp_path)["status.guide"]["links"] == expected
    assert _accepted_links(served, "status-guide") == expected

    # Guidance shows on the ClaimType's backlinks.
    shown = json.loads(_run("backlinks", STATUS, "--json").stdout)
    assert [row["relation"] for row in shown["links"]] == ["guides"]

    # A later files propose of new bytes keeps the links.
    (tmp_path / "guide.md").write_text("# Status guide\nHow to judge status, revised.\n")
    proposed = _run(
        "files", "propose", "--file", "status.guide", "--name", "guide-rev",
        "--root", str(tmp_path),
    )  # fmt: skip
    assert proposed.exit_code == 0, proposed.output
    assert _accepted_links(served, "status-guide") == expected

    unlinked = _run("link", "status.guide", "--guides", TITLE, "--unlink")
    assert unlinked.exit_code == 0, unlinked.output
    assert _entries(tmp_path)["status.guide"]["links"] == expected[:1]
    assert _accepted_links(served, "status-guide") == expected[:1]


def test_link_guides_on_a_write_catalogued_page_declares_its_one_document(
    served: _Served, tmp_path: Path
) -> None:
    noted = _run(
        "note", "notes.md", "--text", "# Review guide\nCheck the status.\n", "--about", WI1,
        "--because", "Triage",
    )  # fmt: skip
    assert noted.exit_code == 0, noted.output
    assert "document_id" not in _entries(tmp_path)["doc-notes.md"]
    linked = _run("link", "notes.md", "--guides", STATUS)
    assert linked.exit_code == 0, linked.output
    assert "Declared Document doc-notes.md" in linked.output
    entries = _entries(tmp_path)
    assert list(entries) == ["doc-notes.md"]  # one entry, never a second
    entry = entries["doc-notes.md"]
    assert (entry["document_id"], entry["title"]) == ("doc-notes.md", "Review guide")
    # files propose revises that same Document.
    (tmp_path / "notes.md").write_text("# Review guide\nCheck the status twice.\n")
    proposed = _run(
        "files", "propose", "--file", "doc-notes.md", "--name", "notes-rev", "--json",
        "--root", str(tmp_path),
    )  # fmt: skip
    assert proposed.exit_code == 0, proposed.output
    documents = served.file_context("inst_write").documents
    (shell,) = [item for item in documents if item["identity"] == "document:doc-notes.md"]
    assert shell["lifecycle"]["revision"] == 2
    assert shell["links"] == [{"relation": "guides", "target_identity": STATUS}]


def test_a_refused_guides_proposal_leaves_the_catalog_unchanged(
    served: _Served, tmp_path: Path
) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nText.\n")
    assert _run("files", "add", "guide.md", "--root", str(tmp_path)).exit_code == 0
    before = (tmp_path / ".cruxible" / "files.yaml").read_text()
    refused = _run("link", "guide.md", "--guides", "ClaimType:no.such.field")
    assert (tmp_path / ".cruxible" / "files.yaml").read_text() == before, refused.output
    assert refused.exit_code == 1 and "files.yaml is unchanged" in refused.output


@pytest.mark.parametrize(
    ("args", "code", "says"),
    [
        (("notes.md", "--guides", WI1), "cruxible.link.guides_needs_definition", "is a Subject"),
        (
            ("notes.md#Triage", "--about", STATUS, "--because", "x"),
            "cruxible.link.about_needs_subject",
            "is a ClaimType",
        ),
        (
            ("notes.md", "--about", WI1, "--because", "x"),
            "cruxible.link.about_needs_section",
            "a whole file",
        ),
        (("notes.md#Triage", "--guides", STATUS), "cruxible.link.guides_needs_page", "a section"),
        (("loose.md", "--guides", STATUS), "cruxible.link.file_unregistered", "files add"),
    ],
)
def test_mismatched_link_arguments_refuse_and_say_which_link_is_which(
    served: _Served, tmp_path: Path, args: tuple[str, ...], code: str, says: str
) -> None:
    noted = _run(
        "note", "notes.md", "--text", "# Triage\nBlocked.\n", "--about", WI1, "--because", "x"
    )
    assert noted.exit_code == 0, noted.output
    (tmp_path / "loose.md").write_text("# Loose\n")
    result = _run("link", *args)
    assert result.exit_code == 1, result.output
    assert code in result.output and says in result.output
    if code != "cruxible.link.file_unregistered":
        assert "A note links a section --about Subjects" in result.output
        assert "proposal review shows it" in result.output


def test_a_catalog_entry_without_links_digests_as_before() -> None:
    """``links`` is omitted when empty, so existing catalogs keep their compilation digests."""

    from cruxible_client.contracts.file_catalog import FileCatalogEntry

    entry = FileCatalogEntry(name="a", locator="a.md")
    assert "links" not in entry.model_dump(mode="json")
