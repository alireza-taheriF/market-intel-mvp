"""Source credibility & claim verification (deterministic, no LLM).

For every important claim we check:
  1. the cited source ids exist (the LLM cannot invent a source),
  2. the source was accessible when collected,
  3. the source text is actually related to the claim (lexical relevance),
  4. the source type: official / independent / unknown credibility.
"""
from __future__ import annotations

import re

from market_intel.core.models import Claim, Company, Source, SourceType, Verification
from market_intel.processing.cleaning import domain_of, normalize_company_name

# Recognised independent publishers (news, industry media, research, institutions).
INDEPENDENT_DOMAINS = {
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "bbc.com", "bbc.co.uk",
    "theguardian.com", "forbes.com", "economist.com", "apnews.com", "nikkei.com", "spglobal.com",
    "energy-storage.news", "pv-magazine.com", "pv-tech.org", "renewablesnow.com", "rechargenews.com",
    "electrek.co", "utilitydive.com", "greentechmedia.com", "energymonitor.ai", "power-technology.com",
    "renewableenergyworld.com", "woodmac.com", "bnef.com", "iea.org", "irena.org", "nrel.gov",
    "energy.gov", "eia.gov", "europa.eu", "worldbank.org", "ifc.org", "wikipedia.org",
    "mckinsey.com", "deloitte.com", "pwc.com", "ey.com", "kpmg.com", "lazard.com",
    "marketsandmarkets.com", "grandviewresearch.com", "fortunebusinessinsights.com",
    "mordorintelligence.com", "precedenceresearch.com", "statista.com", "zawya.com",
    "thenationalnews.com", "arabnews.com", "gulfnews.com", "businesswire.com", "prnewswire.com",
    "globenewswire.com", "ess-news.com", "energy-storage.news", "pv-magazine-india.com",
    # research, academic and consulting publishers
    "sciencedirect.com", "springer.com", "mdpi.com", "ieee.org", "nature.com", "wiley.com", "researchgate.net",
    "rolandberger.com", "bcg.com", "bain.com", "accenture.com", "benchmarkminerals.com", "rystadenergy.com",
    "gminsights.com", "insightaceanalytic.com", "imarcgroup.com", "6wresearch.com", "globaldata.com",
    # energy and regional business news
    "enerdata.net", "energyglobal.com", "energy-utilities.com", "energydigital.com", "power-eng.com",
    "renewableenergymagazine.com", "solarquarter.com", "saurenergy.com", "constructionweekonline.com",
    "meed.com", "argaam.com", "saudigazette.com.sa", "khaleejtimes.com", "agbi.com", "wam.ae", "spa.gov.sa",
    "aljazeera.com", "aa.com.tr", "nytimes.com", "washingtonpost.com", "axios.com", "techcrunch.com",
}
# Social networks and company-data aggregators: never credible on their own.
UNRELIABLE_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "reddit.com",
    "quora.com", "medium.com", "rocketreach.co", "tracxn.com", "leadiq.com", "pitchbook.com", "zoominfo.com",
}
_INDEPENDENT_SUFFIXES = (".gov", ".edu", ".int", ".ac.uk", ".gov.uk", ".gov.au", ".gov.sa", ".gov.ae", ".europa.eu")

_STOPWORDS = set("""a an and are as at be by for from has have in into is it its of on or that the
their this to was were which with will can may such these those other than also more most using use
used based provide provides providing including include includes company companies""".split())

RELEVANT = 0.5
WEAK = 0.3


def _stem(w: str) -> str:
    """Tiny plural stemmer: utilities→utility, centers→center, gas stays gas."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-]+", text.lower())
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 2}


def classify_source(source: Source, official_domains: set[str]) -> SourceType:
    d = source.domain
    if not d:
        return SourceType.UNKNOWN
    if any(d == u or d.endswith("." + u) for u in UNRELIABLE_DOMAINS):
        return SourceType.UNKNOWN
    if any(d == od or d.endswith("." + od) for od in official_domains):
        return SourceType.OFFICIAL
    if any(d == idom or d.endswith("." + idom) for idom in INDEPENDENT_DOMAINS) or d.endswith(_INDEPENDENT_SUFFIXES):
        return SourceType.INDEPENDENT
    return SourceType.UNKNOWN


def relevance(claim_text: str, source_text: str, entity: str | None = None) -> float:
    """Share of the claim's content words found in the source (0..1).

    When the claim is about a named entity, the entity name must appear in the
    source, otherwise relevance is capped at 0.2 (the page is about something else).
    """
    if not source_text:
        return 0.0
    claim_tokens = tokens(claim_text)
    src_tokens = tokens(source_text)
    overlap = len(claim_tokens & src_tokens) / len(claim_tokens) if claim_tokens else 0.0
    if entity:
        name = normalize_company_name(entity)
        if name and name not in " ".join(source_text.lower().split()):
            return round(min(overlap, 0.2), 3)
    return round(overlap, 3)


def verify_claim(claim: Claim, sources: dict[str, Source], entity: str | None = None) -> Claim:
    valid_ids = [sid for sid in claim.source_ids if sid in sources]
    invented = [sid for sid in claim.source_ids if sid not in sources]
    notes = []
    if invented:
        notes.append(f"ignored non-existent source ids {invented}")
    if not valid_ids:
        claim.status, claim.relevance = Verification.UNVERIFIED, 0.0
        claim.note = "; ".join(notes + ["no valid source cited"])
        return claim

    best = 0.0
    strong_credible = 0
    supporting = 0
    for sid in valid_ids:
        src = sources[sid]
        if src.accessible is False and not src.snippet:
            continue
        score = relevance(claim.text, src.evidence_text, entity)
        best = max(best, score)
        if score >= WEAK:
            supporting += 1
        if score >= RELEVANT and src.accessible and src.source_type in (SourceType.OFFICIAL, SourceType.INDEPENDENT):
            strong_credible += 1

    if strong_credible >= 1 or supporting >= 2:
        claim.status = Verification.VERIFIED
    elif supporting == 1:
        claim.status = Verification.PROBABLE
        notes.append("single source of limited credibility/relevance")
    else:
        claim.status = Verification.UNVERIFIED
        notes.append("cited sources are unreachable or do not mention the claim")
    claim.source_ids = valid_ids
    claim.relevance = best
    claim.note = "; ".join(notes)
    return claim


def verify_company(company: Company, sources: dict[str, Source]) -> Company:
    """Company is VERIFIED if its website is reachable AND it is mentioned in a source."""
    if company.reason:
        verify_claim(company.reason, sources, company.name)
    for ev in company.evidence:
        verify_claim(ev, sources, company.name)

    name = normalize_company_name(company.name)
    mentioned = any(
        name and name in " ".join(sources[s].evidence_text.lower().split())
        for s in company.source_ids if s in sources
    )
    signals = int(bool(company.website_reachable)) + int(mentioned)
    company.status = {2: Verification.VERIFIED, 1: Verification.PROBABLE}.get(signals, Verification.UNVERIFIED)
    return company


def official_domains_for(product_url: str | None, companies: list[Company]) -> set[str]:
    domains = {d for c in companies if (d := domain_of(c.website))}
    if d := domain_of(product_url):
        domains.add(d)
    return domains
