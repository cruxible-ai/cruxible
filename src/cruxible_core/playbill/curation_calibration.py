"""Reviewed calibration parameters for curation detectors and audit ranking.

These values are operational policy knobs rather than protocol versions.  Keep
them centralized so a tuning change is explicit, reviewable, and backed by
episode evidence instead of being hidden inside a detector fold.
"""

from fractions import Fraction

# Raise only if episode data shows a single unresolved slot is routine noise.
RECURRING_CONFLICT_MINIMUM_UNRESOLVED_SLOTS = 1

# Raise only if durable refused-attempt pairs are too often isolated mistakes.
ADMISSION_FAILURE_MINIMUM_DISTINCT_DURABLE_ATTEMPTS = 2

# Raise only if three source changes produce unstable freshness medians in practice.
FRESHNESS_MINIMUM_CHANGED_COMMITMENT_INTERVALS = 3
# Tighten or widen only from observed stale-horizon/source-change distributions.
FRESHNESS_RATIO_LOWER = Fraction(1, 2)
FRESHNESS_RATIO_UPPER = Fraction(2, 1)

# Raise only if two live supported claims create too many low-value concentration rows.
PROVENANCE_MINIMUM_LIVE_SUPPORTED_CLAIMS = 2
# Change only if the control-component model gains a reviewed concentration policy.
PROVENANCE_CONCENTRATED_CONTROL_COMPONENT_COUNT = 1
# Change only if writer-population episodes show a different tautology boundary.
PROVENANCE_MINIMUM_ACTIVE_WRITING_PRINCIPALS = 2

# Raise only if pairs of simultaneously live identical statements are expected behavior.
DUPLICATE_STATEMENT_MINIMUM_LIVE_CLAIM_IDENTITIES = 2

# Raise only if three-subject qualifier reuse proves too noisy in episode data.
QUALIFIER_MINIMUM_DISTINCT_SUBJECT_ADDRESSES = 3

# Raise only if three distinct observed bodies over-report ordinary editing churn.
BLOCK_CHURN_MINIMUM_DISTINCT_BODY_DIGESTS = 3
# Raise only if two accepted generations cannot distinguish churn from one edit episode.
BLOCK_CHURN_MINIMUM_OBSERVED_GENERATIONS = 2
# Change only from measured editing cadence across accepted-generation histories.
BLOCK_CHURN_ACCEPTED_GENERATION_WINDOW = 10

# Change only from observed vocabulary adoption latency after the consumption epoch.
DEAD_VOCABULARY_MINIMUM_ZERO_TOUCH_GENERATIONS = 10

# Change budget defaults only from patrol payload-size and reviewer-throughput evidence.
AUDIT_BUDGET_DEFAULT_MAX_ROWS = 100
AUDIT_BUDGET_MIN_MAX_ROWS = 1
AUDIT_BUDGET_MAX_MAX_ROWS = 1_000
AUDIT_BUDGET_DEFAULT_MAX_BYTES = 65_536
AUDIT_BUDGET_MIN_MAX_BYTES = 1_024
AUDIT_BUDGET_MAX_MAX_BYTES = 4_194_304

# Change stake weights only if dependency or demand counts mis-rank reviewed patrols.
AUDIT_STAKE_BASE = 1
AUDIT_DEPENDENT_STAKE_WEIGHT = 1
AUDIT_CONSUMPTION_STAKE_WEIGHT = 1
# Change weakness weights only after comparing each mechanical signal with review yield.
AUDIT_WEAKNESS_BASE = 1
AUDIT_WEAKNESS_SIGNAL_WEIGHT = 1
# Change only if one effective control component ceases to mean zero corroboration.
AUDIT_CORROBORATED_CONTROL_COMPONENT_MINIMUM = 2
# Change only from measured capture-expiry lead time needed by reviewers.
AUDIT_NEAR_FRESHNESS_HORIZON_DIVISOR = 4
# Change staleness weight only if generation age over- or under-ranks verified claims.
AUDIT_STALENESS_BASE = 1
# Change rank weights only from replayed patrol-yield comparisons.
AUDIT_RANK_STAKE_WEIGHT = 1
AUDIT_RANK_WEAKNESS_WEIGHT = 1
AUDIT_RANK_STALENESS_WEIGHT = 1


__all__ = [name for name in globals() if name.isupper()]
