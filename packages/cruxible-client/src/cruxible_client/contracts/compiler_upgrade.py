"""Signed, base-bound compiler transition carried by an ordinary proposal."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.candidates import LawEvaluationCoordinateV1
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.types import CompilerCoordinate

COMPILER_UPGRADE_PATH = "compiler-upgrade.json"


class CompilerUpgradeV1(BaseModel):
    """A transition, not a mutable selector that can rewrite historical meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-compiler-upgrade-v1"] = "playbill-compiler-upgrade-v1"
    instance_id: str = Field(min_length=1)
    base: LawEvaluationCoordinateV1
    target: CompilerCoordinate


def render_compiler_upgrade(value: CompilerUpgradeV1) -> bytes:
    return canonical_bytes(value.model_dump(mode="json")) + b"\n"


def parse_compiler_upgrade(content: bytes) -> CompilerUpgradeV1:
    value = CompilerUpgradeV1.model_validate_json(content)
    if render_compiler_upgrade(value) != content:
        raise ValueError("compiler upgrade record must be canonical")
    return value
