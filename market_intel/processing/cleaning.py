"""Cleaning, normalisation, unknown-value handling and de-duplication."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from market_intel.core.models import Claim, Company, Verification

# Strings an LLM or web page uses to mean "we don't know".
_UNKNOWN_TOKENS = {
    "", "unknown", "n/a", "na", "none", "null", "not available", "not specified",
    "unspecified", "-", "--", "?", "tbd", "various", "not found", "not mentioned",
}

_LEGAL_SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co", "limited", "ltd", "llc",
    "plc", "gmbh", "ag", "sa", "s.a", "spa", "s.p.a", "bv", "b.v", "nv", "n.v", "ab", "as",
    "oy", "pjsc", "psc", "jsc", "pty", "pvt", "kk", "group", "holding", "holdings",
)

_STATUS_RANK = {Verification.VERIFIED: 2, Verification.PROBABLE: 1, Verification.UNVERIFIED: 0}


def clean_text(value: object) -> str | None:
    """Collapse whitespace; map placeholders like 'N/A' to None."""
    if value is None:
        return None
    text = " ".join(str(value).split()).strip(" .;,")
    return None if text.lower() in _UNKNOWN_TOKENS else text


def normalize_url(value: object) -> str | None:
    text = clean_text(value)
    if not text or " " in text:
        return None
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "://" in url else "https://" + url).netloc.lower().split(":")[0]
    return (host[4:] if host.startswith("www.") else host) or None


def normalize_company_name(name: str) -> str:
    """'Siemens Energy AG' -> 'siemens energy'; used only as a dedup key."""
    key = re.sub(r"[^\w\s&.]", " ", name.lower())
    tokens = [t.strip(".") for t in key.split()]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(t for t in tokens if t)


def missing_fields(company: Company) -> list[str]:
    fields = {"country": company.country, "website": company.website,
              "industry": company.industry, "activity": company.activity}
    if company.role == "customer":
        fields["fit reason"] = company.reason.text if company.reason else None
    return [k for k, v in fields.items() if not v]


def _merge_claims(a: list[Claim], b: list[Claim]) -> list[Claim]:
    seen = {c.text.lower() for c in a}
    return a + [c for c in b if c.text.lower() not in seen]


def _merge(primary: Company, other: Company) -> Company:
    """Fill gaps in `primary` from `other`; never overwrite known values."""
    for attr in ("country", "website", "industry", "activity"):
        if not getattr(primary, attr) and getattr(other, attr):
            setattr(primary, attr, getattr(other, attr))
    if primary.reason is None:
        primary.reason = other.reason
    primary.evidence = _merge_claims(primary.evidence, other.evidence)
    primary.source_ids = list(dict.fromkeys(primary.source_ids + other.source_ids))
    primary.merged_from.append(other.name)
    return primary


def _completeness(c: Company) -> tuple[int, int, int]:
    return (_STATUS_RANK[c.status], 4 - len(missing_fields(c)), len(c.evidence))


def dedupe_companies(companies: list[Company]) -> tuple[list[Company], list[str]]:
    """Merge records that share a website domain or a normalised name.

    The most complete record survives and absorbs the missing fields of the
    others. Returns (unique companies, human-readable merge log).
    """
    ordered = sorted(companies, key=_completeness, reverse=True)
    kept: list[Company] = []
    by_key: dict[str, Company] = {}
    log: list[str] = []
    for c in ordered:
        keys = {f"name:{normalize_company_name(c.name)}"}
        if d := domain_of(c.website):
            keys.add(f"domain:{d}")
        match = next((by_key[k] for k in keys if k in by_key), None)
        if match is None:
            kept.append(c)
            target = c
        else:
            _merge(match, c)
            log.append(f"'{c.name}' merged into '{match.name}' (duplicate {c.role})")
            target = match
        for k in keys:
            by_key.setdefault(k, target)
    for c in kept:
        c.missing_fields = missing_fields(c)
    return kept, log


def remove_overlap(customers: list[Company], competitors: list[Company]) -> tuple[list[Company], list[str]]:
    """A company identified as a competitor must not also be listed as a customer."""
    comp_keys = {normalize_company_name(c.name) for c in competitors}
    comp_keys |= {d for c in competitors if (d := domain_of(c.website))}
    kept, log = [], []
    for c in customers:
        if normalize_company_name(c.name) in comp_keys or domain_of(c.website) in comp_keys:
            log.append(f"'{c.name}' removed from customers (it is a competitor)")
        else:
            kept.append(c)
    return kept, log


# Words that don't identify a company, so they can't be used to match its domain.
_GENERIC_NAME_TOKENS = {
    "the", "and", "for", "company", "companies", "group", "international", "industries", "industry",
    "holding", "holdings", "corporation", "global", "national", "middle", "east", "saudi", "arabia",
    "arabian", "emirates", "united", "contracting", "commercial", "development", "projects", "services",
    "solutions", "systems", "technology", "technologies", "energy", "power", "data", "centers", "centres",
}

# Aggregators, social networks and news sites are never a company's own website.
NOT_COMPANY_SITES = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "wikipedia.org",
    "crunchbase.com", "bloomberg.com", "zoominfo.com", "rocketreach.co", "tracxn.com", "pitchbook.com",
    "leadiq.com", "dnb.com", "glassdoor.com", "indeed.com", "yahoo.com", "google.com", "zawya.com",
    "reuters.com", "argaam.com", "wikipedia.com", "craft.co", "cbinsights.com", "owler.com", "globaldata.com",
    "saudipedia.com", "investriyadh.ai", "energynews.pro", "apollo.io", "signalhire.com", "theorg.com",
)


def name_tokens(name: str) -> list[str]:
    """Distinctive tokens of a company name, acronyms like '(EWEC)', and initials ('Saudi Electricity' → 'se')."""
    acronyms = [a.lower() for a in re.findall(r"\b([A-Z]{2,6})\b", name)]
    words = [t for t in re.split(r"[^a-z0-9]+", normalize_company_name(re.sub(r"\([^)]*\)", " ", name))) if t]
    distinctive = [t for t in words if len(t) >= 4 and t not in _GENERIC_NAME_TOKENS]
    initials = "".join(w[0] for w in words if w not in ("the", "and", "of")) if len(words) >= 2 else ""
    # 2-letter initials are ambiguous ("Saudi Energy" vs "Saudi Electricity" → se.com.sa): need 3+.
    return list(dict.fromkeys(acronyms + distinctive + ([initials] if 3 <= len(initials) <= 5 else [])))


# Endings a company domain may add to its brand: acwa+power, vinfast+auto, alsharif+group.
_DOMAIN_SUFFIXES = {"group", "global", "corp", "inc", "co", "energy", "power", "auto", "tech", "intl", "world",
                    "online", "official", "usa", "us", "uk", "plc", "ltd", "company", "the", "hq", "inc"}


def _label_matches(label: str, tokens: list[str], all_words: set[str]) -> bool:
    """The domain label must be built only from words of the name plus usual company endings, and contain at least
    one distinctive token: 'alsharifgroup' = al+sharif+group ✓, but 'ashlandmass' (a town) and
    'aluminiumstewardship' (an association) leave unexplained letters ✗."""
    vocab = sorted((all_words | _DOMAIN_SUFFIXES | set(tokens)) - {""}, key=len, reverse=True)

    def parse(rest: str, seen_token: bool) -> bool:  # small backtracking split; labels are short
        if not rest:
            return seen_token
        return any(parse(rest[len(w):], seen_token or w in tokens) for w in vocab if rest.startswith(w))

    return parse(label, False)


def _institution_mismatch(name: str, domain: str) -> bool:
    """A company is not a university (.edu) or a government body (.gov…), unless its name says so."""
    n = name.lower()
    if re.search(r"\.edu(\.|$)", domain) and not re.search(r"universit|college|school|institut", n):
        return True
    if re.search(r"\.gov(\.|$)", domain) and not re.search(r"ministry|government|authority|department|agency|"
                                                          r"city|municipal|state|office|procurement", n):
        return True
    return False


_SUFFIX_PARTS = {"com", "net", "org", "gov", "co", "ac", "edu", "biz", "info"}


def _registrable(domain: str) -> tuple[str, str]:
    """('jobs.se.com.sa') → ('se', 'se.com.sa'): main label and registrable domain."""
    parts = domain.split(".")
    i = len(parts) - 1
    while i > 0 and (parts[i] in _SUFFIX_PARTS or len(parts[i]) == 2):
        i -= 1
    return parts[i], ".".join(parts[i:])


def pick_official_website(name: str, candidate_urls: list[str]) -> str | None:
    """First search result whose domain matches the company name, as its homepage URL.

    Rules: a short acronym must equal or start the domain's main label (stc.com.sa, bydglobal.com);
    a word of 4+ letters must be contained in it (khazna.ae, acwapower.com). Aggregators/social/news sites are skipped.
    """
    tokens = name_tokens(name)
    if not tokens:
        return None
    all_words = set(re.split(r"[^a-z0-9]+", normalize_company_name(name))) - {""}
    for url in candidate_urls:
        domain = domain_of(url)
        if not domain or any(domain == s or domain.endswith("." + s) for s in NOT_COMPANY_SITES):
            continue
        label, registrable = _registrable(domain)
        label = label.replace("-", "")
        if _institution_mismatch(name, domain):
            continue
        if _label_matches(label, tokens, all_words):
            netloc = urlparse(url).netloc.lower()
            # Homepage of the company, not a sub-site such as jobs.se.com.sa.
            return f"https://{netloc}" if netloc in (registrable, "www." + registrable) else f"https://www.{registrable}"
    return None


_TITLE_NOISE = {"home", "products", "product", "shop", "official site", "official website", "buy online"}


def product_name_from_page(title: str, url: str) -> str | None:
    """Best product name for a link-only input: the page title's most specific part, else the URL slug.

    'SIMATIC S7-1500 PLC – High-Performance Controller | Siemens' → 'SIMATIC S7-1500 PLC'
    '.../products/cr-vertical-multistage-pumps' → 'cr vertical multistage pumps'
    """
    brand = (domain_of(url) or "").split(".")[0]
    parts = [p.strip() for p in re.split(r"\s[|–—:·]\s", title or "") if p.strip()]
    # Drop the site/brand segment ("| Siemens") and navigation words; the first remaining part names the product.
    parts = [p for p in parts if p.lower() not in _TITLE_NOISE and p.lower().replace(" ", "") != brand and len(p) >= 3]
    if parts:
        return parts[0][:120]
    path = [s for s in urlparse(url).path.split("/") if s and not s.isdigit()]
    for seg in reversed(path):
        words = re.sub(r"\.(html?|php|aspx?)$", "", seg).replace("-", " ").replace("_", " ").strip()
        if len(words) >= 4 and words.lower() not in _TITLE_NOISE:
            return words[:120]
    return None
