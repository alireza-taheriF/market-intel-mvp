"""Web search behind a small provider-agnostic interface.

Providers:
  * Tavily (if TAVILY_API_KEY is set) – an API built for LLM research.
  * DuckDuckGo via the `ddgs` package – no key needed, good for an MVP.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from market_intel.core.config import Settings
from market_intel.core.errors import (
    ExternalServiceError,
    InvalidResponseError,
    NetworkError,
    SearchError,
    ServiceTimeoutError,
)
from market_intel.core.faults import FaultConfig

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str
    query: str


class SearchClient:
    def __init__(self, settings: Settings, faults: FaultConfig | None = None):
        self.settings = settings
        self.faults = faults or FaultConfig()

    @property
    def provider(self) -> str:
        p = self.settings.search_provider.lower()
        if p == "auto":
            return "tavily" if self.settings.tavily_api_key else "duckduckgo"
        return p

    def search(self, query: str, max_results: int | None = None,
               exclude_domains: list[str] | None = None) -> list[SearchHit]:
        n = max_results or self.settings.results_per_query
        if self.faults.network_down:
            raise NetworkError("Simulated: no internet connection", service="Web search")
        if self.faults.search_timeout:
            raise ServiceTimeoutError(
                f"Simulated: search request timed out after {self.settings.http_timeout_s:.0f}s",
                service="Web search",
            )
        if self.provider == "tavily":
            hits = self._tavily(query, n, exclude_domains)
        else:
            hits = self._duckduckgo(query, n)
        return [h for h in hits if h.url.startswith(("http://", "https://"))]

    def search_many(self, queries: list[str], max_results: int | None = None) -> tuple[list[SearchHit], list[str]]:
        """Run several queries; tolerate individual failures.

        Raises only if *every* query failed, so one flaky query never kills a step.
        """
        hits: list[SearchHit] = []
        errors: list[str] = []
        last_exc: ExternalServiceError | None = None
        for i, q in enumerate(queries):
            if i and self.provider == "duckduckgo":
                time.sleep(self.settings.search_delay_s)  # keyless search rate-limits bursts
            try:
                found = self.search(q, max_results)
                if not found and self.provider == "duckduckgo" and not self.faults.active:
                    time.sleep(self.settings.search_delay_s * 3)  # one retry: empty often means throttled
                    found = self.search(q, max_results)
                hits.extend(found)
            except ExternalServiceError as exc:
                log.warning("search failed for %r: %s", q, exc)
                errors.append(f"'{q}': {exc}")
                last_exc = exc
        if not hits and last_exc is not None:
            raise last_exc
        return hits, errors

    # --- providers -----------------------------------------------------------------

    def _tavily(self, query: str, n: int, exclude_domains: list[str] | None = None) -> list[SearchHit]:
        payload = {"api_key": self.settings.tavily_api_key, "query": query, "max_results": n}
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=self.settings.http_timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.Timeout as exc:
            raise ServiceTimeoutError(str(exc), service="Tavily search") from exc
        except requests.ConnectionError as exc:
            raise NetworkError(str(exc), service="Tavily search") from exc
        except requests.HTTPError as exc:
            raise SearchError(f"HTTP {exc.response.status_code}", service="Tavily search") from exc
        except ValueError as exc:
            raise InvalidResponseError("response is not JSON", service="Tavily search") from exc
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise InvalidResponseError("missing 'results' list", service="Tavily search")
        return [
            SearchHit(r.get("title", ""), r.get("url", ""), r.get("content", ""), query)
            for r in results if isinstance(r, dict)
        ]

    def _duckduckgo(self, query: str, n: int) -> list[SearchHit]:
        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException, TimeoutException
        except ImportError as exc:  # pragma: no cover - dependency problem
            raise SearchError("package 'ddgs' is not installed") from exc
        try:
            raw = DDGS(timeout=int(self.settings.http_timeout_s)).text(query, max_results=n) or []
        except TimeoutException as exc:
            raise ServiceTimeoutError(str(exc), service="DuckDuckGo search") from exc
        except DDGSException as exc:
            msg = str(exc)
            if "No results" in msg:
                return []
            raise SearchError(msg, service="DuckDuckGo search") from exc
        except Exception as exc:  # network stack errors from the library
            raise NetworkError(str(exc), service="DuckDuckGo search") from exc
        return [
            SearchHit(r.get("title", ""), r.get("href", ""), r.get("body", ""), query)
            for r in raw if isinstance(r, dict)
        ]
