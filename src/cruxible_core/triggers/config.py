"""Internal trigger intervals are daemon-local operational choices, never governed law."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TRIGGER_CONFIG_PATH = Path("daemon/triggers.json")


class TriggerOperationalConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-trigger-operational-config-v1"] = (
        "cruxible-trigger-operational-config-v1"
    )
    evidence_sweep_interval_seconds: int = Field(default=86400, gt=0, strict=True)
    prediction_anchor_retry_interval_seconds: int = Field(default=3600, gt=0, strict=True)

    def cadences(self) -> dict[str, timedelta]:
        return {
            "evidence.sweep": timedelta(seconds=self.evidence_sweep_interval_seconds),
            "prediction.anchor_retry": timedelta(
                seconds=self.prediction_anchor_retry_interval_seconds
            ),
        }


def load_trigger_config(state_root: Path) -> TriggerOperationalConfigV1:
    path = state_root / TRIGGER_CONFIG_PATH
    if not path.exists() and not path.is_symlink():
        return TriggerOperationalConfigV1()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Trigger config must be a regular file")
    return TriggerOperationalConfigV1.model_validate_json(path.read_bytes())
