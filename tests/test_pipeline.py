"""End-to-end pipeline run with fake search/web/LLM (no network, no API key)."""
from market_intel.collectors.fetcher import FetchResult
from market_intel.collectors.search import SearchHit
from market_intel.core.config import Settings
from market_intel.core.models import ProductInput, Verification
from market_intel.pipeline import Pipeline, rank
from market_intel.report import to_markdown
from market_intel.scoring.ranking import DEFAULT_WEIGHTS

PAGE = ("ACWA Power, Saudi Electricity, Masdar, DEWA, NEOM, Aramco and Khazna Data Centers are investing in "
        "battery energy storage projects for renewable integration and peak shaving in Saudi Arabia and the "
        "United Arab Emirates. Tesla, Fluence, Sungrow and BYD supply battery energy storage systems.")


class Search:
    def search(self, query, max_results=None, exclude_domains=None):  # used for website lookup
        return []

    def search_many(self, queries, max_results=None):
        return [SearchHit(f"Article {i}", f"https://www.reuters.com/{abs(hash(q)) % 9999}-{i}", PAGE, q)
                for q in queries for i in range(2)], []


class Fetcher:
    def fetch_many(self, urls, workers=8):
        return {u: FetchResult(u, ok=True, status=200, title="page", text=PAGE) for u in urls}


def _cust(name, country, web, industry, need=True):
    return {"name": name, "country": country, "website": web, "industry": industry,
            "activity": f"{industry} operator", "fit_reason": f"{name} battery energy storage projects",
            "need_evidence": [{"text": f"{name} battery energy storage projects", "sources": ["S1"]}] if need else [],
            "sources": ["S1"]}


class LLM:
    def complete_json(self, prompt, max_tokens=16000):
        if "describe the product" in prompt:
            return {"product_name": "BESS", "summary": "Battery storage.",
                    "features": [{"text": "battery energy storage", "sources": ["S1"]}],
                    "applications": [{"text": "Peak shaving", "sources": ["S1"]},
                                     {"text": "Renewable integration", "sources": ["S1"]}],
                    "industries": [{"text": "Utilities", "sources": ["S1"]}, {"text": "Data centers", "sources": ["S1"]}],
                    "search_keywords": ["battery energy storage"]}
        if "geographic markets" in prompt:
            return {"markets": [{"name": "Saudi Arabia", "rationale": "battery energy storage projects Saudi Arabia",
                                 "sources": ["S1"]},
                                {"name": "United Arab Emirates", "rationale": "battery energy storage United Arab Emirates",
                                 "sources": ["S1"]}]}
        if "(competitors)" in prompt:
            return {"competitors": [{"name": n, "country": c, "website": w, "activity": "BESS supplier",
                                     "reason": f"{n} supply battery energy storage systems", "sources": ["S1"]}
                                    for n, c, w in [("Tesla", "USA", "tesla.com"), ("Fluence", "USA", "fluenceenergy.com"),
                                                    ("Sungrow", "China", "sungrowpower.com")]]}
        if "potential BUYERS" in prompt:
            return {"customers": [
                _cust("ACWA Power", "Saudi Arabia", "acwapower.com", "Utilities"),
                _cust("Masdar", "United Arab Emirates", "masdar.ae", "Renewable energy"),
                _cust("Khazna Data Centers", "United Arab Emirates", "khazna.ae", "Data centers"),
                _cust("NEOM", "Saudi Arabia", None, "Utilities", need=False),
                _cust("Aramco", "N/A", "aramco.com", "Oil and gas"),
                _cust("ACWA Power Company", "Saudi Arabia", "https://www.acwapower.com", "Utilities"),  # duplicate
                _cust("Tesla Inc", "USA", "tesla.com", "Utilities"),                                   # competitor
            ]}
        if "cold email" in prompt:
            return {"subject": "Storage for your projects", "body": "Dear team, " + "word " * 120, "facts_used": ["x"]}
        raise AssertionError("unexpected prompt")


def test_full_run_produces_complete_ranked_validated_result(tmp_path):
    r = Pipeline(Settings(runs_dir=str(tmp_path)), search=Search(), fetcher=Fetcher(), llm=LLM()).run(
        ProductInput(name="Industrial BESS", target_region="GCC"))

    assert r.status == "complete", (r.errors, r.warnings, [(s.name, s.status, s.message) for s in r.steps])
    assert len(r.markets) >= 2 and len(r.competitors) >= 3 and len(r.customers) >= 5
    names = [c.name for c in r.customers]
    assert "ACWA Power Company" not in names and "Tesla Inc" not in names   # deduped / competitor removed
    assert any("merged" in d for d in r.duplicates_removed)

    aramco = next(c for c in r.customers if c.name == "Aramco")
    assert aramco.country is None and "country" in aramco.missing_fields   # "N/A" became unknown

    ranking = rank(r, DEFAULT_WEIGHTS)  # the weights the run used
    assert [s.rank for s in ranking] == list(range(1, len(ranking) + 1))
    neom = next(s for s in ranking if s.name == "NEOM")
    assert "need_evidence" in neom.unknown_criteria
    assert len(r.messages) == 2 and all(m.generated_by == "ai" for m in r.messages)
    assert [m.company_name for m in r.messages] == [s.name for s in ranking[:2]]
    assert r.product.features[0].status == Verification.VERIFIED
    assert "Sources & validation" in to_markdown(r, ranking)


def test_round_robin_gives_every_query_a_place_before_the_cap():
    from market_intel.pipeline import _round_robin
    hits = [SearchHit("t", f"https://a/{q}{i}", "", q) for q in ("q1", "q2") for i in range(3)]
    assert [h.query for h in _round_robin(hits)][:2] == ["q1", "q2"]
