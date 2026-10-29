"""Cleaning, de-duplication and source validation."""
from market_intel.core.models import Claim, Company, Source, SourceType, Verification
from market_intel.processing.cleaning import clean_text, dedupe_companies, normalize_url, remove_overlap
from market_intel.processing.validation import classify_source, verify_claim


def test_placeholders_become_unknown():
    for v in ["N/A", "unknown", " - ", None, "Not specified"]:
        assert clean_text(v) is None
    assert normalize_url("acme.com/") == "https://acme.com"
    assert normalize_url("not a url") is None


def test_duplicates_are_merged_and_gaps_filled():
    a = Company("Siemens Energy AG", "customer", country="Germany")
    b = Company("Siemens Energy", "customer", website="https://siemens-energy.com", industry="Energy")
    c = Company("SE Global", "customer", website="https://www.siemens-energy.com/en")
    kept, log = dedupe_companies([a, b, c])
    assert len(kept) == 1 and len(log) == 2
    assert kept[0].country == "Germany" and kept[0].website and kept[0].industry == "Energy"


def test_competitor_is_not_listed_as_customer():
    customers = [Company("Tesla Inc", "customer"), Company("ACWA Power", "customer")]
    kept, log = remove_overlap(customers, [Company("Tesla", "competitor")])
    assert [c.name for c in kept] == ["ACWA Power"] and log


def _src(sid, url, text, accessible=True):
    return Source(id=sid, url=url, text=text, accessible=accessible)


def test_source_classification():
    official = {"acme.com"}
    assert classify_source(_src("S1", "https://www.acme.com/p", ""), official) == SourceType.OFFICIAL
    assert classify_source(_src("S2", "https://www.reuters.com/x", ""), official) == SourceType.INDEPENDENT
    assert classify_source(_src("S3", "https://energy.gov/x", ""), official) == SourceType.INDEPENDENT
    assert classify_source(_src("S4", "https://random-blog.xyz/x", ""), official) == SourceType.UNKNOWN


def test_claims_with_invented_or_irrelevant_sources_are_unverified():
    s1 = _src("S1", "https://www.reuters.com/a", "ACWA Power signs battery storage project in Saudi Arabia")
    s1.source_type = SourceType.INDEPENDENT
    sources = {"S1": s1}

    ok = verify_claim(Claim("ACWA Power battery storage project Saudi Arabia", ["S1"]), sources)
    assert ok.status == Verification.VERIFIED

    invented = verify_claim(Claim("ACWA Power builds hydrogen plant", ["S99"]), sources)
    assert invented.status == Verification.UNVERIFIED and "non-existent" in invented.note

    unrelated = verify_claim(Claim("Company opens chocolate factory in Peru", ["S1"]), sources)
    assert unrelated.status == Verification.UNVERIFIED


def test_unreachable_source_is_not_strong_evidence():
    s = _src("S1", "https://www.reuters.com/a", "", accessible=False)
    s.snippet, s.source_type = "ACWA Power battery storage project Saudi Arabia", SourceType.INDEPENDENT
    claim = verify_claim(Claim("ACWA Power battery storage project Saudi Arabia", ["S1"]), {"S1": s})
    assert claim.status == Verification.PROBABLE
