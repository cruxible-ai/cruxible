"""Shared base for the Procedure graph grammar modules.

The strict model base and canonical identifier checks used by
:mod:`~cruxible_client.contracts.procedures.models`,
:mod:`~cruxible_client.contracts.procedures.slots` and
:mod:`~cruxible_client.contracts.procedures.nodes`.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict

_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_NODE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


class _StrictProcedureModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, json_schema_mode_override="validation"
    )


def _canonical_identifier(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    if unicodedata.normalize("NFC", value) != value or not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value
