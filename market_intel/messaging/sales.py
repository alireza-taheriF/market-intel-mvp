"""Personalised sales messages for the top-ranked customers.

The LLM writes the message from verified facts only. If the AI model is not
available, a clearly-labelled template message is produced instead, so the
user still gets a usable draft and is never told it was AI-personalised.
"""
from __future__ import annotations

import logging

from market_intel.ai import prompts
from market_intel.ai.llm import LLMClient
from market_intel.core.errors import ExternalServiceError, InvalidResponseError
from market_intel.core.models import Company, ProductProfile, SalesMessage, Verification
from market_intel.processing.cleaning import clean_text

log = logging.getLogger(__name__)


def usable_facts(company: Company, limit: int = 4) -> list[str]:
    """Evidence the message may reference: verified first, then probable. Never unverified."""
    ranked = sorted(
        (e for e in company.evidence if e.status != Verification.UNVERIFIED),
        key=lambda e: (e.status != Verification.VERIFIED, -e.relevance),
    )
    facts = [e.text for e in ranked[:limit]]
    if company.activity:
        facts.insert(0, f"Company activity: {company.activity}")
    return facts


def _company_dict(c: Company) -> dict:
    return {
        "name": c.name, "country": c.country, "industry": c.industry, "activity": c.activity,
        "fit_reason": c.reason.text if c.reason else None,
    }


def template_message(company: Company, product: ProductProfile) -> SalesMessage:
    """Deterministic fallback used when the AI model is unavailable."""
    activity = company.activity or company.industry or "your operations"
    use_case = company.reason.text if company.reason else f"{product.name} can support {activity}."
    body = (
        f"Dear {company.name} team,\n\n"
        f"I am reaching out because of your work in {activity}"
        f"{' in ' + company.country if company.country else ''}. {use_case}\n\n"
        f"Our {product.name}: {product.summary or 'an industrial solution tailored to your sector.'}\n\n"
        "Would you be open to a 20-minute call to explore whether it fits your plans?\n\n"
        "Best regards,\n[Your Name], Sales Team"
    )
    return SalesMessage(
        company_name=company.name,
        subject=f"{product.name} for {company.name}",
        body=body,
        generated_by="template",
        facts_used=usable_facts(company),
    )


def generate_message(llm: LLMClient, company: Company, product: ProductProfile) -> SalesMessage:
    facts = usable_facts(company)
    prompt = prompts.sales_message(product.summary or product.name, product.name, _company_dict(company), facts)
    data = llm.complete_json(prompt, max_tokens=4000)
    subject, body = clean_text(data.get("subject")), data.get("body")
    if not subject or not isinstance(body, str) or len(body.split()) < 40:
        raise InvalidResponseError("sales message is missing a subject or body", service="AI model")
    used = data.get("facts_used")
    return SalesMessage(
        company_name=company.name, subject=subject, body=body.strip(), generated_by="ai",
        facts_used=[str(f) for f in used] if isinstance(used, list) else facts,
    )


def generate_messages(llm: LLMClient | None, companies: list[Company], product: ProductProfile,
                      ai_available: bool = True) -> tuple[list[SalesMessage], list[str]]:
    """Messages for the given (already ranked) companies. Returns (messages, error notes)."""
    out, errors = [], []
    for c in companies:
        if llm is None or not ai_available:
            msg = template_message(c, product)
            msg.error = "AI model unavailable – template fallback, not AI-personalised"
        else:
            try:
                msg = generate_message(llm, c, product)
            except ExternalServiceError as exc:
                log.warning("sales message for %s failed: %s", c.name, exc)
                errors.append(f"Sales message for {c.name}: {exc.user_message}")
                msg = template_message(c, product)
                msg.error = f"AI generation failed ({exc}) – template fallback shown"
        out.append(msg)
    return out, errors
