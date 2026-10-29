# AI Market Intelligence MVP (Streamlit)

Enter an industrial product (name, description and/or product-page URL) and a target region. The system
researches the web, extracts the product's features, applications and customer industries, suggests target
markets, identifies real competitors and potential customers, **ranks customers with a transparent
deterministic algorithm**, drafts personalised English sales emails for the top 2, validates every important
claim against its sources, and lets a human send a test email to the single allowed inbox.

The default example is an **industrial Battery Energy Storage System (BESS)**, but nothing in the code is
product-specific: any product works by changing the form input.

---

## 1. Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in OPENAI_API_KEY (or ANTHROPIC_API_KEY), TAVILY_API_KEY, SMTP_*
streamlit run app.py          # opens http://localhost:8501
```

Run the tests (no network or API key needed):

```bash
pytest -q
```

Command-line failure demo (real pipeline, injected faults):

```bash
python scripts/simulate_failure.py                     # all scenarios
python scripts/simulate_failure.py "AI model unavailable"
```

### Dependencies
Python 3.11+ · `streamlit` (UI) · `openai` / `anthropic` (LLM, either one) · `ddgs` (keyless web search) · `requests` +
`beautifulsoup4` (page fetching / text extraction) · `python-dotenv` · `pandas` · `pytest`.

### Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `openai`, `anthropic`, or `auto` (OpenAI if only `OPENAI_API_KEY` is set). |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | **One LLM key is required.** OpenAI model default `gpt-5.5` (any chat model that supports JSON mode works). |
| `ANTHROPIC_API_KEY` / `LLM_MODEL` | Alternative: Claude, default `claude-opus-5-5`. `LLM_EFFORT` (default `medium`) sets reasoning effort for both. |
| `TAVILY_API_KEY` | Optional but **recommended**. Without it the keyless DuckDuckGo search is used, which rate-limits quickly. |
| `SEARCH_PROVIDER` | `auto` (Tavily if a key is set) · `tavily` · `duckduckgo` |
| `SMTP_*` | Test-email delivery, see below. |
| `HTTP_TIMEOUT_S`, `LLM_TIMEOUT_S`, `SMTP_TIMEOUT_S` | Timeouts for every external call. |

### Email service setup (SMTP)
Gmail example (requires 2-step verification and an **App Password**, not your normal password):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=your.account@gmail.com
SMTP_PASSWORD=xxxx xxxx xxxx xxxx     # App Password
SMTP_FROM=your.account@gmail.com
```

Any other SMTP provider works the same way (`SMTP_SECURITY=ssl` with port 465 for implicit TLS).

**Safety rules, enforced in code (`market_intel/messaging/email.py`), not just in the UI:**
1. The only recipient is `behta.technicalteam@gmail.com` (constant `ALLOWED_TEST_RECIPIENT`, deliberately not
   configurable). Any other address is rejected; the prospect's real address is never used.
2. Nothing is sent without human approval: the user reviews (and may edit) the text, ticks the approval box, and
   presses **Send Test Email**. `send_test_email(..., approved=False)` refuses to send.
3. Every attempt, success or failure, is shown in the UI and appended to `runs/email_log.jsonl`.

---

## 2. Using the UI

1. Fill in product name / description / URL and target region → **🚀 Start Analysis**.
2. A live status panel shows each step (✅ ok · ⚠️ warning · ❌ failed · ⏭️ skipped) with timings.
3. Tabs:
   - **Overview**: run status (COMPLETE / PARTIAL / FAILED), process log, counts, de-duplication log, and
     downloads (Markdown report + raw JSON).
   - **Product**: features, applications and industries, each with source links and a verification badge.
   - **Markets & Competitors**: ≥2 markets with rationale and sources; ≥3 competitors (name, country, website,
     activity, reason).
   - **Customers & Scores**: ranked table (score, coverage, per-criterion values), and for each customer the
     reasons, evidence, a per-criterion points table and the exact calculation (`100 × (w·v + …) = score`).
     **Weights are sliders in the sidebar**; changing them re-ranks immediately.
   - **Sales Emails**: the two AI-personalised messages, editable, with approval and **Send Test Email**.
   - **Sources & Validation**: every source (official / independent / unknown, accessible, HTTP status, error,
     time checked) plus a claim-verification table with a filter for claims lacking sufficient evidence.
   - **Failure handling**: scenarios, a one-click outage demo, and the failure and email logs.
4. Sidebar → **Failure simulation** injects an outage into the next analysis or email send.
5. Sidebar → **Previous runs** reloads any saved run from `runs/`.

---

## 3. Architecture

