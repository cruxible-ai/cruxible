"""The result of trying to land an authored change in the submit call."""

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.projection import AcceptedCoordinate

AuthoringAccept: TypeAlias = Literal["if_allowed", "never"]


class AuthoringLanded(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["landed"] = "landed"
    proposal_id: str | None = None
    accepted_coordinate: AcceptedCoordinate

    def __str__(self) -> str:
        """``landed: accepted at <short oid>``; ``repr()`` stays the full model."""

        from cruxible_client.contracts.text_display import landing_lines

        return "\n".join(landing_lines(self))


class AuthoringProposed(BaseModel):
    """Why submit stopped short of landing a proposal it made, or one not yet ready."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["proposed"] = "proposed"
    proposal_id: str | None = None
    reason: Literal[
        "independent_approval_required",
        "activation_not_permitted",
        "accept_never",
        "not_ready_to_activate",
        "head_moved",
    ]
    message: str
    next: str | None = None

    def __str__(self) -> str:
        """Why it stopped and what to run; ``repr()`` stays the full model."""

        from cruxible_client.contracts.text_display import landing_lines

        return "\n".join(landing_lines(self))


class AuthoringRefused(BaseModel):
    """Submit proposed nothing: the payload failed its preflight.

    The intent's preflight diagnostics name each fault. Preflight already
    re-checks a refused intent at the head, so running it again changes
    nothing: the repair is a revised payload, resubmitted to the same intent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["refused"] = "refused"
    reason: Literal["preflight_refused"] = "preflight_refused"
    message: str
    next: str | None = None

    def __str__(self) -> str:
        """Why it refused and what to run; ``repr()`` stays the full model."""

        from cruxible_client.contracts.text_display import landing_lines

        return "\n".join(landing_lines(self))


AuthoringLanding: TypeAlias = Annotated[
    AuthoringLanded | AuthoringProposed | AuthoringRefused, Field(discriminator="status")
]
