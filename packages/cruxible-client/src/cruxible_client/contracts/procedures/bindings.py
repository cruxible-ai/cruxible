"""Bounded caller choices for a Procedure's declared slots.

A binding is keyed by slot name, or ``<invoke node path>.<slot>`` for a slot of
an invoked child, and names ``provider[/build]#operation`` or
``provider[/build]`` (when the build serves exactly one operation fitting the
slot's signature).
"""

from typing import Annotated, TypeAlias

from pydantic import Field

MAX_PROCEDURE_BINDINGS = 256
MAX_PROCEDURE_SLOT_NAME = 512
# "Provider:" (9) + a canonical artifact name (384) + "#" + an operation name (256).
MAX_PROCEDURE_PROVIDER_REFERENCE = 650

ProcedureBindings: TypeAlias = Annotated[
    dict[
        Annotated[str, Field(min_length=1, max_length=MAX_PROCEDURE_SLOT_NAME)],
        Annotated[str, Field(min_length=1, max_length=MAX_PROCEDURE_PROVIDER_REFERENCE)],
    ],
    Field(max_length=MAX_PROCEDURE_BINDINGS),
]
