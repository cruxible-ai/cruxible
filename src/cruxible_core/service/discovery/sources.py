"""Each logical source runs have landed Captures from: ``orient(section="sources")``.

The answer comes from the landing records, the same ``produced_capture``
records the activity feed lists as ``capture_landed``, whether or not any Claim
cites what landed. Each landing is projected once into the Procedure-run
journal's index (its contract, its logical source read from its Capture's
envelope, its instant), the projection the Capture-landing Triggers already
keep, and triggers there keep per-(source, contract) landing tallies. A page
walks those tallies by source name and seeks each source's latest landing, so
it costs its sources and their contracts, never their landings.

Landings are operational state, not accepted state: like the activity feed,
the section is read live at the current head whatever coordinate the read
names, and says so with ``live``; it also reads the body store live, never at
the enclosing read's landing-log position, since what the landing projection
records is kept. Contract names and versions resolve at the
head. The landing records are best-effort exhaust: a landing whose payload or
Capture envelope is lost when it is projected cannot be attributed to a
source and is counted in ``sources_unattributed``; a latest landing whose
payload or envelope is lost since is listed ``unavailable``, by its run.
A Capture that names no logical source (a Procedure's emitted output) is in
no row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cruxible_client.contracts.errors import CasError
from cruxible_client.contracts.operational_reads import (
    GetCaptureErasure,
    OrientSource,
    OrientSourceContract,
    OrientSourceLanding,
    capture_handle,
)
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_core.exhaust.journal_index import SourceLandings
from cruxible_core.exhaust.payloads import UnavailablePayload
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.contract_names import CaptureContractNames
from cruxible_core.service.discovery.operational import capture_availability
from cruxible_core.service.discovery.runs import (
    procedure_run_journal,
    procedure_run_stream,
    run_identity,
    run_payload,
)
from cruxible_core.storage.capture_metadata import capture_envelope
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.read_positions import unpositioned_operation

_ACCESS = BodyAccessContext(principal_id="playbill-source-reads", can_read_body=True)


@unpositioned_operation
def source_page(
    instance: PlaybillInstance, *, limit: int, after: str | None
) -> tuple[tuple[OrientSource, ...], str | None, int]:
    """One page of logical sources by name, where the next continues, and unattributed landings.

    Read live, never at the enclosing read's log positions: the projection it
    triggers keeps what it records, and a landing newer than those positions
    would otherwise read as lost here (``JournalIndex._project_landings``).
    """

    journal = procedure_run_journal(instance)
    if journal is None:
        return (), None, 0
    stream = procedure_run_stream(instance)
    page = journal.index.source_landings(
        stream, bodies=instance.body_store(), limit=limit, after=after
    )
    head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(head) as projection:
        names = CaptureContractNames(instance, head, connection=projection.typed.connection)
        contracts = {item.source: _contracts(names, item) for item in page.sources}
    from cruxible_core.consumers.next.evidence import evidence_findings_for

    def findings_of(digest: str) -> Sequence[Any]:
        return evidence_findings_for(instance, (digest,))

    rows = tuple(
        OrientSource(
            logical_source=item.source,
            landings=sum(count for _, count in item.counts),
            latest=_latest(instance, journal, stream, item, findings_of),
            contracts=contracts[item.source],
        )
        for item in page.sources
    )
    return rows, page.stop, page.unattributed


def _contracts(
    names: CaptureContractNames, item: SourceLandings
) -> tuple[OrientSourceContract, ...]:
    rows = []
    for digest, count in item.counts:
        name = names.name(digest, qualified=True)
        rows.append(
            OrientSourceContract(
                contract=name,
                version=None
                if name.startswith("unresolved:")
                else names.version_number(name, digest),
                landings=count,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.contract, row.version or 0)))


def _latest(
    instance: PlaybillInstance,
    journal: Any,
    stream: Any,
    item: SourceLandings,
    findings: Any,
) -> OrientSourceLanding:
    """The latest landing: its record's run and Line, its payload's Capture, that envelope."""

    locator = item.latest
    landed_at = parse_datetime(locator.recorded_at)
    assert landed_at is not None
    run = str(locator.run_id)
    admission = journal.index.run_admission_digest(stream, run)
    identity = None if admission is None else run_identity(instance, admission)
    base: dict[str, Any] = {
        "landed_at": landed_at,
        "run": run,
        "line": None if identity is None else identity[1],
    }
    payload = run_payload(instance, locator.payload_digest)
    digest = payload.get("capture_digest") if isinstance(payload, Mapping) else None
    if isinstance(payload, UnavailablePayload) or not isinstance(digest, str):
        return OrientSourceLanding(**base, unavailable=True)
    base.update(capture=capture_handle(digest), ref=f"Capture:{digest}")
    try:
        envelope = capture_envelope(instance.body_store(), digest, access=_ACCESS)
    except (CasError, OSError, ValueError):
        return OrientSourceLanding(**base, unavailable=True)
    status, detail, erasure = capture_availability(instance, digest, findings_of=findings)
    return OrientSourceLanding(
        **base,
        observed_at=envelope.observed_at,
        size=envelope.commitment.byte_length,
        status=status,
        status_detail=detail,
        erased=None
        if erasure is None
        else GetCaptureErasure(by=erasure.erased_by, at=erasure.erased_at, because=erasure.because),
    )


__all__ = ["source_page"]
