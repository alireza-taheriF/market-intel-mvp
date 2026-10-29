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
    if not industry and not activity:
        return None, "Industry unknown – no industry credit"
    targets = [c.text for c in product.industries] + [c.text for c in product.applications]
    if not targets:
        return None, "Product target industries were not extracted"
    cust = tokens(f"{industry or ''} {activity or ''}")
    best, best_t = 0.0, ""
    for t in targets:
        tt = tokens(t)
        if not tt:
            continue
        score = len(cust & tt) / min(len(tt), 4)
        if score > best:
            best, best_t = min(1.0, score), t
    if best == 0:
        return 0.0, f"'{industry or activity}' does not match the product's target industries"
    return round(best, 3), f"Matches product target '{best_t}'"


def need_evidence(company: Company) -> tuple[float | None, str]:
    if not company.evidence:
        return None, "No evidence of need found – no credit"
    pts = sum(_VERIFICATION_VALUE[e.status] for e in company.evidence)
    counts = {s.value: sum(e.status == s for e in company.evidence) for s in Verification}
    summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
    return round(min(1.0, pts / 2.0), 3), f"{len(company.evidence)} evidence item(s): {summary}"


def source_reliability(company: Company) -> tuple[float, str]:
    reach = {True: "website reachable", False: "website NOT reachable", None: "website not checked"}
    return _VERIFICATION_VALUE[company.status], f"Company record {company.status.value}; {reach[company.website_reachable]}"


def build_features(customers: list[Company], product: ProductProfile, target_region: str,
                   markets: list[Market]) -> list[CustomerFeatures]:
    out = []
    for i, c in enumerate(customers):
        ind, ind_r = industry_fit(c.industry, c.activity, product)
        geo, geo_r = geo_fit(c.country, target_region, markets)
        need, need_r = need_evidence(c)
        rel, rel_r = source_reliability(c)
        out.append(CustomerFeatures(
            id=f"C{i + 1}", name=c.name,
            industry_fit=ind, geo_fit=geo, need_evidence=need, source_reliability=rel,
            evidence_count=sum(e.status != Verification.UNVERIFIED for e in c.evidence),
            reasons={"industry_fit": ind_r, "geo_fit": geo_r, "need_evidence": need_r, "source_reliability": rel_r},
        ))
    return out
