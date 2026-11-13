"""The base of every authored Procedure step: identifiers, value templates, step options.

These live apart from :mod:`~cruxible_client.contracts.procedures.authored` so a
step defined beside its own stored node (the layout ``extract``) can build on
them while the stored node union and the authored step union both name it:
this module depends on no Procedure node.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import (  # noqa: F401  (named by Template)
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
)
from typing_extensions import TypeAliasType

from cruxible_client.contracts.refs import Ref  # noqa: F401  (named by Template)

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")]
StepId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,127}$")]
NodeId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]

#: A value template: literal JSON whose leaves may be typed references. A
#: literal string never names a reference (a reference is always a ``Ref``).
Template = TypeAliasType(
    "Template",
    "Annotated[Ref | dict[str, Template] | list[Template] | StrictStr | StrictBool"
    " | StrictInt | None, Field(union_mode='left_to_right')]",
)


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        json_schema_mode_override="validation",
    )


class StepOptions(_Model):
    """``id`` names the stored node; ``next`` jumps forward past fallthrough."""

    id: NodeId | None = None
    next: NodeId | None = None


__all__ = [
    "Name",
    "NodeId",
    "StepId",
    "StepOptions",
    "Template",
]
