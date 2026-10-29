"""Turn collected Company records into ranking criteria values.

Deterministic rules only. Any criterion without evidence is returned as None
(unknown) — never as a guessed positive value.
"""
from __future__ import annotations

import re

from market_intel.core.models import Company, Market, ProductProfile, Verification
from market_intel.processing.validation import tokens
from market_intel.scoring.ranking import CustomerFeatures

# Minimal country → region map for partial geographic credit.
REGIONS: dict[str, set[str]] = {
    "gcc": {"saudi arabia", "united arab emirates", "uae", "qatar", "kuwait", "oman", "bahrain"},
    "middle east": {"saudi arabia", "united arab emirates", "uae", "qatar", "kuwait", "oman", "bahrain",
                    "iraq", "iran", "jordan", "israel", "lebanon", "turkey", "türkiye", "egypt", "yemen", "syria"},
    "europe": {"germany", "france", "united kingdom", "uk", "italy", "spain", "netherlands", "belgium",
               "poland", "sweden", "norway", "denmark", "finland", "austria", "switzerland", "ireland",
               "portugal", "greece", "czech republic", "czechia", "romania", "hungary", "turkey", "türkiye"},
    "north america": {"united states", "usa", "us", "canada", "mexico"},
    "latin america": {"brazil", "chile", "argentina", "colombia", "peru", "mexico", "uruguay"},
    "asia": {"china", "india", "japan", "south korea", "korea", "vietnam", "thailand", "indonesia",
             "malaysia", "philippines", "singapore", "pakistan", "bangladesh", "taiwan", "kazakhstan"},
    "africa": {"south africa", "egypt", "morocco", "nigeria", "kenya", "ethiopia", "algeria", "tunisia"},
    "oceania": {"australia", "new zealand"},
}
REGIONS["mena"] = REGIONS["middle east"] | {"morocco", "algeria", "tunisia", "libya"}
REGIONS["southeast asia"] = {"vietnam", "thailand", "indonesia", "malaysia", "philippines", "singapore"}
REGIONS["eu"] = REGIONS["europe"] - {"united kingdom", "uk", "norway", "switzerland", "turkey", "türkiye"}

# Words too generic to show an industry match ("infrastructure developer" is not "EV charging infrastructure").
_GENERIC_INDUSTRY_TOKENS = {
    "infrastructure", "developer", "development", "service", "services", "solution", "solutions", "system",
    "systems", "project", "projects", "company", "operator", "provider", "management", "commercial",
    "industrial", "general", "large-scale", "facilitie", "facility", "site", "sites", "plant", "plants",
}

_VERIFICATION_VALUE = {Verification.VERIFIED: 1.0, Verification.PROBABLE: 0.5, Verification.UNVERIFIED: 0.0}


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("the ", " ").split())


def regions_of(country: str) -> set[str]:
    c = _norm(country)
    return {r for r, members in REGIONS.items() if c in members}


def _targets(target_region: str, markets: list[Market]) -> set[str]:
    raw = [target_region] + [m.name for m in markets]
    out = set()
    for t in raw:
        for part in t.replace("/", ",").replace(";", ",").split(","):
            if part.strip():
                out.add(_norm(part))
    return out


def geo_fit(country: str | None, target_region: str, markets: list[Market]) -> tuple[float | None, str]:
    if not country:
        return None, "Country unknown – no geographic credit"
    c = _norm(country)
    targets = _targets(target_region, markets)
    if not targets:
        return None, "No target region/markets defined"
    for t in targets:
        if re.search(rf"\b{re.escape(c)}\b", t) or re.search(rf"\b{re.escape(t)}\b", c):
            return 1.0, f"{country} is a target market ({t})"
        if c in REGIONS.get(t, set()):
            return 1.0, f"{country} lies inside target region '{t}'"
    shared = regions_of(country) & {r for t in targets for r in (regions_of(t) | ({t} if t in REGIONS else set()))}
    if shared:
        return 0.5, f"{country} is in the same wider region ({', '.join(sorted(shared))}) as a target market"
    return 0.0, f"{country} is outside the target region/markets"


