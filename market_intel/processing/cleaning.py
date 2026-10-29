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
