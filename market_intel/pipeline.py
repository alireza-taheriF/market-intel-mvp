"""Orchestrates the analysis: collect -> extract -> validate -> score -> message.

Each step runs inside `_step()`, which converts any failure into a StepLog
entry instead of an exception. Data gathered by earlier steps is always kept,
and the final status (complete / partial / failed) is derived from what was
actually produced, so a degraded run is never presented as a full analysis.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from market_intel.ai import prompts
from market_intel.ai.extraction import parse_companies, parse_markets, parse_product
from market_intel.ai.llm import LLMClient
from market_intel.collectors.fetcher import PageFetcher
from market_intel.collectors.search import SearchClient, SearchHit
from market_intel.core.config import Settings
from market_intel.core.errors import (
    ExternalServiceError,
    InvalidResponseError,
    LLMUnavailableError,
    NetworkError,
    ServiceTimeoutError,
)
from market_intel.core.faults import SCENARIOS, FaultConfig
from market_intel.core.models import AnalysisResult, Company, ProductInput, Source, StepLog
from market_intel.messaging.sales import generate_messages
from market_intel.processing.cleaning import dedupe_companies, domain_of, remove_overlap
from market_intel.processing.sources import SourceRegistry
from market_intel.processing.validation import (
    classify_source,
    official_domains_for,
    verify_claim,
    verify_company,
)
from market_intel.scoring.features import build_features
from market_intel.scoring.ranking import DEFAULT_WEIGHTS, ScoredCustomer, rank_customers

log = logging.getLogger(__name__)

MIN_MARKETS, MIN_COMPETITORS, MIN_CUSTOMERS, N_MESSAGES = 2, 3, 5, 2

STEPS = [
    ("product", "1. Product research & feature extraction"),
    ("markets", "2. Target market analysis"),
    ("competitors", "3. Competitor identification"),
    ("customers", "4. Potential customer discovery"),
    ("validation", "5. Source & claim validation"),
    ("scoring", "6. Customer ranking (deterministic algorithm)"),
    ("messages", "7. Personalised sales messages"),
]

ProgressFn = Callable[[StepLog, AnalysisResult], None]


class _Skip(Exception):
    """Raised inside a step to mark it as skipped (dependency missing)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Pipeline:
    def __init__(self, settings: Settings, faults: FaultConfig | None = None, *,
                 search: SearchClient | None = None, fetcher: PageFetcher | None = None,
                 llm: LLMClient | None = None, progress: ProgressFn | None = None):
        self.settings = settings
        self.faults = faults or FaultConfig()
        self.search = search or SearchClient(settings, self.faults)
        self.fetcher = fetcher or PageFetcher(settings, self.faults)
        self.llm = llm or LLMClient(settings, self.faults)
        self.progress = progress or (lambda step, result: None)
        self.ai_down: str | None = None      # reason, once the AI model failed hard
        self.search_down: str | None = None  # reason, once web search failed hard

    # --- public ------------------------------------------------------------------

    def run(self, product_input: ProductInput, weights: dict[str, float] | None = None) -> AnalysisResult:
        scenario = next((k for k, v in SCENARIOS.items() if v == self.faults), "custom")
        self.result = AnalysisResult(
            run_id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
            created_at=_now(), input=product_input, fault_scenario=scenario,
            steps=[StepLog(name=label) for _, label in STEPS],
        )
        self.registry = SourceRegistry()
        self.result.sources = self.registry.sources
        self.weights = weights or DEFAULT_WEIGHTS
        self.ranking: list[ScoredCustomer] = []

        if not (product_input.name or product_input.description or product_input.url):
            self.result.errors.append("Please provide a product name, a description or a product URL.")
            self.result.status = "failed"
            return self.result

        for (key, _), step in zip(STEPS, self.result.steps):
            self._step(step, getattr(self, f"_step_{key}"))
        self.result.status = self._final_status()
        return self.result

    # --- step runner -------------------------------------------------------------

    def _step(self, step: StepLog, fn: Callable[[], str | None]) -> None:
        step.status = "running"
        self.progress(step, self.result)
        t0 = time.monotonic()
        try:
            note = fn()
            step.status = "warning" if note else "ok"
            step.message = note or "done"
        except _Skip as exc:
            step.status, step.message = "skipped", str(exc)
        except ExternalServiceError as exc:
            step.status, step.message = "failed", exc.user_message
            self.result.errors.append(f"{step.name}: {exc.user_message}")
            self._note_outage(exc)
        except Exception as exc:  # last-resort guard: a bug must not kill the whole run
            log.exception("unexpected error in step %s", step.name)
            step.status, step.message = "failed", f"Internal error: {type(exc).__name__}: {exc}"
            self.result.errors.append(f"{step.name}: internal error ({exc})")
        step.duration_s = round(time.monotonic() - t0, 1)
        self.progress(step, self.result)

    def _note_outage(self, exc: ExternalServiceError) -> None:
        """After a hard outage, later steps skip that service instead of waiting on it again."""
        if exc.service == "AI model" and isinstance(exc, (LLMUnavailableError, NetworkError, ServiceTimeoutError)):
            self.ai_down = exc.user_message
        elif "search" in exc.service.lower() and isinstance(exc, (NetworkError, ServiceTimeoutError)):
            self.search_down = exc.user_message

    def _require_ai(self) -> None:
        if self.ai_down:
            raise _Skip(f"Skipped – AI model unavailable earlier in this run ({self.ai_down})")

    # --- collection helpers ------------------------------------------------------

    def _collect(self, queries: list[str], extra_urls: list[str] | None = None) -> tuple[list[Source], list[str]]:
        """Search, register, fetch (accessibility check) and return the step's sources."""
        notes: list[str] = []
        hits: list[SearchHit] = []
        if self.search_down:
            if not extra_urls:
                raise _Skip(f"Skipped – web search unavailable earlier in this run ({self.search_down})")
            notes.append("web search skipped (unavailable earlier in this run)")
        else:
            try:
                hits, errs = self.search.search_many(queries)
                if errs:
                    notes.append(f"{len(errs)}/{len(queries)} search queries failed")
            except ExternalServiceError as exc:
                self._note_outage(exc)
                if not extra_urls:
                    raise
                notes.append(f"web search failed: {exc.user_message}")

        step_sources: list[Source] = [self.registry.add_url(u, query="(user-provided URL)") for u in extra_urls or []]
        for h in hits:
            src = self.registry.add_hit(h)
            if src not in step_sources:
                step_sources.append(src)

        to_fetch = [s for s in step_sources if s.accessible is None][: self.settings.max_pages_per_step]
        results = self.fetcher.fetch_many([s.url for s in to_fetch])
        for s in to_fetch:
            self.registry.apply_fetch(s, results[s.url])
        usable = [s for s in step_sources if s.text or s.snippet]
        if not usable:
            if not hits and not self.search_down:
                raise InvalidResponseError(
                    "search returned no results – the keyless provider is probably rate-limiting this IP; "
                    "wait a few minutes or set TAVILY_API_KEY", service="Web search")
            raise NetworkError("no usable web sources could be collected for this step", service="Web collection")
        unreachable = sum(1 for s in to_fetch if not s.accessible)
        if unreachable:
            notes.append(f"{unreachable}/{len(to_fetch)} pages not accessible (snippet used)")
        return usable[:24], notes

    def _label(self) -> str:
        """Product name for search queries, without parenthetical parts like '(BESS)'."""
        p = self.result.product
        name = (p.name if p else "") or self.result.input.label
        return re.sub(r"\s*\([^)]*\)", "", name).strip() or name

    def _keyword(self) -> str:
        p = self.result.product
        return (p.keywords[0] if p and p.keywords else "") or self._label()

    def _region(self) -> str:
        return self.result.input.target_region.strip()

    # --- steps -------------------------------------------------------------------

    def _step_product(self) -> str | None:
        inp, label = self.result.input, self._label()
        queries = [f"{label} features specifications", f"{label} applications industries use cases"]
        if inp.description and inp.name:
            queries.append(f"{inp.name} {inp.description[:80]}")
        extra = [inp.url] if inp.url else []
        sources, notes = self._collect(queries, extra)
        if inp.url and (src := self.registry.add_url(inp.url)) and not src.accessible:
            notes.append(f"product page not accessible ({src.error})")
        self._require_ai()
        data = self.llm.complete_json(prompts.product_profile(inp, sources))
        self.result.product = parse_product(data, fallback_name=label)
        return "; ".join(notes) or None

    def _step_markets(self) -> str | None:
        self._require_ai()  # every later step needs the AI model; don't search in vain
        label, region, kw = self._label(), self._region(), self._keyword()
        if region:
            queries = [f"{kw} market growth {region}", f"{label} demand {region} 2026", f"{kw} projects policy {region}"]
        else:
            queries = [f"{kw} fastest growing markets by country", f"{label} market by country 2026",
                       f"{kw} government targets incentives countries"]
        sources, notes = self._collect(queries)
        self._require_ai()
        industries = [c.text for c in self.result.product.industries] if self.result.product else []
        markets = parse_markets(self.llm.complete_json(prompts.markets(self.result.input, industries, sources)))
        self.result.markets = markets[:4]
        if len(markets) < MIN_MARKETS:
            notes.append(f"only {len(markets)} market(s) found (minimum {MIN_MARKETS})")
        return "; ".join(notes) or None

    def _step_competitors(self) -> str | None:
        self._require_ai()  # every later step needs the AI model; don't search in vain
        label, kw, region = self._label(), self._keyword(), self._region()
        queries = [f"top {kw} manufacturers", f"leading {label} companies suppliers"]
        if region:
            queries.append(f"{kw} suppliers {region}")
        sources, notes = self._collect(queries)
        self._require_ai()
        comps = parse_companies(self.llm.complete_json(prompts.competitors(self.result.input, sources)),
                                "competitors", "competitor")
        own = domain_of(self.result.input.url)
        comps = [c for c in comps if not own or domain_of(c.website) != own]
        comps, merged = dedupe_companies(comps)
        self.result.duplicates_removed += merged
        self._check_websites(comps)
        self.result.competitors = comps
        if len(comps) < MIN_COMPETITORS:
            notes.append(f"only {len(comps)} competitor(s) found (minimum {MIN_COMPETITORS})")
        return "; ".join(notes) or None

    def _step_customers(self) -> str | None:
        self._require_ai()  # every later step needs the AI model; don't search in vain
        kw = self._keyword()
        product = self.result.product
        industries = [c.text for c in product.industries][:3] if product else []
        markets = [m.name for m in self.result.markets][:2] or ([self._region()] if self._region() else [])
        queries = [f"{ind} company {mk} {kw}" for mk in markets for ind in industries[:2]]
        queries += [f"{kw} project {mk} awarded" for mk in markets] or [f"{kw} project awarded customer"]
        if not industries:
            queries.append(f"{kw} customers case study")
        sources, notes = self._collect(queries[:7])
        self._require_ai()
        prompt = prompts.customers(self.result.input, [c.text for c in product.industries] if product else [],
                                   markets, [c.name for c in self.result.competitors], sources)
        found = parse_companies(self.llm.complete_json(prompt), "customers", "customer")
        found, merged = dedupe_companies(found)
        found, overlap = remove_overlap(found, self.result.competitors)
        self.result.duplicates_removed += merged + overlap
        self._check_websites(found)
        self.result.customers = found
        if len(found) < MIN_CUSTOMERS:
            notes.append(f"only {len(found)} customer(s) found (minimum {MIN_CUSTOMERS})")
        return "; ".join(notes) or None

    def _check_websites(self, companies: list[Company]) -> None:
        """Reachability check of each company website; the homepage is logged as an official source."""
        urls = [c.website for c in companies if c.website]
        results = self.fetcher.fetch_many(urls)
        for c in companies:
            if not c.website:
                c.website_reachable = None
                continue
            res = results[c.website]
            # 401/403 = bot protection: the site exists even though we may not read it.
            c.website_reachable = res.ok or res.status in (401, 403)
            src = self.registry.add_url(c.website, title=f"{c.name} – official website", query="(website check)")
            if src.accessible is None:
                self.registry.apply_fetch(src, res)

    def _step_validation(self) -> str | None:
        r = self.result
        if not r.sources:
            raise _Skip("Skipped – no sources were collected")
        official = official_domains_for(r.input.url, r.competitors + r.customers)
        for s in r.sources.values():
            s.source_type = classify_source(s, official)
        if r.product:
            for claim in r.product.features + r.product.applications + r.product.industries:
                verify_claim(claim, r.sources)
        for m in r.markets:
            verify_claim(m.rationale, r.sources)
        for c in r.competitors + r.customers:
            verify_company(c, r.sources)
        if not (r.product or r.markets or r.competitors or r.customers):
            return "sources were checked, but no claims were extracted to validate"
        return None

    def _step_scoring(self) -> str | None:
        r = self.result
        if not r.customers:
            raise _Skip("Skipped – no customers to rank")
        if not r.product:
            raise _Skip("Skipped – product profile missing")
        self.ranking = rank(r, self.weights)
        return None

    def _step_messages(self) -> str | None:
        r = self.result
        if not self.ranking:
            raise _Skip("Skipped – no ranked customers")
        by_name = {c.name: c for c in r.customers}
        top = [by_name[s.name] for s in self.ranking[:N_MESSAGES]]
        r.messages, errors = generate_messages(self.llm, top, r.product, ai_available=not self.ai_down)
        r.warnings += errors
        templated = [m.company_name for m in r.messages if m.generated_by != "ai"]
        if templated:
            return f"AI unavailable for {', '.join(templated)} – template fallback used (not AI-personalised)"
        return None

    # --- status ------------------------------------------------------------------

    def _final_status(self) -> str:
        r = self.result
        if not (r.product or r.markets or r.competitors or r.customers):
            return "failed"
        shortfalls = []
        if len(r.markets) < MIN_MARKETS:
            shortfalls.append(f"markets {len(r.markets)}/{MIN_MARKETS}")
        if len(r.competitors) < MIN_COMPETITORS:
            shortfalls.append(f"competitors {len(r.competitors)}/{MIN_COMPETITORS}")
        if len(r.customers) < MIN_CUSTOMERS:
            shortfalls.append(f"customers {len(r.customers)}/{MIN_CUSTOMERS}")
        if len(r.messages) < N_MESSAGES or any(m.generated_by != "ai" for m in r.messages):
            shortfalls.append("AI sales messages incomplete")
        if shortfalls:
            r.warnings.append("Incomplete analysis: " + ", ".join(shortfalls))
        bad_steps = any(s.status in ("failed", "skipped") for s in r.steps)
        return "partial" if shortfalls or bad_steps else "complete"


def rank(result: AnalysisResult, weights: dict[str, float]) -> list[ScoredCustomer]:
    """Feature extraction + ranking; also used by the UI to re-rank when weights change."""
    if not result.product or not result.customers:
        return []
    feats = build_features(result.customers, result.product, result.input.target_region, result.markets)
    return rank_customers(feats, weights)
