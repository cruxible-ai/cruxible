"""The result of trying to land an authored change in the submit call."""

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import RepairOperation

AuthoringAccept: TypeAlias = Literal["if_allowed", "never"]


class AuthoringLanded(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["landed"] = "landed"
    proposal_id: str | None = None
    accepted_coordinate: AcceptedCoordinate
    #: Identical to accepted state: nothing was proposed and no generation made
    #: (S2); ``accepted_coordinate`` is the head it matched.
    unchanged: bool = Field(default=False, exclude_if=lambda value: value is False)

    def __str__(self) -> str:
        """``landed: accepted at <short oid>``; ``repr()`` is its first line."""

        from cruxible_client.contracts.text_display import landing_lines

        return "\n".join(landing_lines(self))

    def __repr__(self) -> str:
        """The first line of what the CLI prints (S1); ``model_dump()`` is the model."""

        from cruxible_client.contracts.text_display import one_line_repr

        return one_line_repr(self)


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
    #: The next step, spelled for the client that shows it (the daemon spells it
    #: for the CLI; ``landing_for_surface`` respells it from ``next_repair``).
    next: str | None = None
    #: The served operation ``next`` spells, with its arguments.
    next_repair: RepairOperation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def __str__(self) -> str:
        """Why it stopped and what to run; ``repr()`` is its first line."""

        from cruxible_client.contracts.text_display import landing_lines

        return "\n".join(landing_lines(self))

    def __repr__(self) -> str:
        """The first line of what the CLI prints (S1); ``model_dump()`` is the model."""

        from cruxible_client.contracts.text_display import one_line_repr

        return one_line_repr(self)


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
    next_repair: RepairOperation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def __str__(self) -> str:
        """Why it refused and what to run; ``repr()`` is its first line."""

        from cruxible_client.contracts.text_display import landing_lines

        return "\n".join(landing_lines(self))

    def __repr__(self) -> str:
        """The first line of what the CLI prints (S1); ``model_dump()`` is the model."""

        from cruxible_client.contracts.text_display import one_line_repr

        return one_line_repr(self)


AuthoringLanding: TypeAlias = Annotated[
    AuthoringLanded | AuthoringProposed | AuthoringRefused, Field(discriminator="status")
]

#: A next step this caller cannot take itself, handed to whoever can.
DELEGATED_PREFIX = "someone who can runs "


def landing_for_surface(
    outcome: AuthoringLanded | AuthoringProposed | AuthoringRefused | None,
    surface: Literal["cli", "mcp", "sdk"],
) -> AuthoringLanded | AuthoringProposed | AuthoringRefused | None:
    """The outcome with ``next`` spelled for ``surface`` from its structured repair."""

    from cruxible_client.contracts.repair_spelling import render_repair

    if isinstance(outcome, AuthoringLanded) or outcome is None or outcome.next_repair is None:
        return outcome
    delegated = (outcome.next or "").startswith(DELEGATED_PREFIX)
    line = render_repair(outcome.next_repair, surface=surface)
    return outcome.model_copy(update={"next": f"{DELEGATED_PREFIX}{line}" if delegated else line})
