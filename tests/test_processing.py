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


def test_official_website_is_picked_only_on_domain_name_match():
    from market_intel.processing.cleaning import pick_official_website
    urls = ["https://www.linkedin.com/company/acwa-power", "https://www.zawya.com/acwa", "https://www.acwapower.com/en/"]
    assert pick_official_website("ACWA Power", urls) == "https://www.acwapower.com"
    assert pick_official_website("BYD", ["https://www.bydglobal.com/x"]) == "https://www.bydglobal.com"
    assert pick_official_website("Khazna Data Centers", ["https://khazna.ae/about"]) == "https://khazna.ae"
    # no match -> unknown, never a guess
    assert pick_official_website("Saudi Power Procurement Company", ["https://www.spa.gov.sa/n1"]) is None
    assert pick_official_website("Saudi Energy", ["https://www.saudienergy.example"]) is None  # only generic words


def test_ambiguous_initials_never_produce_a_website():
    from market_intel.processing.cleaning import pick_official_website
    assert pick_official_website("Saudi Energy", ["https://www.se.com.sa/en"]) is None
    assert pick_official_website("Alfanar Projects", ["https://alfanarprojects.com/en"]) == "https://alfanarprojects.com"
    # sub-site → company homepage
    assert pick_official_website("Kuehne Nagel", ["https://jobs.kuehne-nagel.com/x"]) == "https://www.kuehne-nagel.com"


def test_plural_stemming_matches_singular():
    from market_intel.processing.validation import tokens
    assert tokens("Utilities") == tokens("Utility") and tokens("Data centers") == tokens("data center")


def test_product_name_from_link_only_input():
    from market_intel.processing.cleaning import product_name_from_page
    url = "https://www.siemens.com/global/en/products/automation/plc/s7-1500.html"
    assert product_name_from_page("SIMATIC S7-1500 PLC – High-Performance Controller | Siemens", url) == "SIMATIC S7-1500 PLC"
    assert product_name_from_page("", "https://x.com/products/cr-vertical-multistage-pumps/") == "cr vertical multistage pumps"
    assert product_name_from_page("Home | Acme", "https://acme.com/") is None


def test_websites_that_only_contain_the_name_are_rejected():
    from market_intel.processing.cleaning import pick_official_website as pick
    assert pick("USI", ["https://www.usi.edu"]) is None                                    # a university
    assert pick("Ashland", ["https://www.ashlandmass.com"]) is None                        # a town
    assert pick("PETRONAS", ["https://www.pli-petronas.com"]) is None                      # another company
    assert pick("Emirates Global Aluminium", ["https://aluminium-stewardship.org"]) is None  # an association
    assert pick("VinFast", ["https://vinfastauto.com/vn"]) == "https://vinfastauto.com"
    assert pick("ACWA Power", ["https://www.acwapower.com"]) == "https://www.acwapower.com"
    assert pick("Al Sharif Contracting", ["https://www.alsharifgroup.com"]) == "https://www.alsharifgroup.com"
    assert pick("Trung Nguyen Legend", ["https://trungnguyenlegend.us"]) == "https://trungnguyenlegend.us"
