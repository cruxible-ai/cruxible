"""Compatibility re-exports; Playbill owns the canonical service module."""

from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate as PlaybillAcceptedCoordinate,
)
from cruxible_core.playbill.service.documents import (
    PlaybillActivationReceipt as PlaybillActivationReceipt,
)
from cruxible_core.playbill.service.documents import (
    PlaybillApprovalReceipt as PlaybillApprovalReceipt,
)
from cruxible_core.playbill.service.documents import PlaybillBodyRead as PlaybillBodyRead
from cruxible_core.playbill.service.documents import (
    PlaybillDocumentHistory as PlaybillDocumentHistory,
)
from cruxible_core.playbill.service.documents import (
    PlaybillDocumentHistoryEntry as PlaybillDocumentHistoryEntry,
)
from cruxible_core.playbill.service.documents import (
    PlaybillDocumentList as PlaybillDocumentList,
)
from cruxible_core.playbill.service.documents import (
    PlaybillDocumentView as PlaybillDocumentView,
)
from cruxible_core.playbill.service.documents import (
    PlaybillPrincipalList as PlaybillPrincipalList,
)
from cruxible_core.playbill.service.documents import (
    PlaybillProposalInspection as PlaybillProposalInspection,
)
from cruxible_core.playbill.service.documents import (
    PlaybillRefusalInspection as PlaybillRefusalInspection,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal as service_activate_playbill_proposal,
)
from cruxible_core.playbill.service.documents import (
    service_dereference_playbill_document as service_dereference_playbill_document,
)
from cruxible_core.playbill.service.documents import (
    service_get_playbill_document as service_get_playbill_document,
)
from cruxible_core.playbill.service.documents import (
    service_inspect_playbill_proposal as service_inspect_playbill_proposal,
)
from cruxible_core.playbill.service.documents import (
    service_inspect_playbill_refusal as service_inspect_playbill_refusal,
)
from cruxible_core.playbill.service.documents import (
    service_list_playbill_documents as service_list_playbill_documents,
)
from cruxible_core.playbill.service.documents import (
    service_list_playbill_principals as service_list_playbill_principals,
)
from cruxible_core.playbill.service.documents import (
    service_playbill_document_history as service_playbill_document_history,
)
from cruxible_core.playbill.service.documents import (
    service_propose_playbill_document as service_propose_playbill_document,
)
from cruxible_core.playbill.service.documents import (
    service_propose_playbill_principal_change as service_propose_playbill_principal_change,
)
from cruxible_core.playbill.service.documents import (
    service_store_playbill_body as service_store_playbill_body,
)
from cruxible_core.playbill.service.documents import (
    service_submit_playbill_approval as service_submit_playbill_approval,
)
