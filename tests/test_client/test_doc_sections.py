"""Section edits operate on one document's exact bytes, without workspace I/O."""

import pytest

from cruxible_client.contracts.doc_sections import (
    DocSectionError,
    append_section,
    parse_sections,
    replace_section,
    section_digest,
)


def test_heading_paths_extents_and_utf8_offsets() -> None:
    content = "Intro é\n# Root ##\nbody\n### Child\nchild\n## Other\nlast\n# End\n".encode()
    doc = parse_sections(content)
    assert [s.path for s in doc.sections] == [
        ("Root",),
        ("Root", "Child"),
        ("Root", "Other"),
        ("End",),
    ]
    assert doc.subtree(()) == "Intro é\n".encode()
    assert doc.own_body(("Root",)) == b"# Root ##\nbody\n"
    assert doc.subtree(("Root",)) == content[content.index(b"# Root") : content.index(b"# End")]
    assert doc.section(("Root",)).line == 2
    assert doc.section(("Root",)).digest == section_digest(doc.subtree(("Root",)))


@pytest.mark.parametrize("fence", ["```", "~~~~", "   ```python"])
def test_fenced_headings_are_text(fence: str) -> None:
    closing = "~~~~~" if "~" in fence else "````"
    text = f"# A\n{fence}\n# fake\n## fake child\n{closing}\n## Real\n"
    assert [s.path for s in parse_sections(text.encode()).sections] == [("A",), ("A", "Real")]


def test_fence_closes_only_with_same_character_sufficient_length_and_no_info() -> None:
    content = b"# A\n````\n```\n~~~~\n```` info\n# Fake\n`````\n# Real\n"
    assert [s.path for s in parse_sections(content).sections] == [("A",), ("Real",)]


def test_setext_html_indented_and_invalid_atx_are_text() -> None:
    content = b"Title\n=====\n<h1>HTML</h1>\n    # indented\n#no-space\n####### seven\n# Real\n"
    doc = parse_sections(content)
    assert [s.path for s in doc.sections] == [("Real",)]
    assert doc.subtree(()) == content[: content.index(b"# Real")]


def test_exact_case_closing_hashes_and_empty_headings() -> None:
    content = b"# Name ### \n## lower#\n## Lower\n#\n"
    assert [s.path for s in parse_sections(content).sections] == [
        ("Name",),
        ("Name", "lower#"),
        ("Name", "Lower"),
        ("",),
    ]


def test_frontmatter_is_excluded_from_preamble_and_headings() -> None:
    content = b"---\ntype: agent-choice\n# Not a heading\n---\npreamble\n# A\n"
    doc = parse_sections(content)
    assert [s.path for s in doc.sections] == [("A",)]
    assert doc.subtree(()) == b"preamble\n"
    edited = replace_section(content, section=(), text="New preamble")
    assert edited.content == content[: content.index(b"preamble")] + b"New preamble\n# A\n"


@pytest.mark.parametrize("content", [b"", b"body\n", b"---\n# only metadata\n---\n"])
def test_headingless_documents_have_preamble(content: bytes) -> None:
    doc = parse_sections(content)
    assert not doc.sections
    assert doc.subtree(()) == (b"" if content.startswith(b"---") else content)


def test_duplicate_path_refuses_with_each_line_but_other_path_is_usable() -> None:
    doc = parse_sections(b"# A\n## X\n# A\n## X\n# B\n")
    with pytest.raises(DocSectionError) as caught:
        doc.section(("A", "X"))
    assert caught.value.code == "cruxible.doc.section_ambiguous"
    assert caught.value.lines == (2, 4)
    assert doc.section(("B",)).line == 5


def test_append_under_path_and_retry() -> None:
    content = b"# A\nbody\n## Old\nchild\n# B\n"
    result = append_section(content, under=("A",), text="## New\nnew")
    assert result.content == b"# A\nbody\n## Old\nchild\n## New\nnew\n# B\n"
    assert result.section == ("A", "New")
    assert result.before_digest is None
    assert result.after_digest == section_digest(b"## New\nnew\n")
    assert not append_section(result.content, under=("A",), text="## New\nnew").changed


