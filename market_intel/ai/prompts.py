"""Prompt templates. Product-agnostic: everything product-specific comes from input."""
from __future__ import annotations

from market_intel.core.models import ProductInput, Source


def format_sources(sources: list[Source], chars: int = 1800) -> str:
    blocks = []
    for s in sources:
        body = (s.text or s.snippet or "")[:chars]
        status = "accessible" if s.accessible else f"NOT accessible ({s.error or 'unknown'}) – snippet only"
        blocks.append(f"[{s.id}] {s.title}\nURL: {s.url}\nStatus: {status}\nContent: {body}")
    return "\n\n".join(blocks)


def _product_block(p: ProductInput) -> str:
    return (f"Product name: {p.name or '(not given)'}\n"
            f"Description: {p.description or '(not given)'}\n"
            f"Product page: {p.url or '(not given)'}\n"
            f"Target country/region: {p.target_region or '(open – suggest the best)'}")


def product_profile(p: ProductInput, sources: list[Source]) -> str:
    return f"""{_product_block(p)}

Using ONLY the sources below, describe the product.

SOURCES:
{format_sources(sources)}

Return JSON:
{{
  "product_name": str,
  "summary": str (max 3 sentences),
  "features": [{{"text": str, "sources": ["S1", ...]}}],        // 4-8 key technical/commercial features
  "applications": [{{"text": str, "sources": [...]}}],          // 3-8 use cases
  "industries": [{{"text": str, "sources": [...]}}],            // 3-8 customer industries, short names e.g. "Data centers"
  "search_keywords": [str]                                        // 4-6 English keywords buyers use for this product
}}"""


def markets(p: ProductInput, industries: list[str], sources: list[Source]) -> str:
    return f"""{_product_block(p)}
Main customer industries: {', '.join(industries) or 'unknown'}

Suggest at least 2 (max 4) geographic markets (countries, or the target region's countries) to sell this product,
justified ONLY by the sources (demand, policy, growth, projects). If a target region is given, prefer countries in it.
Always return at least 2 markets: if the target is a single country, add the best-supported neighbouring or similar
country from the sources as the second market.

SOURCES:
{format_sources(sources)}

Return JSON:
{{"markets": [{{"name": str (country or region), "rationale": str (2-3 sentences, concrete), "sources": ["S..."]}}]}}"""


def competitors(p: ProductInput, sources: list[Source]) -> str:
    return f"""{_product_block(p)}

Identify at least 3 (max 8) REAL companies that make or sell this kind of product (competitors), found in the sources.
Do not list the manufacturer of the product page itself. Do not invent websites: give the website only if it appears
in the sources or is the company's well-known official domain; otherwise null.

SOURCES:
{format_sources(sources)}

Return JSON:
For "country" (headquarters) and "website": use the sources. If the sources are silent, fill them from general
knowledge for any well-known company (e.g. Atlas Copco → Sweden) and list those fields in "from_general_knowledge";
use null only if you genuinely do not know.

{{"competitors": [{{"name": str, "country": str|null (headquarters), "website": str|null,
   "from_general_knowledge": ["country"|"website", ...],
   "activity": str (what they sell / field of activity), "reason": str (why it competes with this product),
   "sources": ["S..."]}}]}}"""


def customers(p: ProductInput, industries: list[str], markets_: list[str], competitor_names: list[str],
              sources: list[Source]) -> str:
    return f"""{_product_block(p)}
Product customer industries: {', '.join(industries) or 'unknown'}
Suggested markets: {', '.join(markets_) or 'unknown'}
Known competitors (exclude them): {', '.join(competitor_names) or 'none'}

Identify at least 6 (max 12) REAL companies or organisations that appear in the sources and are potential BUYERS of
this product (end users, plant operators, project owners, contractors/integrators who procure it), preferably in the
suggested markets. Do not include manufacturers or sellers of this kind of product.

Include a company only if the sources show a concrete buying signal concerning THAT company, either:
  (a) a project, tender, contract, purchase or use of this kind of product, or
  (b) a new plant, facility, expansion, upgrade or modernisation in one of the customer industries where this
      kind of product is typically needed.
Appearing in a generic list of companies of a sector or country is NOT a buying signal.
Each need_evidence item must state that signal for that specific company, as found in the source.

"name": the company name as written in the source. "industry": a short label of what the company is (e.g. "Dairy
producer", "Electric utility"). "country": where the relevant plant/project is. Use null for anything the sources do
not support.

SOURCES:
{format_sources(sources)}

Return JSON:
{{"customers": [{{"name": str, "country": str|null, "website": str|null, "industry": str|null,
   "activity": str|null (what the company does), "fit_reason": str (why this product fits them),
   "need_evidence": [{{"text": str, "sources": ["S..."]}}],
   "sources": ["S..."]  // sources that mention the company
}}]}}"""


def sales_message(product_summary: str, product_name: str, company: dict, facts: list[str]) -> str:
    facts_txt = "\n".join(f"- {f}" for f in facts) or "- (no verified facts beyond the company profile)"
    return f"""Write a short, personalised B2B cold email in English from the sales team of a manufacturer of
"{product_name}" to the company below.

Product: {product_summary}

Target company profile:
  Name: {company['name']}
  Country: {company.get('country') or 'unknown'}
  Industry: {company.get('industry') or 'unknown'}
  Activity: {company.get('activity') or 'unknown'}
  Why the product fits: {company.get('fit_reason') or 'unknown'}

Facts about the company found online (use only these; do not invent anything else):
{facts_txt}

Rules: 110-170 words body; reference the company's real activity and 1-2 of the facts; describe one concrete use case
of the product for them; one clear call-to-action (a 20-minute call); no fake numbers, prices, names or claims;
sign as "[Your Name], Sales Team". Return JSON: {{"subject": str, "body": str, "facts_used": [str]}}"""
