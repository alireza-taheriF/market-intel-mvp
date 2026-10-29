"""Convert the LLM's JSON into typed, cleaned model objects.

LLM output is treated as untrusted: wrong types are dropped, placeholder
values become None, and every claim keeps the source ids it cited so the
validation stage can check them.
"""
from __future__ import annotations

from typing import Any

from market_intel.core.errors import InvalidResponseError
from market_intel.core.models import Claim, Company, Market, ProductProfile
from market_intel.processing.cleaning import clean_text, normalize_url


def _list(data: dict, key: str) -> list:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip().strip("[]") for v in value if str(v).strip()]


def _claims(items: list) -> list[Claim]:
    out = []
    for item in items:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"))
        if text:
            out.append(Claim(text=text, source_ids=_ids(item.get("sources"))))
    return out


def parse_product(data: dict, fallback_name: str) -> ProductProfile:
    profile = ProductProfile(
        name=clean_text(data.get("product_name")) or fallback_name,
        summary=clean_text(data.get("summary")) or "",
        features=_claims(_list(data, "features")),
        applications=_claims(_list(data, "applications")),
        industries=_claims(_list(data, "industries")),
        keywords=[k for k in (clean_text(x) for x in _list(data, "search_keywords")) if k],
    )
    if not (profile.features or profile.applications or profile.industries):
        raise InvalidResponseError("no product features/applications/industries extracted", service="AI model")
    return profile


def parse_markets(data: dict) -> list[Market]:
    out = []
    for m in _list(data, "markets"):
        if not isinstance(m, dict) or not (name := clean_text(m.get("name"))):
            continue
        rationale = clean_text(m.get("rationale")) or ""
        out.append(Market(name=name, rationale=Claim(rationale, _ids(m.get("sources")))))
    return out


def _company(item: dict, role: str) -> Company | None:
    name = clean_text(item.get("name"))
    if not name:
        return None
    reason_text = clean_text(item.get("fit_reason") if role == "customer" else item.get("reason"))
    src = _ids(item.get("sources"))
    return Company(
        name=name,
        role=role,
        country=clean_text(item.get("country")),
        website=normalize_url(item.get("website")),
        industry=clean_text(item.get("industry")),
        activity=clean_text(item.get("activity")),
        reason=Claim(reason_text, src) if reason_text else None,
        evidence=_claims(_list(item, "need_evidence")),
        source_ids=src,
    )


def parse_companies(data: dict, key: str, role: str) -> list[Company]:
    return [c for item in _list(data, key) if isinstance(item, dict) and (c := _company(item, role))]
