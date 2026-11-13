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


class AuthoringProposed(BaseModel):
    """Why submit stopped; a refused preflight has no proposal yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["proposed"] = "proposed"
    proposal_id: str | None = None
    reason: Literal[
        "independent_approval_required",
        "activation_not_permitted",
        "accept_never",
        "preflight_refused",
        "not_ready_to_activate",
        "head_moved",
    ]
    message: str
    next: str | None = None


AuthoringLanding: TypeAlias = Annotated[
    AuthoringLanded | AuthoringProposed, Field(discriminator="status")
]
