"""Signed, base-bound compiler transition carried by an ordinary proposal."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.candidates import LawEvaluationCoordinate
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.types import CompilerCoordinate

COMPILER_UPGRADE_PATH = "compiler-upgrade.json"


class CompilerUpgradeNotice(BaseModel):
    """What sets a compiler upgrade apart from every other proposal: it is one-way.

    Carried by the upgrade's preview, its proposal and its review, where the
    decision to activate is made. Once activated, the accepted head names
    ``target_compiler_digest``, and a release whose daemon does not install
    that compiler can no longer serve the instance; there is no downgrade.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["cruxible-compiler-upgrade-notice-v1"] = "cruxible-compiler-upgrade-notice-v1"
    source_compiler_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_revision: str | None = None
    target_compiler_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_revision: str | None = None
    one_way: Literal[True] = True
    message: str = Field(min_length=1)


class CompilerUpgrade(BaseModel):
    """A transition, not a mutable selector that can rewrite historical meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-compiler-upgrade-v1"] = "playbill-compiler-upgrade-v1"
    instance_id: str = Field(min_length=1)
    base: LawEvaluationCoordinate
    target: CompilerCoordinate


def render_compiler_upgrade(value: CompilerUpgrade) -> bytes:
    return canonical_bytes(value.model_dump(mode="json")) + b"\n"


def parse_compiler_upgrade(content: bytes) -> CompilerUpgrade:
    value = CompilerUpgrade.model_validate_json(content)
    if render_compiler_upgrade(value) != content:
        raise ValueError("compiler upgrade record must be canonical")
    return value
