"""The backlink index and its reads (D-R12, D-R17).

One index, keyed by target, joins three pointer kinds: section citations
recorded by ``write``, notes linking a workspace section ``about`` Subjects,
and governed Document links (``about``, ``guides``). Reads are keyset pages;
a cited section whose bytes moved reads ``changed_since_cited``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.backlinks import BacklinksRequest
from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.documents import (
    DocumentLifecycle,
    DocumentLink,
    DocumentShell,
)
from cruxible_client.contracts.errors import ReadRefusalError
from cruxible_client.contracts.write import SectionEvidence, SetChange, WriteRequest
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_store_playbill_body,
)
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.backlinks import (
    review_guidance,
    service_playbill_backlinks,
)
from cruxible_core.service.list_pages import ListCursorMismatch
from tests.core_support._write_support import KIND, caller, seed_write_surface
from tests.test_service.test_notes import SOURCE, about, observed

STATUS = f"{KIND}.status"


def write(instance, *changes):
    result = service_playbill_write(
        instance, request=WriteRequest(because="Context", changes=changes), caller=caller()
    )
    assert result.status == "accepted", result
    return result


def cite(subject: str, value: str, text: bytes = b"# Reason\nWords\n") -> SetChange:
    return SetChange(
        subject=f"{KIND}/{subject}",
        field="status",
        value=value,
        evidence=SectionEvidence(file=SOURCE, section=("Reason",), observation=observed(text)),
    )


def links(instance, **fields):
    return service_playbill_backlinks(instance, request=BacklinksRequest(**fields))


def propose_document(instance, name: str, *links: DocumentLink) -> None:
    body = service_store_playbill_body(instance, content=b"# Review\nRead the source.\n").digest
    proposal = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity=f"document:{name}",
            document_kind="guidance",
            title=f"Guidance {name}",
            media_type="text/markdown",
            body_digest=body,
            links=tuple(sorted(links, key=lambda item: (item.relation, item.target_identity))),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name=f"doc-{name}",
        timestamp="2026-09-29T12:30:00.000000Z",
    )
    assert proposal.proposal.candidate is not None, proposal
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal.proposal.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt


def test_subject_reads_its_notes_and_the_sections_its_claims_cite(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    write(instance, about(f"{KIND}/wi-1"), cite("wi-1", "ready"))
    result = links(instance, ref=f"{KIND}/wi-1")
    assert [(link.relation, link.source_kind) for link in result.links] == [
        ("about", "Note"),
        ("cites", "Claim"),
    ]
    note, citation = result.links
    assert (note.file, note.section) == (SOURCE, ("Reason",))
    assert citation.subject == f"{KIND}/wi-1" and citation.field == STATUS
    assert citation.cited_digest == section_digest(b"# Reason\nWords\n")
    assert {note.drift, citation.drift} == {"unknown"}
    # From the section: the Claims resting on it.
    section = links(instance, file=SOURCE, section=("Reason",))
    assert [link.source for link in section.links] == [citation.source]
    # A ClaimType no Document guides has no guidance; any other kind refuses.
    assert links(instance, ref=STATUS).links == ()
    claim = citation.source
    with pytest.raises(ReadRefusalError) as refused:
        links(instance, ref=claim)
    assert refused.value.error_code == "cruxible.backlinks.target_unsupported"


def test_retired_claim_and_unlinked_note_leave_the_index(tmp_path: Path) -> None:
    from cruxible_client.contracts.write import AboutChange, RetireChange

    instance, _ = seed_write_surface(tmp_path)
    outcome = write(instance, about(f"{KIND}/wi-1"), cite("wi-1", "ready"))
    claim = next(change.claim for change in outcome.changes if change.op == "set")
    write(
        instance,
        RetireChange(target=claim),
        AboutChange(file=SOURCE, section=("Reason",), unlink=(f"{KIND}/wi-1",)),
    )
    assert links(instance, ref=f"{KIND}/wi-1").links == ()
    assert links(instance, file=SOURCE, section=("Reason",)).links == ()


def test_a_later_observation_with_other_bytes_marks_earlier_citations_changed(
    tmp_path: Path,
) -> None:
    instance, _ = seed_write_surface(tmp_path)
    write(instance, cite("wi-1", "ready"))
    write(instance, cite("wi-2", "ready", text=b"# Reason\nWords, revised\n"))
    rows = {link.subject: link for link in links(instance, file=SOURCE, section=("Reason",)).links}
    assert rows[f"{KIND}/wi-1"].drift == "changed_since_cited"
    assert rows[f"{KIND}/wi-2"].drift == "unknown"


def test_pages_are_keyset_ranges_continued_by_cursor(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    write(instance, about(f"{KIND}/wi-1"), cite("wi-1", "ready"))
    first = links(instance, ref=f"{KIND}/wi-1", limit=1)
    assert first.truncated and first.next_cursor is not None
    second = links(instance, ref=f"{KIND}/wi-1", limit=1, cursor=first.next_cursor)
    assert [link.relation for link in (*first.links, *second.links)] == ["about", "cites"]
    assert not second.truncated and second.next_cursor is None
    with pytest.raises(ListCursorMismatch):
        links(instance, ref=f"{KIND}/wi-2", cursor=first.next_cursor)


def test_document_links_index_about_and_guides_and_review_shows_guidance(
    tmp_path: Path,
) -> None:
    instance, _ = seed_write_surface(tmp_path)
    propose_document(
        instance,
        "status-review",
        DocumentLink(relation="guides", target_identity=f"ClaimType:{STATUS}"),
        DocumentLink(relation="about", target_identity=f"Subject:{KIND}/wi-1"),
        DocumentLink(relation="see-also", target_identity="document:elsewhere"),
    )
    guidance = links(instance, ref=STATUS)
    assert [(link.relation, link.source, link.title) for link in guidance.links] == [
        ("guides", "document:status-review", "Guidance status-review")
    ]
    subject = links(instance, ref=f"{KIND}/wi-1")
    assert [(link.relation, link.source_kind) for link in subject.links] == [("about", "Document")]
    shown = review_guidance(instance, (f"ClaimType:{STATUS}", "ClaimType:none.such"))
    assert [(item.definition, item.document) for item in shown] == [
        (f"ClaimType:{STATUS}", "document:status-review")
    ]


def test_document_links_refuse_wrong_kinds_and_dead_targets(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Review\n").digest

    def propose(link: DocumentLink):
        return service_propose_playbill_document(
            instance,
            shell=DocumentShell(
                identity="document:bad",
                document_kind="guidance",
                title="Bad",
                media_type="text/markdown",
                body_digest=body,
                links=(link,),
                governance_scope=("project:playbill",),
                lifecycle=DocumentLifecycle(revision=1),
            ),
            actor_id="owner",
            proposal_name="doc-bad",
            timestamp="2026-09-29T12:30:00.000000Z",
            dry_run=True,
        )

    def codes(result) -> set[str]:
        evaluation = result.proposal.evaluation
        return {item.code for item in evaluation.diagnostics}

    wrong = propose(DocumentLink(relation="guides", target_identity=f"Subject:{KIND}/wi-1"))
    assert "cruxible.document.link_target_invalid" in codes(wrong), wrong
    dead = propose(DocumentLink(relation="guides", target_identity="ClaimType:none.such"))
    assert "cruxible.document.link_target_unavailable" in codes(dead), dead
