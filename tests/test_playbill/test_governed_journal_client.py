"""Open governed-journal protocol and untrusted HTTP peer behavior."""

from __future__ import annotations

import inspect

from cruxible_core.playbill.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    GovernedJournalClientProtocol,
    RemoteJournalConflict,
    RemoteJournalError,
    RemoteJournalRefusal,
    RemoteJournalTransportError,
    RemoteJournalVerificationError,
)
from cruxible_core.playbill.exhaust import governed


def test_governed_client_public_contract_has_no_private_service_vocabulary() -> None:
    public = [
        *governed.__all__,
        str(inspect.signature(GovernedJournalClientProtocol.append)),
        *(getattr(governed, name).__doc__ or "" for name in governed.__all__),
    ]
    words = {word.lower().strip(".,:;`()") for text in public for word in text.split()}
    assert words.isdisjoint({"tenant", "account", "quota", "credit", "billing", "org"})
    assert "idempotency_key" not in inspect.signature(
        GovernedJournalClientProtocol.append
    ).parameters


def test_remote_refusals_compose_with_the_journal_error_family() -> None:
    refusal = RemoteJournalRefusal(remote_status=429, refusal_id="home-refusal-17")
    conflict = RemoteJournalConflict(remote_status=409, refusal_id="journal_law_refused")

    assert isinstance(refusal, PlaybillJournalError)
    assert isinstance(conflict, RemoteJournalRefusal)
    assert refusal.remote_status == 429
    assert refusal.refusal_id == "home-refusal-17"
    assert issubclass(RemoteJournalTransportError, RemoteJournalError)
    assert issubclass(RemoteJournalVerificationError, RemoteJournalError)
