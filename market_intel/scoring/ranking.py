"""Customer ranking algorithm (pure Python, no LLM, fully unit-testable).

final_score = 100 * Σ_i (w_i · v_i) / Σ_i w_i

* v_i ∈ [0, 1] is the value of criterion i, or None when it is UNKNOWN.
* An UNKNOWN criterion contributes exactly 0 – missing data never earns
  points. It is reported separately via `coverage` so a low score caused by
  missing data can be told apart from a low score caused by a poor fit.
* Weights are user-adjustable, must be ≥ 0, and are normalised to sum to 1.
* Ordering is deterministic: score ↓, coverage ↓, evidence count ↓,
  name (case-insensitive) ↑, id ↑ – ties never depend on input order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CRITERIA = ("industry_fit", "geo_fit", "need_evidence", "source_reliability")

CRITERION_LABELS = {
    "industry_fit": "Industry fit",
    "geo_fit": "Geographic fit",
    "need_evidence": "Evidence of need",
    "source_reliability": "Data reliability",
}

DEFAULT_WEIGHTS: dict[str, float] = {
    "industry_fit": 0.35,
    "geo_fit": 0.25,
    "need_evidence": 0.25,
    "source_reliability": 0.15,
}


@dataclass(frozen=True)
class CustomerFeatures:
    """Criterion values for one customer. None = unknown (no evidence)."""
    id: str
    name: str
    industry_fit: float | None = None
    geo_fit: float | None = None
    need_evidence: float | None = None
    source_reliability: float | None = None
    evidence_count: int = 0
    reasons: dict[str, str] = field(default_factory=dict)

    def value(self, criterion: str) -> float | None:
        return getattr(self, criterion)


@dataclass(frozen=True)
class CriterionScore:
    criterion: str
    value: float | None        # raw 0..1, None = unknown
    weight: float              # normalised weight
    points: float              # contribution to the 0..100 final score
    reason: str


@dataclass(frozen=True)
class ScoredCustomer:
    features: CustomerFeatures
    score: float               # 0..100
    coverage: float            # share of weight backed by known data, 0..1
    breakdown: tuple[CriterionScore, ...]
    rank: int = 0

    @property
    def name(self) -> str:
        return self.features.name

    @property
    def unknown_criteria(self) -> list[str]:
        return [c.criterion for c in self.breakdown if c.value is None]

    def explain(self) -> str:
        """Human-readable audit trail of how the final score was computed."""
        parts = []
        for c in self.breakdown:
            v = "unknown→0" if c.value is None else f"{c.value:.2f}"
            parts.append(f"{c.weight:.3f}×{v}")
        return f"100 × ({' + '.join(parts)}) = {self.score:.2f}"


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    unknown = set(weights) - set(CRITERIA)
    if unknown:
        raise ValueError(f"Unknown criteria in weights: {sorted(unknown)}")
    w = {c: float(weights.get(c, 0.0)) for c in CRITERIA}
    if any(v < 0 for v in w.values()):
        raise ValueError("Weights must be non-negative")
    total = sum(w.values())
    if total <= 0:
        raise ValueError("At least one weight must be positive")
    return {c: v / total for c, v in w.items()}


def _clamp(v: float | None) -> float | None:
    if v is None:
        return None
    return min(1.0, max(0.0, float(v)))


def score_customer(features: CustomerFeatures, weights: dict[str, float] | None = None) -> ScoredCustomer:
    w = normalize_weights(weights or DEFAULT_WEIGHTS)
    breakdown = []
    coverage = total = 0.0
    for c in CRITERIA:
        v = _clamp(features.value(c))
        points = 0.0 if v is None else 100.0 * w[c] * v
        if v is not None:
            coverage += w[c]
        total += points
        reason = features.reasons.get(c) or ("Unknown – no evidence found, contributes 0" if v is None else "")
        breakdown.append(CriterionScore(c, v, round(w[c], 4), round(points, 2), reason))
    # Sum unrounded points: summing rounded ones would break exact ties (e.g. 49.99 vs 50.00).
    score = round(total, 2)
    return ScoredCustomer(features, score=min(100.0, max(0.0, score)), coverage=round(coverage, 4),
                          breakdown=tuple(breakdown))


def sort_key(s: ScoredCustomer) -> tuple:
    return (-s.score, -s.coverage, -s.features.evidence_count, s.features.name.casefold(), s.features.id)


def rank_customers(features: list[CustomerFeatures], weights: dict[str, float] | None = None) -> list[ScoredCustomer]:
    scored = sorted((score_customer(f, weights) for f in features), key=sort_key)
    return [
        ScoredCustomer(s.features, s.score, s.coverage, s.breakdown, rank=i)
        for i, s in enumerate(scored, start=1)
    ]
