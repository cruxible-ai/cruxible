"""Observe `next` rows before presentation folds supporting evidence away."""

from __future__ import annotations

from typing import Any
from unittest import mock

from cruxible_core.service.discovery.next import service_playbill_next


def unfolded_next(instance: Any, *, request: Any, **kwargs: Any) -> Any:
    """The queue with every supporting capture still a row of its own.

    Presentation folds `claim_new_evidence_supporting` into the row it would
    resolve, or drops it. The attestation reducer's laws are about which
    captures it reports, so they are pinned before that fold.
    """

    with mock.patch(
        "cruxible_core.service.discovery.next._fold_supporting",
        lambda items: (items, {}),
    ):
        return service_playbill_next(instance, request=request, **kwargs)
