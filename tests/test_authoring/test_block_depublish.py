"""Block depublish releases only blocks a registration names.

The insertion/publication road is cut: nothing prepares, confirms or abandons a
publication, and `block depublish` releases only blocks declared with
`block repin`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import FormatError
from cruxible_core.service.proposals.publications import service_depublish_playbill_block
from tests.core_support._support import initialize_local


def test_depublishing_a_block_no_registration_names_refuses_by_name(tmp_path: Path) -> None:
    """Releasing a block nothing registers refuses by name, and only by name."""

    instance, _owner = initialize_local(tmp_path)

    with pytest.raises(FormatError, match="cruxible.block.not_registered"):
        service_depublish_playbill_block(
            instance,
            source_id="repo.work-items",
            block_id="nothing-registers-this",
        )
