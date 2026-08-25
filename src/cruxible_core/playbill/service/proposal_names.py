"""Shared lowering for human proposal labels at Playbill service boundaries."""

from cruxible_client.contracts.proposal_models import canonical_proposal_ref_name
from cruxible_core.errors import DataValidationError


def canonical_playbill_proposal_name(display_name: str, *, family: str) -> str:
    """Return one ref-safe name or a transport-safe typed validation refusal."""

    try:
        return canonical_proposal_ref_name(display_name)
    except ValueError as exc:
        raise DataValidationError(
            f"Playbill {family} proposal name is invalid",
            errors=[str(exc)],
        ) from exc


__all__ = ["canonical_playbill_proposal_name"]
