"""External-service failure scenarios: the app must degrade, never crash or over-claim."""
import json

import pytest

from market_intel.ai.llm import parse_json_object
from market_intel.collectors.fetcher import FetchResult
from market_intel.collectors.search import SearchHit
from market_intel.core.config import ALLOWED_TEST_RECIPIENT, Settings
from market_intel.core.errors import InvalidResponseError, LLMUnavailableError
from market_intel.core.faults import FaultConfig
from market_intel.core.models import ProductInput, SalesMessage
from market_intel.messaging.email import send_test_email
from market_intel.pipeline import Pipeline

BESS = ProductInput(name="Industrial Battery Energy Storage System (BESS)", target_region="Middle East")


@pytest.fixture
def settings(tmp_path):
    return Settings(runs_dir=str(tmp_path), smtp_host="smtp.example.com", smtp_from="bot@example.com")


class FakeSearch:
    def __init__(self, fail=None):
        self.fail = fail

    def search_many(self, queries, max_results=None):
        if self.fail:
            raise self.fail
        return [SearchHit(f"Result {i} for {q}", f"https://news.example.org/{abs(hash(q)) % 999}/{i}",
                          f"snippet about {q}", q) for q in queries for i in range(2)], []


class FakeFetcher:
    def fetch_many(self, urls, workers=8):
        return {u: FetchResult(u, ok=True, status=200, title="t", text=f"page text {u}") for u in urls}


class FakeLLM:
    """Answers the product step, then fails like a dropped API connection."""

    def __init__(self, fail_after=0, error=None):
        self.calls, self.fail_after = 0, fail_after
        self.error = error or LLMUnavailableError("connection refused")

    def complete_json(self, prompt, max_tokens=16000):
        self.calls += 1
        if self.calls > self.fail_after:
            raise self.error
        return {"product_name": "BESS", "summary": "Battery storage.",
                "features": [{"text": "Lithium iron phosphate cells", "sources": ["S1"]}],
                "applications": [{"text": "Peak shaving", "sources": ["S1"]}],
                "industries": [{"text": "Data centers", "sources": ["S1"]}],
                "search_keywords": ["battery energy storage"]}


def test_ai_outage_mid_run_keeps_collected_data_and_reports_partial(settings):
    llm = FakeLLM(fail_after=1)
    result = Pipeline(settings, search=FakeSearch(), fetcher=FakeFetcher(), llm=llm).run(BESS)

    assert result.status == "partial"                 # never shown as a complete analysis
    assert result.product is not None                 # data from before the outage is kept
    assert result.sources                             # collected sources are kept
    statuses = {s.name.split(".")[0]: s.status for s in result.steps}
    assert statuses["1"] == "ok" and statuses["2"] == "failed"
    assert statuses["3"] == "skipped" and statuses["4"] == "skipped"   # no repeated waiting on a dead service
    assert llm.calls == 2
    assert any("AI model" in e for e in result.errors)


def test_simulated_network_down_fails_cleanly_with_message(settings):
    result = Pipeline(settings, FaultConfig(network_down=True)).run(BESS)
    assert result.status == "failed"
    assert result.errors and all(isinstance(e, str) for e in result.errors)
    assert any("internet" in e.lower() or "network" in e.lower() for e in result.errors)
    assert not result.customers and not result.messages


def test_simulated_ai_unavailable_still_collects_sources(settings):
    result = Pipeline(settings, FaultConfig(llm_unavailable=True), search=FakeSearch(), fetcher=FakeFetcher()).run(BESS)
    assert result.status == "failed"   # sources only, no analysis
    assert result.sources
    assert any("AI model" in e for e in result.errors)


def test_invalid_llm_answers_are_rejected():
    for bad in ["", "   ", "<html>502 Bad Gateway</html>", "{not json", "[1, 2]"]:
        with pytest.raises(InvalidResponseError):
            parse_json_object(bad)
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_empty_input_is_rejected_without_calling_services(settings):
    result = Pipeline(settings, search=FakeSearch(fail=AssertionError()), llm=FakeLLM()).run(ProductInput(name=""))
    assert result.status == "failed" and result.errors


# --- email ---------------------------------------------------------------------------

MSG = SalesMessage(company_name="ACME", subject="Hello", body="Dear team, " * 20)


def test_email_requires_whitelisted_recipient(settings):
    res = send_test_email(settings, MSG, "customer@acme.com", approved=True)
    assert not res.ok and "not allowed" in res.detail


def test_email_requires_human_approval(settings):
    res = send_test_email(settings, MSG, ALLOWED_TEST_RECIPIENT, approved=False)
    assert not res.ok and "approval" in res.detail


def test_simulated_smtp_failure_is_reported_and_logged(settings, tmp_path):
    res = send_test_email(settings, MSG, ALLOWED_TEST_RECIPIENT, approved=True, faults=FaultConfig(smtp_failure=True))
    assert not res.ok and "Simulated" in res.detail
    log = [json.loads(line) for line in (tmp_path / "email_log.jsonl").read_text().splitlines()]
    assert log[-1]["ok"] is False


def test_unreachable_smtp_server_is_a_controlled_failure(settings):
    s = Settings(runs_dir=settings.runs_dir, smtp_host="127.0.0.1", smtp_port=1, smtp_from="a@b.c", smtp_timeout_s=2)
    res = send_test_email(s, MSG, ALLOWED_TEST_RECIPIENT, approved=True)
    assert not res.ok and "Email (SMTP)" in res.detail
