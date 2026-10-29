"""Fetch web pages, extract readable text, and check link accessibility.

Never raises: every outcome (including network errors) is returned as a
FetchResult, because an unreachable source is *data* for validation, not a
reason to stop the analysis.
"""
from __future__ import annotations

import ipaddress
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from market_intel.core.config import Settings
from market_intel.core.faults import FaultConfig

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 market-intel-mvp/0.1"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    title: str = ""
    text: str = ""
    error: str | None = None


def is_public_http_url(url: str) -> bool:
    """Only fetch public http(s) URLs (no file://, localhost or private IPs)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return True  # a hostname, not a literal IP


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return title, text


class PageFetcher:
    def __init__(self, settings: Settings, faults: FaultConfig | None = None):
        self.settings = settings
        self.faults = faults or FaultConfig()
        self._cache: dict[str, FetchResult] = {}

    def fetch(self, url: str) -> FetchResult:
        if url in self._cache:
            return self._cache[url]
        result = self._fetch(url)
        self._cache[url] = result
        return result

    def fetch_many(self, urls: list[str], workers: int = 8) -> dict[str, FetchResult]:
        unique = list(dict.fromkeys(urls))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(unique, pool.map(self.fetch, unique)))

    def _fetch(self, url: str) -> FetchResult:
        if self.faults.network_down:
            return FetchResult(url, ok=False, error="Simulated network outage")
        if not is_public_http_url(url):
            return FetchResult(url, ok=False, error="Blocked: not a public http(s) URL")
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.settings.http_timeout_s, allow_redirects=True)
        except requests.Timeout:
            return FetchResult(url, ok=False, error=f"Timeout after {self.settings.http_timeout_s:.0f}s")
        except requests.RequestException as exc:
            return FetchResult(url, ok=False, error=f"Connection error: {type(exc).__name__}")
        if resp.status_code >= 400:
            # 401/403 often mean "bot protection": the page exists but we cannot read it.
            return FetchResult(url, ok=False, status=resp.status_code, error=f"HTTP {resp.status_code}")
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            return FetchResult(url, ok=True, status=resp.status_code, error=f"Non-HTML content ({ctype or 'unknown'})")
        try:
            title, text = html_to_text(resp.text)
        except Exception as exc:  # malformed HTML should not crash collection
            return FetchResult(url, ok=True, status=resp.status_code, error=f"Parse error: {exc}")
        return FetchResult(url, ok=True, status=resp.status_code, title=title,
                           text=text[: self.settings.page_text_chars])
