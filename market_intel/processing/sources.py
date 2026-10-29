"""Registry of every web source used in a run (stable ids S1, S2, ...)."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from market_intel.collectors.fetcher import FetchResult
from market_intel.collectors.search import SearchHit
from market_intel.core.models import Source


def _url_key(url: str) -> str:
    p = urlparse(url)
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/')}?{p.query}"


class SourceRegistry:
    def __init__(self, existing: dict[str, Source] | None = None):
        self.sources: dict[str, Source] = dict(existing or {})
        self._by_key = {_url_key(s.url): s.id for s in self.sources.values()}

    def add_hit(self, hit: SearchHit) -> Source:
        return self.add_url(hit.url, title=hit.title, snippet=hit.snippet, query=hit.query)

    def add_url(self, url: str, *, title: str = "", snippet: str = "", query: str = "") -> Source:
        key = _url_key(url)
        if key in self._by_key:  # duplicate URL -> reuse, keep the longer snippet
            src = self.sources[self._by_key[key]]
            if len(snippet) > len(src.snippet):
                src.snippet = snippet
            src.title = src.title or title
            return src
        sid = f"S{len(self.sources) + 1}"
        src = Source(id=sid, url=url, title=title, snippet=snippet, query=query)
        self.sources[sid] = src
        self._by_key[key] = sid
        return src

    def apply_fetch(self, src: Source, result: FetchResult) -> None:
        src.accessible = result.ok
        src.http_status = result.status
        src.error = result.error
        src.title = src.title or result.title
        src.text = result.text
        src.checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(self, sid: str) -> Source | None:
        return self.sources.get(sid)
