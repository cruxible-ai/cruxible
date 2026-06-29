"""CLI compatibility re-export for the runtime instance implementation."""

import json

from cruxible_core.runtime.instance import CruxibleInstance

# Preserve legacy patch targets like cruxible_core.cli.instance.json.dump.

__all__ = ["CruxibleInstance", "json"]
