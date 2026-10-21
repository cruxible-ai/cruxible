"""Admission precedes local writes; CAS rebases only unchanged target sections."""

from pathlib import Path

import pytest
import yaml

from cruxible_client.authoring.doc_writes import write_workspace
from cruxible_client.contracts.doc_sections import parse_sections
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.get_reads import GetCoordinate
from cruxible_client.contracts.write import (
    AppendSection,
    ReplaceSection,
    WriteOutcome,
    WriteRequest,
)


def _answer(status="accepted"):
    return WriteOutcome(status=status, coordinate=GetCoordinate(git_oid="a" * 12, generation=1))


def test_create_is_written_after_acceptance_and_catalog_retry_is_unique(tmp_path: Path) -> None:
    request = WriteRequest(
        because="Prose", docs=(AppendSection(file="chosen/notes.md", text="# A\nwords"),)
    )

    def submit(observed):
        assert not (tmp_path / "chosen/notes.md").exists()
        assert observed.docs[0].source_id == "doc-chosen-notes.md"
        assert "chosen/notes.md" not in observed.model_dump_json()
        return _answer()

    result = write_workspace(request, workspace=tmp_path, submit=submit)
    assert result.docs[0].written
    assert (tmp_path / "chosen/notes.md").read_bytes() == b"# A\nwords\n"
    retried = write_workspace(request, workspace=tmp_path, submit=lambda _: _answer())
    assert retried.docs[0].written
    catalog = yaml.safe_load((tmp_path / ".cruxible/files.yaml").read_text())
    assert len(catalog["entries"]) == 1


@pytest.mark.parametrize(
    "status,dry_run", [("refused", False), ("would_accept", True), ("awaiting_approval", False)]
)
def test_refusal_and_dry_run_leave_bytes_while_awaiting_approval_writes(
    tmp_path: Path, status, dry_run
) -> None:
    request = WriteRequest(
        because="Prose", docs=(AppendSection(file="notes.md", text="# A"),), dry_run=dry_run
    )
    result = write_workspace(request, workspace=tmp_path, submit=lambda _: _answer(status))
    assert result.docs[0].written == (status == "awaiting_approval")
    assert (tmp_path / "notes.md").exists() == (status == "awaiting_approval")


@pytest.mark.parametrize("same", [False, True])
def test_whole_file_cas_rebases_only_unrelated_edits(tmp_path: Path, same: bool) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes(b"# A\nold\n# B\nother\n")
    request = WriteRequest(
        because="Prose", docs=(ReplaceSection(file="notes.md", section=("A",), text="# A\nnew"),)
    )

    def submit(_):
        path.write_bytes(
            b"# A\nconcurrent\n# B\nother\n" if same else b"# A\nold\n# B\nconcurrent\n"
        )
        return _answer()

    result = write_workspace(request, workspace=tmp_path, submit=submit)
    assert result.docs[0].written is not same
    if same:
        assert result.docs[0].refusal.code == "cruxible.doc.doc_write_conflict"
        assert result.docs[0].refusal.repair
        assert b"concurrent" in path.read_bytes()
    else:
        assert path.read_bytes() == b"# A\nnew\n# B\nconcurrent\n"


