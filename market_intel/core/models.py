"""Data model shared by all stages.

Plain dataclasses (no LLM or UI dependency) so collection, processing, scoring
and messaging stay decoupled and every stage is testable on its own.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class SourceType(str, Enum):
    OFFICIAL = "official"        # the entity's own website (manufacturer / company)
    INDEPENDENT = "independent"  # recognised third party: gov, edu, news, industry media, research
    UNKNOWN = "unknown"          # credibility cannot be established


class Verification(str, Enum):
    VERIFIED = "verified"      # backed by an accessible, relevant, credible source
    PROBABLE = "probable"      # some support, but weak / single unknown source
    UNVERIFIED = "unverified"  # no usable evidence – must not be trusted


@dataclass
class Source:
    id: str
    url: str
    title: str = ""
    snippet: str = ""
    query: str = ""
    text: str = ""
    accessible: bool | None = None  # None = not checked
    http_status: int | None = None
    error: str | None = None
    source_type: SourceType = SourceType.UNKNOWN
    checked_at: str | None = None

    @property
    def domain(self) -> str:
        host = urlparse(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    @property
    def evidence_text(self) -> str:
        """Text used to check claims: page text if fetched, otherwise the search snippet."""
        return self.text or f"{self.title}\n{self.snippet}"


@dataclass
class Claim:
    text: str
    source_ids: list[str] = field(default_factory=list)
    status: Verification = Verification.UNVERIFIED
    relevance: float = 0.0
    note: str = ""


@dataclass
class ProductInput:
    name: str
    description: str = ""
    url: str = ""
    target_region: str = ""

    @property
    def label(self) -> str:
        return self.name or self.description[:60] or self.url


@dataclass
class ProductProfile:
    name: str
    summary: str = ""
    features: list[Claim] = field(default_factory=list)
    applications: list[Claim] = field(default_factory=list)
    industries: list[Claim] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class Market:
    name: str
    rationale: Claim


@dataclass
class Company:
    name: str
    role: str  # "competitor" | "customer"
    country: str | None = None
    website: str | None = None
    industry: str | None = None
    activity: str | None = None
    reason: Claim | None = None
    evidence: list[Claim] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    website_reachable: bool | None = None
    status: Verification = Verification.UNVERIFIED
    missing_fields: list[str] = field(default_factory=list)
    merged_from: list[str] = field(default_factory=list)


@dataclass
class SalesMessage:
    company_name: str
    subject: str = ""
    body: str = ""
    generated_by: str = "ai"  # "ai" | "template"
    facts_used: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class StepLog:
    name: str
    status: str = "pending"  # pending|running|ok|warning|failed|skipped
    message: str = ""
    duration_s: float = 0.0


@dataclass
class AnalysisResult:
    run_id: str
    created_at: str
    input: ProductInput
    fault_scenario: str = "None (real services)"
    status: str = "running"  # running|complete|partial|failed
    steps: list[StepLog] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    product: ProductProfile | None = None
    markets: list[Market] = field(default_factory=list)
    competitors: list[Company] = field(default_factory=list)
    customers: list[Company] = field(default_factory=list)
    messages: list[SalesMessage] = field(default_factory=list)
    sources: dict[str, Source] = field(default_factory=dict)
    duplicates_removed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnalysisResult":
        def claim(c):
            if c is None:
                return None
            return Claim(**{**c, "status": Verification(c.get("status", "unverified"))})

        def company(c):
            return Company(**{
                **c,
                "reason": claim(c.get("reason")),
                "evidence": [claim(e) for e in c.get("evidence", [])],
                "status": Verification(c.get("status", "unverified")),
            })

        product = d.get("product")
        if product:
            product = ProductProfile(
                name=product["name"],
                summary=product.get("summary", ""),
                features=[claim(c) for c in product.get("features", [])],
                applications=[claim(c) for c in product.get("applications", [])],
                industries=[claim(c) for c in product.get("industries", [])],
                keywords=product.get("keywords", []),
            )
        return cls(
            run_id=d["run_id"],
            created_at=d["created_at"],
            input=ProductInput(**d["input"]),
            fault_scenario=d.get("fault_scenario", ""),
            status=d.get("status", "failed"),
            steps=[StepLog(**s) for s in d.get("steps", [])],
            errors=d.get("errors", []),
            warnings=d.get("warnings", []),
            product=product,
            markets=[Market(name=m["name"], rationale=claim(m["rationale"])) for m in d.get("markets", [])],
            competitors=[company(c) for c in d.get("competitors", [])],
            customers=[company(c) for c in d.get("customers", [])],
            messages=[SalesMessage(**m) for m in d.get("messages", [])],
            sources={k: Source(**{**v, "source_type": SourceType(v.get("source_type", "unknown"))})
                     for k, v in d.get("sources", {}).items()},
            duplicates_removed=d.get("duplicates_removed", []),
        )