def industry_fit(industry: str | None, activity: str | None, product: ProductProfile) -> tuple[float | None, str]:
    """Match the customer's industry label (activity only as fallback) against the product's target industries.

    Only the short label is used when present: long activity texts mention many sectors and create false matches.
    """
    if not industry and not activity:
        return None, "Industry unknown – no industry credit"
    targets = [c.text for c in product.industries] + [c.text for c in product.applications]
    if not targets:
        return None, "Product target industries were not extracted"
    cust = tokens(industry or activity or "") - _GENERIC_INDUSTRY_TOKENS
    best, best_t = 0.0, ""
    for t in targets:
        tt = tokens(t) - _GENERIC_INDUSTRY_TOKENS
        if not tt:
            continue
        score = len(cust & tt) / min(len(tt), 4)
        if score > best:
            best, best_t = min(1.0, score), t
    if best == 0:
        return 0.0, f"'{industry or activity}' does not match the product's target industries"
    return round(best, 3), f"Matches product target '{best_t}'"


def product_terms(product: ProductProfile) -> set[str]:
    """Words that tie a piece of evidence to the product (name, buyer keywords, applications)."""
    text = " ".join([product.name, *product.keywords, *(c.text for c in product.applications)])
    return tokens(text) - _GENERIC_INDUSTRY_TOKENS


# Product-agnostic buying triggers: an expansion in a target industry signals need even if the product isn't named.
_TRIGGERS = tokens("expansion expand new plant factory facility investment invest construction build project tender "
                   "award contract procurement purchase upgrade modernisation modernization capacity commission "
                   "install installation retrofit greenfield")


def industry_terms(product: ProductProfile) -> set[str]:
    return tokens(" ".join(c.text for c in product.industries)) - _GENERIC_INDUSTRY_TOKENS


def is_need_signal(text: str, terms: set[str], ind_terms: set[str] = frozenset()) -> bool:
    """Evidence names the product (terms), or a buying trigger together with a target industry."""
    t = tokens(text)
    return bool(t & terms) or bool(t & _TRIGGERS and t & ind_terms)


def need_evidence(company: Company, product: ProductProfile | None = None) -> tuple[float | None, str]:
    """verified = 1, probable = 0.5, unverified = 0 – only for evidence of a need for the product: it names the
    product, or reports a buying trigger (new plant, expansion, contract…) in a target industry.
    "Listed among manufacturing startups" is about the company, but not about a need.
    """
    if not company.evidence:
        return None, "No evidence of need found – no credit"
    terms = product_terms(product) if product else set()
    ind = industry_terms(product) if product else set()
    relevant = [e for e in company.evidence if not terms or is_need_signal(e.text, terms, ind)]
    pts = sum(_VERIFICATION_VALUE[e.status] for e in relevant)
    counts = {s.value: sum(e.status == s for e in relevant) for s in Verification}
    summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "none"
    ignored = len(company.evidence) - len(relevant)
    note = f"; {ignored} item(s) ignored – not about a need for the product" if ignored else ""
    return round(min(1.0, pts / 2.0), 3), f"{len(relevant)} product-related evidence item(s): {summary}{note}"


def source_reliability(company: Company) -> tuple[float, str]:
    reach = {True: "website reachable", False: "website NOT reachable", None: "website not checked"}
    return _VERIFICATION_VALUE[company.status], f"Company record {company.status.value}; {reach[company.website_reachable]}"


def build_features(customers: list[Company], product: ProductProfile, target_region: str,
                   markets: list[Market]) -> list[CustomerFeatures]:
    out = []
    terms, ind_terms = product_terms(product), industry_terms(product)
    for i, c in enumerate(customers):
        ind, ind_r = industry_fit(c.industry, c.activity, product)
        geo, geo_r = geo_fit(c.country, target_region, markets)
        need, need_r = need_evidence(c, product)
        rel, rel_r = source_reliability(c)
        out.append(CustomerFeatures(
            id=f"C{i + 1}", name=c.name,
            industry_fit=ind, geo_fit=geo, need_evidence=need, source_reliability=rel,
            evidence_count=sum(e.status != Verification.UNVERIFIED and is_need_signal(e.text, terms, ind_terms)
                               for e in c.evidence),
            reasons={"industry_fit": ind_r, "geo_fit": geo_r, "need_evidence": need_r, "source_reliability": rel_r},
        ))
    return out
