"""Daemon admission checks for client-observed document edits; never file I/O."""

import base64

from cruxible_client.contracts.declared_blocks import declares_projection_block, stamped_projection_windows
from cruxible_client.contracts.write import DocEditObservation, DocOp
from cruxible_core.authoring.registrations import source_projection_blocks
from cruxible_core.runtime.instance import PlaybillInstance


def validate_doc_edits(instance: PlaybillInstance, docs: tuple[DocOp | DocEditObservation, ...]) -> None:
    # Per request: only supplied edits and blocks of their logical source. No
    # instance Claims/citations are inventoried (S4 is deferred).
    from cruxible_core.service.authoring.write_verbs import _refuse

    for index, edit in enumerate(docs):
        if not isinstance(edit, DocEditObservation):
            raise _refuse(
                "cruxible.write.doc_unobserved", "Document operations must be observed by the writer.",
                field_path=f"docs[{index}]", repair="Use the CLI, SDK or MCP write adapter beside the workspace",
            )
        blocks = source_projection_blocks(instance, edit.source_id)
        page = None if edit.source_content_base64 is None else base64.b64decode(edit.source_content_base64)
        windows = () if page is None else stamped_projection_windows(page)
        if blocks is None or (blocks and (page is None or set(blocks) - {w.block_id for w in windows})):
            raise _refuse(
                "cruxible.projection.window_unverifiable", "The edited source's projection windows cannot be verified.",
                field_path=f"docs[{index}]", repair="Present the original page with all registered projection markers",
            )
        if declares_projection_block(edit.content) or any(
            (edit.start_byte < w.end_byte and edit.end_byte > w.start_byte)
            or (edit.start_byte == edit.end_byte and w.start_byte <= edit.start_byte < w.end_byte)
            for w in windows
        ):
            raise _refuse(
                "cruxible.doc.projection_edit", "Document operations cannot edit projection blocks.",
                field_path=f"docs[{index}]", repair="Choose prose outside the block; use block repin to update it",
            )
