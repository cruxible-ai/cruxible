"""Served preflight results for client and surface stubs.

The served `AuthoringPreflightResult` carries the daemon's own typed,
self-digesting certificate and bounded frontier, so a stub cannot hand a reader
a loose dict any more. These builders mint a valid one from a stored
certificate, varying only what a stub needs to say.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import (
    AuthoringDiagnostic,
    DiagnosticFrontier,
    PreflightCertificate,
    build_preflight_certificate,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

_STORED = Path(__file__).resolve().parents[1] / "fixtures/pre_hotfix_stored_records_0960559c.json"

#: The intent the stored certificate names.
STUB_INTENT_ID = "AIT-00000000000000000000000000000001"


def stub_diagnostic(code: str, message: str = "refused", **values: Any) -> AuthoringDiagnostic:
    """One refusal a stub frontier carries; the owner is the daemon so no repair is owed."""

    return AuthoringDiagnostic.model_validate(
        {
            "code": code,
            "stage": "preflight",
            "offending_element": "payload",
            "message": message,
            "owner": "daemon",
            "disposition": "terminal",
            **values,
        }
    )


def stub_preflight_result(
    *,
    verdict: Literal["passed", "refused"] = "passed",
    intent_id: str = STUB_INTENT_ID,
    diagnostics: Iterable[AuthoringDiagnostic | Mapping[str, Any]] = (),
    accepted_coordinate: Mapping[str, Any] | None = None,
    lint: Mapping[str, Any] | None = None,
) -> contracts.AuthoringPreflightResult:
    """A served preflight result whose certificate and frontier both validate."""

    frontier = DiagnosticFrontier(
        diagnostics=tuple(
            sorted(
                (AuthoringDiagnostic.model_validate(item) for item in diagnostics),
                key=lambda item: (
                    item.stage.encode(),
                    item.code.encode(),
                    item.offending_element.encode(),
                ),
            )
        )
    )
    stored = PreflightCertificate.model_validate(
        json.loads(_STORED.read_text(encoding="utf-8"))["preflight_certificate"]
    )
    # The builder digests a provisional model, so every value stays typed.
    values: dict[str, Any] = {name: getattr(stored, name) for name in type(stored).model_fields}
    del values["certificate_digest"]
    values.update(intent_id=intent_id, frontier_digest=frontier.digest)
    if accepted_coordinate is not None:
        values["accepted_coordinate"] = AcceptedCoordinate.model_validate(dict(accepted_coordinate))
    return contracts.AuthoringPreflightResult.model_validate(
        {
            "verdict": verdict,
            "certificate": build_preflight_certificate(**values).model_dump(mode="json"),
            "frontier": frontier.model_dump(mode="json"),
            **({} if lint is None else {"lint": dict(lint)}),
        }
    )
