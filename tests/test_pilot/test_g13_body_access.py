"""G13: a restricted CaptureKind's body is the inventory view without the key (C14).

The KEV pilot runs under a ``restricted`` CaptureKind. A reader without
``cruxible_body_read_restricted`` gets the inventory view and a typed
``body_restricted`` notice everywhere the Capture's bytes or body-derived
metadata would show: the Capture read, its proof, the Claim's source handles,
the run's output and findings, and the inbox items its values reached. Accepted
Claim values are never withheld. A reader holding the key sees the bytes.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.runs import procedure_run_status
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from cruxible_core.service.procedures.line_inbox import service_get_inbox_item, service_list_inbox
from tests.test_pilot.support import FIXTURES, KEV, pilot_world
from tests.test_procedures.test_claims_terminal import activate, candidate_claims, run

READER = BodyAccessContext(principal_id="reader", can_read_body=True)
KEY_HOLDER = BodyAccessContext(
    principal_id="reviewer", can_read_body=True, can_read_restricted=True
)
KIND = "CaptureContract:test.orders-v1"


def _restricted_world(tmp_path: Path, fixture: str = "kev.json"):  # type: ignore[no-untyped-def]
    instance, root = pilot_world(tmp_path, fixture, capture_body_access="restricted")
    state = run(instance, root)
    (observation,) = [o for o in state.source_observations if o.capture_digest]
    return instance, state, observation.capture_digest


def test_g13_restricted_body_is_the_inventory_view_and_the_key_reads_bytes(
    tmp_path: Path,
) -> None:
    instance, state, digest = _restricted_world(tmp_path)
    request = CaptureReadRequest(capture_digest=digest)

    withheld = service_read_playbill_capture(instance, request=request, access=READER)
    assert withheld.status == "restricted"
    assert withheld.reason == "body_restricted"
    assert withheld.envelope is None and withheld.material is None
    assert withheld.contract_address == "capture-contracts/test.orders-v1.json"
    assert withheld.notice is not None
    assert withheld.notice.capture_digests == (digest,)
    assert withheld.notice.capture_kinds == (KIND,)
    assert withheld.notice.required_permission == "cruxible_body_read_restricted"

    read = service_read_playbill_capture(instance, request=request, access=KEY_HOLDER)
    assert read.status == "verified" and read.notice is None
    assert read.material is not None and read.material.status == "verified"
    from cruxible_client.authoring.sdk_types import CaptureView

    assert CaptureView(result=read).content == (FIXTURES / "kev.json").read_bytes()

    # Accepted Claim values are never withheld; the Claim's cited sources are.
    claims = candidate_claims(instance, state)
    activate(instance, state)
    assert {c.statement.predicate for c in claims} >= {KEV + "listed", KEV + "date_added"}
    claim = claims[0].identity.qualified
    why = service_playbill_get(
        instance, request=GetRequest(ref=claim, detail="why"), access=READER
    ).model_dump(mode="json")
    explanation = why["why"]
    assert explanation["source_handles"] == []
    assert explanation["withheld"]["capture_digests"] == [digest]
    unrestricted = service_playbill_get(
        instance, request=GetRequest(ref=claim, detail="why"), access=KEY_HOLDER
    ).model_dump(mode="json")["why"]
    assert "withheld" not in unrestricted
    assert [h["access_class"] for h in unrestricted["source_handles"]] == ["restricted"]
    card = service_playbill_get(instance, request=GetRequest(ref=claim), access=READER)
    assert card.model_dump(mode="json")["card"]["value"] is not None


def test_g13_capture_proof_and_citation_row_carry_the_kinds_access(tmp_path: Path) -> None:
    instance, state, digest = _restricted_world(tmp_path)
    activate(instance, state)
    ref = "Capture:" + digest
    proof = service_playbill_get(
        instance, request=GetRequest(ref=ref, detail="proof"), access=READER
    ).model_dump(mode="json")["proof"]
    assert proof["envelope"] is None
    assert proof["notice"]["kind"] == "body_restricted"
    opened = service_playbill_get(
        instance, request=GetRequest(ref=ref, detail="proof"), access=KEY_HOLDER
    ).model_dump(mode="json")["proof"]
    assert opened["envelope"]["source"] is not None
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (access,) = projection.typed.connection.execute(
            "SELECT access_class FROM captures WHERE capture_digest=?", (digest,)
        ).fetchone()
    assert access == "restricted"
    card = service_playbill_get(instance, request=GetRequest(ref=ref), access=READER).model_dump(
        mode="json"
    )
    assert card["card"]["access"] == "restricted"


def test_g13_run_values_and_inbox_items_inherit_the_restriction(tmp_path: Path) -> None:
    instance, state, digest = _restricted_world(tmp_path, "kev-invalid.json")
    assert state.run_id is not None and state.findings
    status = procedure_run_status(instance, state.run_id, viewer=None, access=READER)
    assert status.withheld is not None and status.withheld.capture_digests == (digest,)
    assert status.findings == [] and status.result is None
    assert all(node.summary is None for node in status.nodes)
    opened = procedure_run_status(instance, state.run_id, viewer=None, access=KEY_HOLDER)
    assert opened.withheld is None and opened.findings

    page = service_list_inbox(instance, limit=20, access=READER)
    (entry,) = page.entries
    assert entry.payload_preview is None and entry.withheld is not None
    item = service_get_inbox_item(instance, item=entry.item.item_id, access=READER)
    assert item.payload is None and item.withheld is not None
    assert item.withheld.capture_kinds == (KIND,)
    whole = service_get_inbox_item(instance, item=entry.item.item_id, access=KEY_HOLDER)
    assert whole.withheld is None and whole.payload["lexeme"] == "Maybe"  # type: ignore[index]


def test_an_open_kind_withholds_nothing(tmp_path: Path) -> None:
    instance, root = pilot_world(tmp_path, "kev-invalid.json")
    state = run(instance, root)
    (observation,) = [o for o in state.source_observations if o.capture_digest]
    read = service_read_playbill_capture(
        instance,
        request=CaptureReadRequest(capture_digest=observation.capture_digest),
        access=READER,
    )
    assert read.status == "verified" and read.notice is None
    assert read.material is not None and read.material.status == "verified"
    assert state.run_id is not None
    status = procedure_run_status(instance, state.run_id, viewer=None, access=READER)
    assert status.withheld is None and status.findings
    (entry,) = service_list_inbox(instance, limit=20, access=READER).entries
    assert entry.withheld is None and entry.payload_preview is not None
