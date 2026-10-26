"""Bounded caller choices for a Procedure's declared Provider slots."""

from typing import Annotated, TypeAlias

from pydantic import Field

MAX_PROCEDURE_BINDINGS = 256
MAX_PROCEDURE_SLOT_NAME = 256
# A canonical artifact name (384) optionally prefixed with "Provider:" (9).
MAX_PROCEDURE_PROVIDER_REFERENCE = 393

ProcedureBindings: TypeAlias = Annotated[
    dict[
        Annotated[str, Field(min_length=1, max_length=MAX_PROCEDURE_SLOT_NAME)],
        Annotated[str, Field(min_length=1, max_length=MAX_PROCEDURE_PROVIDER_REFERENCE)],
    ],
    Field(max_length=MAX_PROCEDURE_BINDINGS),
]