```
app.py                      Streamlit UI only (no business logic)
market_intel/
  core/        config.py    settings from .env, the fixed test recipient
               models.py    plain dataclasses shared by every stage (no UI / LLM dependency)
               errors.py    typed external-service errors with user-readable messages
               faults.py    fault-injection flags for the failure simulation
  collectors/  search.py    web search (Tavily or DuckDuckGo) behind one interface
               fetcher.py   page fetch + text extraction + accessibility check (never raises)
  processing/  sources.py   source registry: stable ids S1, S2… and URL de-duplication
               cleaning.py  normalisation, "N/A"→unknown, company de-duplication/merging, competitor overlap
               validation.py source classification + claim verification (deterministic, no LLM)
  ai/          llm.py       LLM client (OpenAI or Claude): JSON-only answers, every SDK error → typed error
               prompts.py   product-agnostic prompt templates
               extraction.py LLM JSON → typed, cleaned objects (LLM output treated as untrusted)
  scoring/     features.py  Company → criterion values (rules only; unknown stays None)
               ranking.py   the ranking algorithm (pure Python, no LLM)
  messaging/   sales.py     sales messages (AI, with a labelled template fallback)
               email.py     SMTP sending with the recipient whitelist and approval gate
  pipeline.py  orchestration, per-step error isolation, final status
  report.py    Markdown report
  storage.py   run persistence (JSON) and failure log
tests/         ranking, processing/validation, failure scenarios, end-to-end pipeline with fakes
```

### Pipeline
`product research → markets → competitors → customers → validation → scoring → messages`

Each research step does three things. (1) It runs a few generated search queries. (2) It fetches the top pages,
which also serves as the accessibility check. (3) It sends **numbered sources** (`[S1] … [Sn]`) to the LLM, asking
for JSON in which every claim cites source ids. The AI is used for what it is good at: reading unstructured
pages and extracting entities. It is **not** used for anything that has to be checkable: validation, scoring,
de-duplication and the final status are all deterministic code.

### Key technical decisions

| Decision | Why |
|---|---|
| **AI extracts, code decides.** | Ranking and verification must be testable and reproducible; LLM output varies between runs. |
| **Websites checked, not trusted.** | A website comes from a cited source, from the model's knowledge, or from a `"<name> official website"` search. A website from the model's knowledge or from search is accepted only if its domain matches the company name (`acwapower.com` ↔ ACWA Power), and every website is then fetched to check reachability. Social, aggregator and news domains are never accepted. |
| **Fields from general knowledge are flagged.** | A competitor's headquarters country may come from the model's knowledge when the sources are silent; the UI and the report then mark it *⚠️ general knowledge, not in sources*. |
| **Numbered sources + mandatory citations.** | Every claim can be traced to a URL. Invented source ids are detected and discarded. |
| **Claim verification by lexical relevance**, not by asking the LLM "is this true?". | Asking the same model to grade itself is circular. The code checks that the cited page is reachable, has a credible type, and actually contains the claim's content words; for claims about a company, the company's name must appear. |
| **Three-level status** (verified / probable / unverified). | Separates confirmed facts from likely or unsupported ones, as the brief requires. |
| **Unknown = `None`, never a default number.** | Missing data cannot silently earn points. |
| **Typed errors + per-step isolation.** | One failure never kills the run; the UI always gets a readable message. |
| **Fault injection at the client layer.** | The demo exercises the *real* error path (not a mocked screen). |
| **Status derived from output** (`complete` only if every step succeeded and the minimums are met: ≥2 markets, ≥3 competitors, ≥5 customers, 2 AI messages). | A degraded run can never be shown as a complete analysis. |
| **Dataclasses, no framework.** | Small, readable, easy to test; each stage can be swapped (e.g. another search API or LLM). |
| **Two LLM providers behind one `LLMClient`.** | OpenAI and Claude are interchangeable via `LLM_PROVIDER`; both map their SDK errors to the same typed errors, so the pipeline doesn't know which one runs. |
| **Product-agnostic prompts and queries.** | Another product only needs different form input. |

---

## 4. Customer ranking algorithm (`scoring/ranking.py`)

```
final_score = 100 × Σ wᵢ·vᵢ / Σ wᵢ          vᵢ ∈ [0,1] or None (unknown → contributes 0)
coverage    = Σ wᵢ over known criteria / Σ wᵢ
```

| Criterion | How the value is computed (`scoring/features.py`) | Unknown when |
|---|---|---|
| Industry fit | token overlap between the customer's short industry label (activity text only as fallback) and the product's extracted industries/applications; generic words such as *infrastructure*, *developer* and *services* are ignored | industry and activity both unknown |
| Geographic fit | 1.0 = country is a target market or inside the target region · 0.5 = same wider region · 0 = outside | country unknown |
| Evidence of need | Σ(verified = 1, probable = 0.5, unverified = 0) / 2, capped at 1, counting **only evidence that relates to the product** (shares words with the product name, buyer keywords or applications). "Listed among manufacturing startups" earns 0. | no evidence items |
| Data reliability | company record verified = 1 (website reachable **and** named in a cited source), probable = 0.5, unverified = 0 | — |

- **Adjustable weights:** sidebar sliders; they must be ≥ 0 with at least one > 0, and are normalised to sum to 1.
- **Unknown values:** they contribute exactly 0 and are listed separately. `coverage` shows whether a low score
  comes from poor fit or from missing data. Unverified evidence earns nothing.
- **Deterministic ordering:** score ↓ → coverage ↓ → number of supported evidence items ↓ → name
  (case-insensitive) ↑ → id ↑. The order of equal scores never depends on input order.
