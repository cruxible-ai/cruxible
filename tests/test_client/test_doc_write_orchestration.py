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
    DocOutcome,
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
    assert (tmp_path / "chosen/notes.md").read_bytes() == b"# A\nwords\n\n"
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
    # Spelled for the request's client (MCP when unnamed; the CLI sends "cli").
    assert projection.repair_line == 'cruxible_block_repin(file="notes.md", block="summary")'
    invalid = refused(AppendSection(file="new.md", text="no heading"))
    assert invalid.error_code == "cruxible.doc.section_invalid"
    # A heading-less append names the heading it needs (gapb-25).
    assert invalid.repair_line is not None and "--heading TITLE" in invalid.repair_line


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
        # An appended section leaves one blank line after it; a replacement is exact.
        assert evidence.observation.selected_content == (
            b"# After\nnew\n" if replace else b"# After\nnew\n\n"
        )
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


def test_about_link_is_observed_like_section_evidence_and_unlink_needs_no_section(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.write import AboutChange

    request = WriteRequest(
        because="Note",
        docs=(AppendSection(id="n", file="notes.md", text="# Triage\nLooks benign\n"),),
        changes=(AboutChange(ref="n", subjects=("project.work_item/wi-1",)),),
    )
    seen = []

    def submit(observed):
        seen.append(observed)
        return _answer()

    write_workspace(request, workspace=tmp_path, submit=submit)
    (change,) = seen[0].changes
    assert isinstance(change, AboutChange) and change.ref is None
    assert change.file == "doc-notes.md" and change.section == ("Triage",)
    assert change.observation is not None
    assert change.observation.selected_content == b"# Triage\nLooks benign\n\n"
    assert "notes.md" not in change.observation.model_dump_json().replace("doc-notes.md", "")
    # The section is gone: an unlink still names the note by source and path.
    (tmp_path / "notes.md").write_bytes(b"# Other\n")
    unlink = WriteRequest(
        because="Unlink",
        changes=(
            AboutChange(file="notes.md", section=("Triage",), unlink=("project.work_item/wi-1",)),
        ),
    )
    write_workspace(unlink, workspace=tmp_path, submit=submit)
    (gone,) = seen[1].changes
    assert gone.observation is None and gone.file == "doc-notes.md"
    # The file itself is gone (still catalogued): the unlink still names the note.
    (tmp_path / "notes.md").unlink()
    write_workspace(unlink, workspace=tmp_path, submit=submit)
    (missing,) = seen[2].changes
    assert missing.observation is None and missing.file == "doc-notes.md"
    with pytest.raises(WriteRefusalError):
        write_workspace(
            WriteRequest(
                because="Link",
                changes=(
                    AboutChange(
                        file="notes.md", section=("Triage",), subjects=("project.work_item/wi-1",)
                    ),
                ),
            ),
            workspace=tmp_path,
            submit=submit,
        )


def test_failed_history_confirmation_reports_that_the_file_was_written(tmp_path):
    path = tmp_path / "notes.md"
    path.write_bytes(b"# A\nold\n")
    token = "DCE-" + "a" * 32

    def submit(request):
        if request.doc_confirmations:
            assert request.doc_confirmations == (token,)
            assert path.read_bytes() == b"# A\nnew\n"
            return _answer("refused")
        edit = request.docs[0]
        return _answer().model_copy(
            update={
                "docs": (
                    DocOutcome(
                        source_id=edit.source_id,
                        section=edit.section,
                        before_digest=edit.before_digest,
                        after_digest=edit.after_digest,
                        confirmation=token,
                    ),
                )
            }
        )

    with pytest.raises(WriteRefusalError, match="file was written") as caught:
        write_workspace(
            WriteRequest(
                because="Replace",
                docs=(
                    ReplaceSection(
                        file="notes.md",
                        section=("A",),
                        text="# A\nnew",
                    ),
                ),
            ),
            workspace=tmp_path,
            submit=submit,
        )
    assert caught.value.error_code == "cruxible.doc.history_unconfirmed"
    assert token in caught.value.repair_line


@pytest.mark.parametrize("kind", ["file", "section", "replace"])
def test_a_parent_swapped_for_a_symlink_after_the_check_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Review P1: the race itself, forced between the name check and the read."""

    from cruxible_client.authoring import doc_writes
    from cruxible_client.contracts.write import FileEvidence, SectionEvidence, SetChange

    workspace = tmp_path / "workspace"
    (workspace / "inside").mkdir(parents=True)
    (workspace / "inside" / "file.md").write_text("# Public\nnothing here\n")
    private = tmp_path / "private"
    private.mkdir()
    (private / "file.md").write_text("# Public\nSECRET outside the workspace\n")
    real_path = doc_writes._path

    def swapped(root: Path, file: str) -> Path:
        checked = real_path(root, file)
        # After the containment check: the parent becomes a link outside.
        (root / "inside").rename(root / "moved")
        (root / "inside").symlink_to(private, target_is_directory=True)
        return checked

    monkeypatch.setattr(doc_writes, "_path", swapped)
    if kind == "replace":
        request = WriteRequest(
            because="x",
            docs=(ReplaceSection(file="inside/file.md", section=("Public",), text="# Public\nx"),),
        )
    else:
        evidence = (
            FileEvidence(file="inside/file.md#SECRET")
            if kind == "file"
            else SectionEvidence(file="inside/file.md", section=("Public",))
        )
        request = WriteRequest(
            because="x",
            changes=(
                SetChange(subject="dev.item/a", field="status", value="x", evidence=evidence),
            ),
        )
    with pytest.raises(WriteRefusalError) as caught:
        write_workspace(
            request,
            workspace=workspace,
            submit=lambda _: pytest.fail("bytes from outside the workspace were submitted"),
        )
    assert caught.value.error_code == "cruxible.doc.protected_path"
    assert "SECRET" not in str(caught.value)


def test_derived_catalog_names_never_share_a_name_for_two_paths(tmp_path: Path) -> None:
    """Review P2(a): flattening that cannot be read back takes a digest."""

    from cruxible_client.authoring.files import default_catalog_name

    def name(relative: str) -> str:
        return default_catalog_name(tmp_path, tmp_path / relative)

    # Names a workspace already has stay as they were.
    assert name("notes.md") == "doc-notes.md"
    assert name("app/forms.py") == "doc-app-forms.py"
    paths = ["a/b.py", "a-b.py", "A/b.py", "a b.py", "a/b/c.py", "a-b/c.py", "x" * 130 + ".md"]
    names = [name(item) for item in paths]
    assert len(set(names)) == len(paths), names
    assert names[0] == "doc-a-b.py" and names[1].startswith("doc-a-b.py-")
    assert all(len(item) <= 128 for item in names)


def _cite(relative: str, anchor: str) -> WriteRequest:
    from cruxible_client.contracts.write import FileEvidence, SetChange

    return WriteRequest(
        because="x",
        changes=(
            SetChange(
                subject="dev.item/a",
                field="status",
                value="x",
                evidence=FileEvidence(file=f"{relative}#{anchor}"),
            ),
        ),
    )


def test_concurrent_catalog_edits_take_turns_and_never_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P2(b): both snapshots are taken before either write, without the lock."""

    import threading
    import time

    from cruxible_client.authoring import files

    for relative in ("a/b.py", "c.py", "d.py"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("x = 1\n")
    real_write = files._write_portable

    def slow_write(path, entries):  # type: ignore[no-untyped-def]
        time.sleep(0.3)
        real_write(path, entries)

    monkeypatch.setattr(files, "_write_portable", slow_write)
    errors: list[Exception] = []

    def add(name: str, relative: str) -> None:
        try:
            files.catalog_file(tmp_path, name=name, path=tmp_path / relative)
        except files.FileCatalogEditError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=add, args=("one", "a/b.py")),
        threading.Thread(target=add, args=("two", "c.py")),
        threading.Thread(target=add, args=("two", "d.py")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = yaml.safe_load((tmp_path / ".cruxible" / "files.yaml").read_text())["entries"]
    by_name = {item["name"]: item["locator"] for item in entries}
    assert set(by_name) == {"one", "two"} and by_name["one"] == "a/b.py"
    # The second "two" refused instead of replacing the first.
    assert [exc.reason for exc in errors] == ["entry_exists"]
    assert by_name["two"] in {"c.py", "d.py"}


def test_a_name_taken_after_admission_is_refused_not_overwritten(tmp_path: Path) -> None:
    from cruxible_client.authoring.files import catalog_file

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b.py").write_text("def f():\n    pass\n")
    (tmp_path / "other.py").write_text("y = 2\n")

    def taken_meanwhile(observed: WriteRequest) -> WriteOutcome:
        # Another writer catalogues a different file under the same name.
        catalog_file(tmp_path, name="doc-a-b.py", path=tmp_path / "other.py")
        return _answer()

    outcome = write_workspace(_cite("a/b.py", "def f"), workspace=tmp_path, submit=taken_meanwhile)

    (item,) = outcome.catalogued
    assert (item.name, item.written) == ("doc-a-b.py", False)
    assert outcome.next is not None and "catalogued for another file" in outcome.next
    entries = yaml.safe_load((tmp_path / ".cruxible" / "files.yaml").read_text())["entries"]
    assert entries == [{"name": "doc-a-b.py", "locator": "other.py"}]

    # The same file catalogued under the name by a concurrent write counts as done.
    (tmp_path / "c.py").write_text("z = 3\n")

    def same_meanwhile(observed: WriteRequest) -> WriteOutcome:
        catalog_file(tmp_path, name="doc-c.py", path=tmp_path / "c.py")
        return _answer()

    again = write_workspace(_cite("c.py", "z = 3"), workspace=tmp_path, submit=same_meanwhile)
    assert [(item.name, item.written) for item in again.catalogued] == [("doc-c.py", True)]