def test_append_root_allows_any_level_and_repairs_missing_newline() -> None:
    result = append_section(b"# A\nbody", text="### B")
    assert result.content == b"# A\nbody\n### B\n"
    # At EOF, a lower-level root append is still nested by Markdown's hierarchy.
    assert result.section == ("A", "B")


@pytest.mark.parametrize(
    "text", ["plain body", "# Peer", "### Too deep", "## New\n# Escape", "## X\n## Y"]
)
def test_append_refuses_invalid_subtrees(text: str) -> None:
    with pytest.raises(DocSectionError, match="Append"):
        append_section(b"# A\n", under=("A",), text=text)


def test_append_and_rename_refuse_collisions_including_descendants() -> None:
    content = b"# A\n## X\nold\n## Y\n"
    for edit in [
        lambda: append_section(content, under=("A",), text="## X\nnew"),
        lambda: replace_section(content, section=("A", "Y"), text="## X\nnew"),
        lambda: append_section(content, text="# B\n## X\n## X\n"),
    ]:
        with pytest.raises(DocSectionError) as caught:
            edit()
        assert caught.value.code == "cruxible.doc.section_exists"


def test_replace_subtree_rename_and_digest_cas() -> None:
    content = b"# A\n## X\nold\n### Child\nold child\n## Y\nkeep\n"
    digest = parse_sections(content).section(("A", "X")).digest
    result = replace_section(content, section=("A", "X"), text="## Renamed\nnew", expect=digest)
    assert result.content == b"# A\n## Renamed\nnew\n## Y\nkeep\n"
    assert result.section == ("A", "Renamed")
    assert result.before_digest == digest
    assert not replace_section(
        result.content, section=("A", "X"), text="## Renamed\nnew", expect=digest
    ).changed
    with pytest.raises(DocSectionError) as caught:
        replace_section(content, section=("A", "X"), text="## X\nnew", expect="sha256:" + "0" * 64)
    assert caught.value.code == "cruxible.doc.section_changed"


@pytest.mark.parametrize("text", ["plain", "# Wrong", "## X\n## Peer", "## X\n# Escape"])
def test_replace_refuses_non_subtree(text: str) -> None:
    with pytest.raises(DocSectionError):
        replace_section(b"# A\n## X\n", section=("A", "X"), text=text)


def test_preamble_replacement_cannot_insert_headings() -> None:
    with pytest.raises(DocSectionError):
        replace_section(b"intro\n# A\n", section=(), text="# New")
    assert replace_section(b"intro\n# A\n", section=(), text="").content == b"# A\n"


def test_crlf_preserved_and_identical_replacement_is_noop() -> None:
    content = b"# A\r\nold\r\n# B\r\n"
    result = replace_section(content, section=("A",), text="# A\nnew")
    assert result.content == b"# A\r\nnew\r\n# B\r\n"
    assert not replace_section(result.content, section=("A",), text="# A\nnew").changed


@pytest.mark.parametrize("text", ["# A\nbody", "---\nagent: chosen\n---\n# A\nbody"])
def test_missing_file_contains_only_agent_text_and_retry_converges(text: str) -> None:
    result = append_section(None, text=text, file="chosen.markdown")
    assert result.content == (text + "\n").encode()
    assert not append_section(result.content, text=text).changed


@pytest.mark.parametrize("content,file", [(b"\xff", "x.md"), (b"# A", "x.txt")])
def test_not_markdown_refusal(content: bytes, file: str) -> None:
    with pytest.raises(DocSectionError) as caught:
        parse_sections(content, file=file)
    assert caught.value.code == "cruxible.doc.not_markdown"


def test_section_digests_survive_unrelated_edits_and_own_body_survives_child_append() -> None:
    content = b"intro\n# A\nbody\n# B\nother\n"
    before = parse_sections(content)
    other = replace_section(content, section=("B",), text="# B\nchanged")
    assert parse_sections(other.content).section(("A",)).digest == before.section(("A",)).digest
    child = append_section(content, under=("A",), text="## Child\nnew")
    assert parse_sections(child.content).own_body(("A",)) == before.own_body(("A",))
    assert parse_sections(child.content).section(("A",)).digest != before.section(("A",)).digest
