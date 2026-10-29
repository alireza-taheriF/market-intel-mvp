"""Tests for the deterministic customer-ranking algorithm (no LLM, no network)."""
import random

import pytest

from market_intel.core.models import Claim, Company, Market, ProductProfile, Verification
from market_intel.scoring.features import build_features, geo_fit, industry_fit, need_evidence
from market_intel.scoring.ranking import (
    CRITERIA,
    DEFAULT_WEIGHTS,
    CustomerFeatures,
    normalize_weights,
    rank_customers,
    score_customer,
)

FULL = dict(industry_fit=1.0, geo_fit=1.0, need_evidence=1.0, source_reliability=1.0)


# --- 1. weight changes ---------------------------------------------------------------

def test_weight_change_reorders_according_to_logic():
    industry_strong = CustomerFeatures("C1", "IndustryCo", industry_fit=1.0, geo_fit=0.0,
                                       need_evidence=0.5, source_reliability=1.0)
    geo_strong = CustomerFeatures("C2", "GeoCo", industry_fit=0.0, geo_fit=1.0,
                                  need_evidence=0.5, source_reliability=1.0)

    industry_heavy = {"industry_fit": 0.7, "geo_fit": 0.1, "need_evidence": 0.1, "source_reliability": 0.1}
    geo_heavy = {"industry_fit": 0.1, "geo_fit": 0.7, "need_evidence": 0.1, "source_reliability": 0.1}

    assert [s.name for s in rank_customers([industry_strong, geo_strong], industry_heavy)] == ["IndustryCo", "GeoCo"]
    assert [s.name for s in rank_customers([industry_strong, geo_strong], geo_heavy)] == ["GeoCo", "IndustryCo"]


def test_raising_a_weight_raises_score_of_customer_strong_in_it():
    f = CustomerFeatures("C1", "A", industry_fit=1.0, geo_fit=0.2, need_evidence=0.2, source_reliability=0.2)
    low = score_customer(f, {**DEFAULT_WEIGHTS, "industry_fit": 0.1}).score
    high = score_customer(f, {**DEFAULT_WEIGHTS, "industry_fit": 0.9}).score
    assert high > low


def test_zero_weight_removes_criterion_influence():
    a = CustomerFeatures("C1", "A", industry_fit=1.0, geo_fit=0.5, need_evidence=0.5, source_reliability=0.5)
    b = CustomerFeatures("C2", "B", industry_fit=0.0, geo_fit=0.5, need_evidence=0.5, source_reliability=0.5)
    w = {**DEFAULT_WEIGHTS, "industry_fit": 0.0}
    assert score_customer(a, w).score == score_customer(b, w).score


def test_weights_are_normalised_and_validated():
    assert sum(normalize_weights({"industry_fit": 2, "geo_fit": 2}).values()) == pytest.approx(1.0)
    # Scaling all weights by a constant must not change the score.
    f = CustomerFeatures("C1", "A", **{**FULL, "geo_fit": 0.3})
    assert score_customer(f, {c: 1 for c in CRITERIA}).score == score_customer(f, {c: 5 for c in CRITERIA}).score
    with pytest.raises(ValueError):
        normalize_weights({"industry_fit": -1, "geo_fit": 1})
    with pytest.raises(ValueError):
        normalize_weights({c: 0 for c in CRITERIA})
    with pytest.raises(ValueError):
        normalize_weights({"made_up": 1})


# --- 2. incomplete data --------------------------------------------------------------

def test_unknown_criterion_contributes_zero_points():
    f = CustomerFeatures("C1", "A", industry_fit=1.0, geo_fit=None, need_evidence=None, source_reliability=1.0)
    s = score_customer(f)
    by = {b.criterion: b for b in s.breakdown}
    assert by["geo_fit"].points == 0 and by["need_evidence"].points == 0
    assert by["geo_fit"].value is None
    assert s.score == pytest.approx(100 * (DEFAULT_WEIGHTS["industry_fit"] + DEFAULT_WEIGHTS["source_reliability"]))
    assert s.coverage == pytest.approx(DEFAULT_WEIGHTS["industry_fit"] + DEFAULT_WEIGHTS["source_reliability"])
    assert set(s.unknown_criteria) == {"geo_fit", "need_evidence"}


