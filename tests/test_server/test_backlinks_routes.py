"""Backlinks over HTTP through the SDK: the daemon's index, the workspace's drift."""

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
    client, instance_id, _ = playbill_http
    raw = client.backlinks(instance_id, request=BacklinksRequest(ref=f"{KIND}/wi-1"))
    assert raw.links[0].drift == "unknown"
