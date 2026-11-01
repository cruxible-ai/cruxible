"""Bounded, per-node Claims reasons and the sole diagnostic allowed into the inbox."""

from dataclasses import dataclass, field

from cruxible_client.contracts.claim_evidence import ClaimsDiagnostic, ExtractRefusedInboxItem
from cruxible_client.contracts.line_inbox import INBOX_RUN_ITEM_CAP_MAX

CLAIMS_REASON_LIMIT = 100


@dataclass
class ClaimsDiagnostics:
    count: int = 0
    reasons: list[ClaimsDiagnostic] = field(default_factory=list)
    inbox_selected: int = 0
    inbox: list[ExtractRefusedInboxItem] = field(default_factory=list)

    def add(self, diagnostic: ClaimsDiagnostic) -> None:
        self.count += 1
        if len(self.reasons) < CLAIMS_REASON_LIMIT:
            self.reasons.append(diagnostic)
        # §5.4 reports incomplete_source; only §7b extract_refused is posted.
        if diagnostic.kind == "extract_refused":
            self.inbox_selected += 1
            if len(self.inbox) < INBOX_RUN_ITEM_CAP_MAX:
                self.inbox.append(
                    ExtractRefusedInboxItem(
                        target=diagnostic.target,
                        field=diagnostic.field or "value",
                        problem=diagnostic.problem or "value_invalid",
                        lexeme=diagnostic.lexeme,
                    )
                )

    def report(self) -> dict[str, object]:
        return {
            "claims_diagnostics": [d.model_dump(mode="json") for d in self.reasons],
            "claims_diagnostics_count": self.count,
            "claims_diagnostics_truncated": self.count - len(self.reasons),
        }