def test_unknown_never_scores_above_known_zero_or_positive():
    unknown = CustomerFeatures("C1", "Unknown", industry_fit=None, geo_fit=None, need_evidence=None,
                               source_reliability=None)
    s = score_customer(unknown)
    assert s.score == 0 and s.coverage == 0
    assert all(b.points == 0 for b in s.breakdown)


def test_all_unknown_customer_ranks_below_any_evidence():
    unknown = CustomerFeatures("C1", "AAA Unknown")
    weak = CustomerFeatures("C2", "ZZZ Weak", industry_fit=0.1)
    assert [s.name for s in rank_customers([unknown, weak])] == ["ZZZ Weak", "AAA Unknown"]


def test_feature_extraction_does_not_invent_evidence():
    product = ProductProfile(name="BESS", industries=[Claim("Data centers")], applications=[Claim("Peak shaving")])
    c = Company(name="Mystery Ltd", role="customer")  # no country, industry, activity, evidence
    assert industry_fit(c.industry, c.activity, product)[0] is None
    assert geo_fit(c.country, "Saudi Arabia", [])[0] is None
    assert need_evidence(c)[0] is None
    [feat] = build_features([c], product, "Saudi Arabia", [])
    s = score_customer(feat)
    # Only "data reliability" is known, and an unverified record is worth 0.
    assert s.score == 0


def test_unverified_evidence_gives_no_credit():
    c = Company(name="X", role="customer",
                evidence=[Claim("claims a big project", ["S1"], status=Verification.UNVERIFIED)])
    value, _ = need_evidence(c)
    assert value == 0.0


# --- 3. ties -------------------------------------------------------------------------

def test_equal_scores_have_stable_reproducible_order():
    same = dict(industry_fit=0.5, geo_fit=0.5, need_evidence=0.5, source_reliability=0.5)
    customers = [CustomerFeatures(f"C{i}", name, **same) for i, name in enumerate(["delta", "Alpha", "charlie", "Bravo"])]
    expected = ["Alpha", "Bravo", "charlie", "delta"]  # case-insensitive name as final tie-breaker
    for seed in range(10):
        shuffled = customers[:]
        random.Random(seed).shuffle(shuffled)
        assert [s.name for s in rank_customers(shuffled)] == expected


def test_tie_breaks_on_coverage_then_evidence_before_name():
    # Same score (50), but B's score is backed by more known data.
    a = CustomerFeatures("C1", "A", industry_fit=1.0, geo_fit=None, need_evidence=None, source_reliability=None)
    b = CustomerFeatures("C2", "B", industry_fit=0.5, geo_fit=0.5, need_evidence=0.5, source_reliability=0.5)
    w_a = {"industry_fit": 0.5, "geo_fit": 0.5 / 3, "need_evidence": 0.5 / 3, "source_reliability": 0.5 / 3}
    ranked = rank_customers([a, b], w_a)
    assert ranked[0].score == ranked[1].score
    assert [s.name for s in ranked] == ["B", "A"]

    c = CustomerFeatures("C3", "C", **FULL, evidence_count=1)
    d = CustomerFeatures("C4", "D", **FULL, evidence_count=3)
    assert [s.name for s in rank_customers([c, d])] == ["D", "C"]


# --- explainability ------------------------------------------------------------------

def test_breakdown_sums_to_final_score_and_is_explained():
    f = CustomerFeatures("C1", "A", industry_fit=0.8, geo_fit=1.0, need_evidence=None, source_reliability=0.5,
                         reasons={"industry_fit": "matches data centers"})
    s = score_customer(f)
    assert sum(b.points for b in s.breakdown) == pytest.approx(s.score, abs=0.02)
    assert 0 <= s.score <= 100
    assert "unknown→0" in s.explain()
    assert {b.criterion: b.reason for b in s.breakdown}["industry_fit"] == "matches data centers"


def test_ranks_are_consecutive_from_one():
    fs = [CustomerFeatures(f"C{i}", f"N{i}", industry_fit=i / 10) for i in range(5)]
    assert [s.rank for s in rank_customers(fs)] == [1, 2, 3, 4, 5]


def test_geo_fit_rules():
    markets = [Market("Saudi Arabia", Claim("x"))]
    assert geo_fit("Saudi Arabia", "", markets)[0] == 1.0
    assert geo_fit("United Arab Emirates", "GCC", [])[0] == 1.0      # inside target region
    assert geo_fit("Qatar", "", markets)[0] == 0.5                   # same wider region
    assert geo_fit("Brazil", "GCC", markets)[0] == 0.0
