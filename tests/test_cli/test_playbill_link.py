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

    def file_context(self, instance_id: str, *, documents: Any = None) -> contracts.FileContext:
        self.requests.append(("file_context", documents))
        return contracts.FileContext.model_validate(
            service_playbill_file_context(self.instance, documents=documents).model_dump(
                mode="json"
            )
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
        self.requests.append(("propose", file, proposal_name, dry_run, bundle))
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


def test_an_entry_edited_during_the_proposal_is_not_overwritten(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog lock is not held across the daemon call: compare, then write."""

    (tmp_path / "guide.md").write_text("# Guide\nText.\n")
    assert _run("files", "add", "guide.md", "--root", str(tmp_path)).exit_code == 0
    propose = _Served.propose_file_bundle

    def edit_meanwhile(self: _Served, *args: Any, **kwargs: Any) -> Any:
        # Another local command edits the entry while the proposal is out.
        catalog = tmp_path / ".cruxible" / "files.yaml"
        entries = _entries(tmp_path)
        entries["doc-guide.md"]["required_tier"] = "admin"
        catalog.write_text(
            yaml.safe_dump({"catalog_kind": "portable", "entries": list(entries.values())})
        )
        return propose(self, *args, **kwargs)

    monkeypatch.setattr(_Served, "propose_file_bundle", edit_meanwhile)
    result = _run("link", "guide.md", "--guides", STATUS)
    assert result.exit_code == 1 and "changed this entry meanwhile" in result.output
    entry = _entries(tmp_path)["doc-guide.md"]
    assert entry["required_tier"] == "admin" and "links" not in entry


def test_guides_resolves_a_path_before_a_colliding_catalog_name(
    served: _Served, tmp_path: Path
) -> None:
    """An existing path wins, as for every citation; it is never re-read as a name."""

    (tmp_path / "guide").write_text("# Actual guide\nThe page at path guide.\n")
    (tmp_path / "other.md").write_text("# Other\nA different page.\n")
    for name, path, document in (
        ("guide", "other.md", "other-doc"),
        ("actual", "guide", "actual-doc"),
    ):
        added = _run(
            "files", "add", name, path, "--document-id", document, "--document-kind", "guidance",
            "--title", name, "--media-type", "text/markdown", "--scope", "project",
            "--root", str(tmp_path),
        )  # fmt: skip
        assert added.exit_code == 0, added.output
    linked = _run("link", "guide", "--guides", STATUS, "--json")
    assert linked.exit_code == 0, linked.output
    assert json.loads(linked.stdout)["file"] == "actual"
    entries = _entries(tmp_path)
    assert "links" in entries["actual"] and "links" not in entries["guide"]
    unlinked = _run("link", "guide", "--guides", STATUS, "--unlink", "--json")
    assert json.loads(unlinked.stdout)["file"] == "actual"


_OVERLAY = "catalog_kind: local\nentries:\n  - name: {name}\n    locator: {locator}\n{extra}"


@pytest.mark.parametrize("document", [True, False])
def test_a_local_overlay_of_the_page_refuses_before_anything_is_sent(
    served: _Served, tmp_path: Path, document: bool
) -> None:
    """The overlay replaces the whole entry on merge, so portable links would be hidden."""

    (tmp_path / "guide.md").write_text("# Guide\nText.\n")
    fields = (
        ["--document-id", "guide", "--document-kind", "guidance", "--title", "Guide"]
        + ["--scope", "project"]
        if document
        else []
    )
    assert _run("files", "add", "guide.md", *fields, "--root", str(tmp_path)).exit_code == 0
    extra = (
        "    document_id: guide\n    document_kind: guidance\n    title: Local\n"
        "    media_type: text/markdown\n    governance_scope: [project]\n"
        if document
        else ""
    )
    (tmp_path / ".cruxible" / "files.local.yaml").write_text(
        _OVERLAY.format(name="doc-guide.md", locator="guide.md", extra=extra)
    )
    before = (tmp_path / ".cruxible" / "files.yaml").read_text()
    result = _run("link", "guide.md", "--guides", STATUS)
    assert result.exit_code == 1, result.output
    assert "cruxible.link.file_overlaid" in result.output
    assert (tmp_path / ".cruxible" / "files.yaml").read_text() == before
    assert not [item for item in served.requests if item[0:1] == ("propose",)]


def test_an_overlay_added_during_the_proposal_is_a_conflict(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "guide.md").write_text("# Guide\nText.\n")
    assert _run("files", "add", "guide.md", "--root", str(tmp_path)).exit_code == 0
    before = (tmp_path / ".cruxible" / "files.yaml").read_text()
    propose = _Served.propose_file_bundle

    def overlay_meanwhile(self: _Served, *args: Any, **kwargs: Any) -> Any:
        (tmp_path / ".cruxible" / "files.local.yaml").write_text(
            _OVERLAY.format(name="doc-guide.md", locator="guide.md", extra="")
        )
        return propose(self, *args, **kwargs)

    monkeypatch.setattr(_Served, "propose_file_bundle", overlay_meanwhile)
    result = _run("link", "guide.md", "--guides", STATUS)
    assert result.exit_code == 1 and "changed this entry meanwhile" in result.output
    assert (tmp_path / ".cruxible" / "files.yaml").read_text() == before


@pytest.mark.parametrize("unrelated", [3, 30])
def test_linking_one_page_compiles_and_looks_up_only_that_page(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unrelated: int
) -> None:
    """The work is the linked page's: no other catalogued file, no other accepted Document."""

    from cruxible_core.service.evidence import file_catalog

    for index in range(unrelated):
        (tmp_path / f"page{index}.md").write_text(f"# Page {index}\n")
        added = _run(
            "files", "add", f"page{index}.md", "--document-id", f"page{index}",
            "--document-kind", "note", "--title", f"Page {index}", "--scope", "project",
            "--root", str(tmp_path),
        )  # fmt: skip
        assert added.exit_code == 0, added.output
    proposed = _run("files", "propose", "--file", "doc-page0.md", "--name", "p0",
                    "--root", str(tmp_path))  # fmt: skip
    assert proposed.exit_code == 0, proposed.output
    # An unrelated catalogued file that has gone missing does not block the link.
    (tmp_path / f"page{unrelated - 1}.md").unlink()
    (tmp_path / "guide.md").write_text("# Guide\nText.\n")
    assert _run("files", "add", "guide.md", "--root", str(tmp_path)).exit_code == 0

    def no_listing(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("linking one page listed every accepted Document")

    monkeypatch.setattr(file_catalog, "_accepted_documents", no_listing)
    served.requests.clear()
    linked = _run("link", "guide.md", "--guides", STATUS)
    assert linked.exit_code == 0, linked.output
    (context,) = [item for item in served.requests if item[0] == "file_context"]
    assert context == ("file_context", ("doc-guide.md",))
    (proposal,) = [item for item in served.requests if item[0] == "propose"]
    bundle = proposal[4]
    assert [item["source"]["name"] for item in bundle["documents"]] == ["doc-guide.md"]
    # A page already accepted compiles as its next revision from its own lookup.
    linked_again = _run("link", "doc-page0.md", "--guides", STATUS, "--json")
    assert linked_again.exit_code == 0, linked_again.output
    shell = json.loads(linked_again.stdout)["proposal"]
    assert shell["outcome"]["status"] == "landed"
    (page0,) = [
        item
        for item in served.file_context("inst_write", documents=("page0",)).documents
        if item["identity"] == "document:page0"
    ]
    assert page0["lifecycle"]["revision"] == 2


def test_an_overlay_made_unreadable_after_admission_keeps_the_receipt(
    served: _Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the daemon admits the proposal, local catalog trouble is a conflict, not a crash."""

    (tmp_path / "guide.md").write_text("# Guide\nText.\n")
    assert _run("files", "add", "guide.md", "--root", str(tmp_path)).exit_code == 0
    before = (tmp_path / ".cruxible" / "files.yaml").read_text()
    propose = _Served.propose_file_bundle

    def break_overlay(self: _Served, *args: Any, **kwargs: Any) -> Any:
        (tmp_path / ".cruxible" / "files.local.yaml").write_text("entries: [not, a, catalog\n")
        return propose(self, *args, **kwargs)

    monkeypatch.setattr(_Served, "propose_file_bundle", break_overlay)
    result = _run("link", "guide.md", "--guides", STATUS, "--json")
    assert result.exit_code == 1, result.output
    outcome = json.loads(result.stdout)
    assert outcome["catalog_conflict"] and not outcome["catalog_written"]
    assert outcome["proposal"]["outcome"]["status"] == "landed"
    assert (tmp_path / ".cruxible" / "files.yaml").read_text() == before


@pytest.mark.parametrize(
    ("surface", "repair"),
    [
        ("cli", "Register it first: cruxible files add loose.md"),
        ("sdk", 'Register it first: cx.files.add("loose.md")'),
        (
            "mcp",
            "Register it first: cruxible files add loose.md  (on the CLI; MCP has no tool "
            "that edits the file catalog)",
        ),
    ],
)
def test_an_unregistered_page_names_the_callers_own_way_to_register_it(
    tmp_path: Path, surface: Any, repair: str
) -> None:
    """coldmcp-08, coldsdk-15: the SDK and the MCP server printed `cruxible files add`
    as if their caller could run it; the SDK has `cx.files.add`, and MCP has no tool."""

    from cruxible_client.authoring.links import link_guides
    from cruxible_client.contracts.errors import WriteRefusalError

    (tmp_path / "loose.md").write_text("# Loose\n")
    with pytest.raises(WriteRefusalError) as refused:
        link_guides(
            object(),  # type: ignore[arg-type]
            "inst",
            workspace=tmp_path,
            source="loose.md",
            guides=(STATUS,),
            surface=surface,
        )

    assert refused.value.error_code == "cruxible.link.file_unregistered"
    assert refused.value.repair_line == repair
