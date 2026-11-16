"""Backlinks over HTTP through the SDK: the daemon's index, the workspace's drift."""

import yaml

from cruxible_client.contracts.backlinks import BacklinksRequest
from tests.core_support._write_support import KIND
from tests.test_server.test_doc_writes import _batch, _context


def test_sdk_backlinks_read_the_index_and_check_drift_in_the_workspace(playbill_http, tmp_path):
    cx, _instance, workspace = _context(playbill_http, tmp_path)
    written = _batch(cx).write()
    assert written.status == "accepted", written
    claim = written.changes[0].claim
    by_subject = cx.backlinks(f"{KIND}/wi-1")
    (link,) = by_subject.links
    assert (link.relation, link.source, link.drift) == ("cites", claim, "unchanged")
    by_section = cx.backlinks(file="notes.md", section=("Decision",))
    assert [item.source for item in by_section.links] == [claim]
    (workspace / "notes.md").write_text("# Decision\nThe basis was edited by hand.\n")
    (drifted,) = cx.backlinks(file="notes.md", section=("Decision",)).links
    assert drifted.drift == "changed_since_cited"
    # The raw route answers without a workspace: the daemon cannot see the file.
    _client, instance_id, _ = playbill_http
    raw = cx._client.backlinks(instance_id, request=BacklinksRequest(ref=f"{KIND}/wi-1"))
    assert raw.links[0].drift == "unknown"


def test_sdk_note_appends_the_section_and_links_it_in_one_write(playbill_http, tmp_path):
    cx, instance, workspace = _context(playbill_http, tmp_path)
    before = instance.accepted_coordinate()
    outcome = cx.note(
        "notes.md",
        text="# Triage\nNot reachable from the entry point.\n",
        about=(f"{KIND}/wi-2",),
        because="Triage context",
    )
    assert outcome.status == "accepted" and outcome.docs[0].written, outcome
    assert instance.accepted_coordinate() != before
    (change,) = outcome.changes
    assert change.op == "about" and change.about == (f"{KIND}/wi-2",) and change.claim is None
    assert "# Triage" in (workspace / "notes.md").read_text()
    (link,) = cx.backlinks(f"{KIND}/wi-2").links
    assert (link.relation, link.source_kind, link.source) == ("about", "Note", change.note)
    assert link.drift == "unchanged"


def test_sdk_link_about_a_section_and_guides_a_claim_type(playbill_http, tmp_path):
    from cruxible_client.authoring.links import GuidanceLinkOutcome

    cx, _instance, workspace = _context(playbill_http, tmp_path)
    noted = cx.note(
        "notes.md",
        text="# Triage\nNot reachable.\n",
        about=(f"{KIND}/wi-1",),
        because="Triage context",
    )
    assert noted.status == "accepted", noted
    # The section by its catalog name, linked about one more Subject.
    linked = cx.link("doc-notes.md#Triage", about=(f"{KIND}/wi-2",), because="Also wi-2")
    assert linked.status == "accepted", linked
    (row,) = cx.backlinks(f"{KIND}/wi-2").links
    assert row.relation == "about"

    # The whole page as guidance of a ClaimType: one catalog entry, one Document.
    target = f"ClaimType:{KIND}.status"
    guided = cx.link("notes.md", guides=(target,))
    assert isinstance(guided, GuidanceLinkOutcome) and guided.catalog_written, guided
    assert guided.proposal.outcome is not None and guided.proposal.outcome.status == "landed"
    catalog = yaml.safe_load((workspace / ".cruxible" / "files.yaml").read_text())
    (entry,) = catalog["entries"]
    assert entry["links"] == [{"relation": "guides", "target_identity": target}]
    (guide,) = cx.backlinks(target).links
    assert guide.relation == "guides"