def test_stale_explicit_section_cas_refuses_before_submit(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes(b"# A\nold\n")
    digest = parse_sections(path.read_bytes()).section(("A",)).digest
    path.write_bytes(b"# A\nchanged\n")
    with pytest.raises(WriteRefusalError) as caught:
        write_workspace(
            WriteRequest(
                because="Prose",
                docs=(
                    ReplaceSection(
                        file="notes.md",
                        section=("A",),
                        text="# A\nnew",
                        expect=digest,
                    ),
                ),
            ),
            workspace=tmp_path,
            submit=lambda _: pytest.fail("must not submit a stale edit"),
        )
    assert caught.value.error_code == "cruxible.doc.section_changed"
    current = parse_sections(path.read_bytes()).section(("A",)).digest
    assert caught.value.repair_line == (f"re-read the section, then retry with expect: {current}")
    assert ".." not in str(caught.value)


def test_each_doc_refusal_carries_its_own_repair(tmp_path: Path) -> None:
    def refused(op) -> WriteRefusalError:
        with pytest.raises(WriteRefusalError) as caught:
            write_workspace(
                WriteRequest(because="Prose", docs=(op,)),
                workspace=tmp_path,
                submit=lambda _: pytest.fail("must refuse locally"),
            )
        assert ".." not in str(caught.value)
        return caught.value

    protected = refused(AppendSection(file=".git/notes.md", text="# A"))
    assert protected.error_code == "cruxible.doc.protected_path"
    assert protected.repair_line is None and "outside .cruxible and .git" in protected.detail
    (tmp_path / "notes.md").write_text(
        "# Decision\n<!-- cruxible:block:summary:ref:aaaaaaaaaaaa -->\n"
        "## Protected\nbody\n<!-- /cruxible:block:summary -->\n"
    )
    projection = refused(
        ReplaceSection(file="notes.md", section=("Decision", "Protected"), text="## Protected\nx")
    )
    assert projection.error_code == "cruxible.doc.projection_edit"
    assert projection.repair_line == "cruxible block repin notes.md summary"
    invalid = refused(AppendSection(file="new.md", text="no heading"))
    assert invalid.error_code == "cruxible.doc.section_invalid"
    assert invalid.repair_line is None


@pytest.mark.parametrize(
    "path", [".cruxible/floor/notes.md", ".CrUxIbLe/notes.md", ".git/notes.md"]
)
def test_protected_paths_refuse_before_submit(tmp_path: Path, path: str) -> None:
    with pytest.raises(WriteRefusalError, match="protected_path"):
        write_workspace(
            WriteRequest(because="Prose", docs=(AppendSection(file=path, text="# A"),)),
            workspace=tmp_path,
            submit=lambda _: pytest.fail("must refuse locally"),
        )


def test_symlink_into_floor_refuses(tmp_path: Path) -> None:
    floor = tmp_path / ".cruxible/floor"
    floor.mkdir(parents=True)
    (floor / "notes.md").write_text("# A\n")
    (tmp_path / "alias").symlink_to(floor, target_is_directory=True)
    with pytest.raises(WriteRefusalError, match="protected_path"):
        write_workspace(
            WriteRequest(because="Prose", docs=(AppendSection(file="alias/notes.md", text="# B"),)),
            workspace=tmp_path,
            submit=lambda _: pytest.fail("must refuse locally"),
        )


@pytest.mark.parametrize("replace", [False, True])
def test_section_ref_resolves_resulting_path_and_never_crosses_wire(tmp_path, replace):
    from cruxible_client.contracts.write import SectionEvidence, SetChange

    if replace:
        (tmp_path / "notes.md").write_text("# Before\nold\n")
    op = (
        ReplaceSection(id="basis", file="notes.md", section=("Before",), text="# After\nnew")
        if replace
        else AppendSection(id="basis", file="notes.md", text="# After\nnew")
    )

    def submit(request):
        evidence = request.changes[0].evidence
        assert evidence.ref is None and evidence.section == ("After",)
        assert evidence.observation.selected_content == b"# After\nnew\n"
        assert evidence.own_words
        assert '"ref"' not in request.model_dump_json()
        assert '"id"' not in request.model_dump_json()
        assert WriteRequest.model_validate_json(request.model_dump_json()) == request
        return _answer()

    result = write_workspace(
        WriteRequest(
            because="Reason",
            docs=(op,),
            changes=(
                SetChange(
                    subject="project.item/a",
                    field="status",
                    value="ready",
                    evidence=SectionEvidence(ref="basis"),
                ),
            ),
        ),
        workspace=tmp_path,
        submit=submit,
    )
    assert result.docs[0].written


def test_unknown_and_duplicate_refs_refuse_before_submit(tmp_path):
    from cruxible_client.contracts.write import SectionEvidence, SetChange

    for ops, code in [
        ((AppendSection(id="other", file="notes.md", text="# A"),), "section_ref_unknown"),
        (
            (
                AppendSection(id="basis", file="notes.md", text="# A"),
                AppendSection(id="basis", file="notes.md", text="# B"),
            ),
            "section_ref_ambiguous",
        ),
    ]:
        with pytest.raises(WriteRefusalError, match=code):
            write_workspace(
                WriteRequest(
                    because="Reason",
                    docs=ops,
                    changes=(
                        SetChange(
                            field="status", value="ready", evidence=SectionEvidence(ref="basis")
                        ),
                    ),
                ),
                workspace=tmp_path,
                submit=lambda _: pytest.fail("must not submit"),
            )
        assert not (tmp_path / "notes.md").exists()


def test_ref_to_preamble_is_citable(tmp_path):
    from cruxible_client.authoring.files import add_catalog_entry
    from cruxible_client.contracts.write import SectionEvidence, SetChange

    path = tmp_path / "notes.md"
    path.write_text("old preamble\n# A\nbody\n")
    add_catalog_entry(tmp_path, name="notes", path=path, fields={})

    def submit(request):
        evidence = request.changes[0].evidence
        assert evidence.section == ()
        assert evidence.observation.selected_content == b"new preamble\n"
        return _answer()

    write_workspace(
        WriteRequest(
            because="Reason",
            docs=(ReplaceSection(id="intro", file="notes.md", section=(), text="new preamble"),),
            changes=(
                SetChange(field="status", value="ready", evidence=SectionEvidence(ref="intro")),
            ),
        ),
        workspace=tmp_path,
        submit=submit,
    )


def test_cli_prints_a_local_doc_refusal_as_not_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from click.testing import CliRunner

    from cruxible_core.cli.main import cli

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: pytest.fail("a local refusal never reaches the daemon"),
    )
    payload = {"because": "Prose", "docs": [{"op": "append", "file": ".git/x.md", "text": "# A"}]}
    result = CliRunner().invoke(
        cli,
        [
            *("--server-url", "http://server", "--instance-id", "inst_doc"),
            *("write", "-", "--workspace-root", str(tmp_path)),
        ],
        input=json.dumps(payload),
    )
    assert result.exit_code == 1
    lines = [line for line in result.stderr.splitlines() if not line.startswith("target:")]
    assert lines == [
        "refused (not submitted)",
        "cruxible.doc.protected_path: Choose a workspace Markdown path outside .cruxible and .git",
    ]
    assert "WriteRefusalError" not in result.output
