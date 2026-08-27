"""Freeze the complete sanctioned persistent derivative-text writer inventory."""

from __future__ import annotations

import inspect

from cruxible_client.authoring.blocks import repin_projection_block
from cruxible_client.authoring.insertions import apply_playbill_publication


def test_every_sanctioned_derivative_writer_uses_the_shared_block_assertion() -> None:
    writers = {
        "projection_repin": repin_projection_block,
        "publication_v2": apply_playbill_publication,
    }

    assert set(writers) == {"projection_repin", "publication_v2"}
    for writer in writers.values():
        assert "assert_projection_block_frame(" in inspect.getsource(writer)