- **Auditable:** each customer shows its value, weight, points and reason per criterion, plus the full formula.
  The score is summed from unrounded points, so exact ties stay exact.

### Tests (`pytest -q`, 30 tests, no network)
- **Weight changes** (`test_ranking.py`): shifting weight from industry to geography reverses the order of an
  industry-strong vs a geo-strong customer; raising a weight raises the score of a customer strong in that
  criterion; a weight of 0 removes a criterion's influence; scaling all weights changes nothing; invalid weights
  are rejected.
- **Incomplete data:** unknown criteria earn 0 points and lower coverage; an all-unknown customer scores 0 and
  ranks below one with minimal evidence; feature extraction does not invent values for an empty company record;
  unverified evidence earns 0.
- **Equal scores:** shuffling the input 10 times always gives the same order; ties break on coverage, then
  evidence count, then name.
- **Processing / validation** (`test_processing.py`): placeholder cleaning, duplicate merging, competitor/customer
  overlap, source classification, invented or irrelevant sources → unverified, unreachable source → not strong
  evidence.
- **Failure scenarios** (`test_failures.py`): AI outage mid-run keeps earlier data and gives `partial`; simulated
  internet outage → `failed` with a message, no crash; invalid or empty LLM answers rejected; email recipient
  whitelist; approval gate; simulated and real (unreachable host) SMTP failure.
- **End-to-end** (`test_pipeline.py`): a full run with fake search, web and LLM reaches `complete`, with duplicates
  merged, the competitor removed from customers, `N/A` turned into unknown, ranks assigned, and messages for the
  top 2.

---

## 5. Source validation

For every important claim (product features, applications and industries, market rationales, competitor
reasons, customer fit reasons and evidence of need), the system checks:
1. **Source recorded:** the claim cites source ids; ids that don't exist are flagged and ignored.
2. **Accessible at collection time:** each URL is fetched; HTTP status, error and timestamp are stored.
3. **Relevant to the claim:** the share of the claim's content words found in the source text. For claims about a
   company, the company's name must appear, or relevance is capped.
4. **Source type:** *official* (the manufacturer's or the company's own domain), *independent* (news, industry
   media, research, government or education domains from a curated list), or *unknown*.
5. **Verdict:** *verified* = relevant text in an accessible official or independent source, or ≥2 supporting
   sources · *probable* = one weaker source · *unverified* = no usable evidence. The UI can filter to the claims
   that lack sufficient evidence.

---

## 6. External-service failure handling

| Scenario | Behaviour |
|---|---|
| No internet / source unreachable | Each unreachable page is recorded as such (snippet used if available). If search itself is down: the step fails with a readable message, later search steps are skipped, run = **FAILED**. |
| API error / timeout | Typed `ServiceTimeoutError` / `SearchError`; single failed queries are tolerated, and the step only fails if all fail. |
| Empty / invalid response | Empty search → clear "rate-limited / no results" message. A malformed LLM answer (HTML, broken JSON, missing fields, truncation, refusal) is rejected as `InvalidResponseError` and never used as data. |
| AI model unavailable | Missing or invalid key, connection error, rate limit and timeout map to `LLMUnavailableError`. Sources already collected are kept; later AI steps are **skipped** instead of waiting again; run = **PARTIAL** or **FAILED**. If only message generation fails, a template draft is shown, labelled *not AI-personalised*. |
| SMTP failure | Email not sent; the reason (auth, connection, timeout, simulated) is shown and logged. |

Previously collected data is preserved at three levels: within a run, earlier steps' results are kept; every
run is saved to `runs/run-*.json` and can be reloaded; and if a new run fails, the UI keeps showing the last
valid result together with the failure report.

**Demonstration:** sidebar → *Failure simulation* (5 scenarios), the **Quick demo** button in the *Failure
handling* tab, `scripts/simulate_failure.py`, and the automated tests. Results are logged to
`runs/failure_log.jsonl`.

---

## 7. Limitations (MVP)

- **Search quality and rate limits:** keyless DuckDuckGo search throttles after a few dozen queries per IP
  ("No results found"). Use `TAVILY_API_KEY` for reliable runs. Results depend on what the search engine returns.
- **The verification is lexical.** It confirms that a source *mentions* the claim's content, not that the claim is
  true. Paraphrased claims can score lower; pages behind bot protection or JavaScript rendering are recorded as
  not accessible, and their snippets are used.
- **Company facts come from the LLM reading the sources.** A website the LLM supplies is checked for
  reachability, but not proven to belong to that company.
- **The independent-domain list is curated and incomplete;** sources not on it are labelled *unknown*, which is
  conservative.
- **The region map** used for geographic fit covers major regions only; unknown country names get no partial credit.
- **No contact persons or email addresses are collected.** Emails go only to the test inbox, by design.
- **A run takes roughly 2–5 minutes** (about 20 searches, about 60 page fetches, 5–7 LLM calls). It is synchronous; there is
  no job queue.
- **Cost:** each run makes 5–7 LLM calls with roughly 10k input tokens each.
