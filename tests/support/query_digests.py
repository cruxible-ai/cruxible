"""Digests of rebuilt read models, so tests can compare two rebuilds byte for byte."""

from __future__ import annotations

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.query.backends import SubjectQueryViewV1
from cruxible_core.query.cards import ClaimTypeCardV1, SubjectProfileV1

CLAIM_TYPE_CARD_DIGEST_DOMAIN = "playbill-claim-type-card-v1"
SUBJECT_PROFILE_DIGEST_DOMAIN = "playbill-subject-profile-v1"
VIEW_DIGEST_DOMAIN = "playbill-subject-query-view-v1"


def claim_type_card_digest(card: ClaimTypeCardV1) -> str:
    """Digest one card so a rebuild can be compared without re-reading the facts."""

    payload = card.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, CLAIM_TYPE_CARD_DIGEST_DOMAIN, payload).tagged


def subject_profile_digest(profile: SubjectProfileV1) -> str:
    """Digest one profile so a rebuild can be compared without re-reading the facts."""

    payload = profile.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, SUBJECT_PROFILE_DIGEST_DOMAIN, payload).tagged


def render_subject_query_view(view: SubjectQueryViewV1) -> bytes:
    """Return the canonical wire bytes of one materialized Subject view."""

    return canonical_bytes(view.model_dump(mode="json")) + b"\n"


def subject_query_view_digest(view: SubjectQueryViewV1) -> str:
    """Digest one materialized Subject view for replay and rebuild comparison."""

    payload = view.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, VIEW_DIGEST_DOMAIN, payload).tagged


__all__ = [
    "claim_type_card_digest",
    "render_subject_query_view",
    "subject_profile_digest",
    "subject_query_view_digest",
]
