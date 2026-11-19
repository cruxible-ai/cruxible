"""Bounded per-node findings; only each Line decides what reaches its inbox."""

from dataclasses import dataclass, field

from cruxible_client.contracts.claim_evidence import ClaimsDiagnostic, FindingKind
from cruxible_client.contracts.line_inbox import INBOX_RUN_ITEM_CAP_MAX

CLAIMS_REASON_LIMIT = 100


@dataclass
class ClaimsDiagnostics:
    count: int = 0
    reasons: list[ClaimsDiagnostic] = field(default_factory=list)
    findings: list[ClaimsDiagnostic] = field(default_factory=list)
    counts: dict[FindingKind, int] = field(default_factory=dict)
    same: int = 0

    def add(self, diagnostic: ClaimsDiagnostic) -> None:
        self.count += 1
        self.counts[diagnostic.kind] = self.counts.get(diagnostic.kind, 0) + 1
        if len(self.reasons) < CLAIMS_REASON_LIMIT:
            self.reasons.append(diagnostic)
        if self.counts[diagnostic.kind] <= INBOX_RUN_ITEM_CAP_MAX:
            self.findings.append(diagnostic)

    def report(self) -> dict[str, object]:
        return {
            "claims_diagnostics": [d.model_dump(mode="json") for d in self.reasons],
            "claims_diagnostics_count": self.count,
            "claims_diagnostics_truncated": self.count - len(self.reasons),
            "claims_same_count": self.same,
        }
