"""Compatibility re-exports; Playbill owns the canonical service module."""

from cruxible_core.playbill.service.source_catalog import (
    PlaybillSourceCheckResult as PlaybillSourceCheckResult,
)
from cruxible_core.playbill.service.source_catalog import (
    PlaybillSourceContext as PlaybillSourceContext,
)
from cruxible_core.playbill.service.source_catalog import (
    service_check_playbill_source_bundle as service_check_playbill_source_bundle,
)
from cruxible_core.playbill.service.source_catalog import (
    service_check_playbill_sources as service_check_playbill_sources,
)
from cruxible_core.playbill.service.source_catalog import (
    service_compile_playbill_sources as service_compile_playbill_sources,
)
from cruxible_core.playbill.service.source_catalog import (
    service_playbill_source_context as service_playbill_source_context,
)
from cruxible_core.playbill.service.source_catalog import (
    service_propose_playbill_source_bundle as service_propose_playbill_source_bundle,
)
