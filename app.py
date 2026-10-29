"""Streamlit UI for the AI market-intelligence MVP.

Run:  streamlit run app.py
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from market_intel.ai.llm import LLMClient
from market_intel.core.config import ALLOWED_TEST_RECIPIENT, Settings
from market_intel.core.faults import SCENARIOS
from market_intel.core.models import AnalysisResult, Claim, ProductInput, StepLog
from market_intel.messaging.email import send_test_email
from market_intel.messaging.sales import generate_messages
from market_intel.pipeline import N_MESSAGES, STEPS, Pipeline, rank
from market_intel.report import to_markdown
from market_intel.scoring.ranking import CRITERIA, CRITERION_LABELS, DEFAULT_WEIGHTS, normalize_weights
from market_intel.storage import append_failure_log, list_runs, load_run, save_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

st.set_page_config(page_title="Market Intelligence MVP", page_icon="📊", layout="wide")

settings = Settings()
STATUS_ICON = {"pending": "⏳", "running": "🔄", "ok": "✅", "warning": "⚠️", "failed": "❌", "skipped": "⏭️"}
VERIF_ICON = {"verified": "✅ verified", "probable": "🟡 probable", "unverified": "⚠️ unverified"}
SOURCE_ICON = {"official": "🏢 official", "independent": "📰 independent", "unknown": "❔ unknown"}

DEFAULTS = {
    "name": "Industrial Battery Energy Storage System (BESS)",
    "description": ("Containerised lithium-iron-phosphate battery energy storage for industrial and utility use: "
                    "peak shaving, renewable integration, backup power and grid services (0.5–5 MWh per unit)."),
    "url": "",
    "region": "Middle East (GCC)",
}


# --- sidebar -------------------------------------------------------------------------

def sidebar() -> tuple[str, dict[str, float]]:
    st.sidebar.header("⚙️ Configuration")
    llm_ok = settings.llm_key_present
    st.sidebar.markdown(
        f"- AI model: `{settings.llm_provider}` / `{settings.llm_model}` "
        f"{'✅' if llm_ok else '⚠️ no ' + ("OPENAI_API_KEY" if settings.llm_provider == "openai" else "ANTHROPIC_API_KEY")}\n"
        f"- Web search: `{'tavily' if settings.tavily_api_key and settings.search_provider == 'auto' else settings.search_provider}`\n"
        f"- SMTP: {'✅ ' + settings.smtp_host if settings.smtp_configured else '⚠️ not configured'}\n"
        f"- Test inbox: `{ALLOWED_TEST_RECIPIENT}`"
    )

    st.sidebar.header("🧪 Failure simulation")
    scenario = st.sidebar.selectbox(
        "External-service fault", list(SCENARIOS),
        help="Injects the same typed error a real outage produces, to demonstrate controlled failure handling.")
    if SCENARIOS[scenario].active:
        st.sidebar.warning(f"Simulation active: {scenario}")

    st.sidebar.header("⚖️ Ranking weights")
    st.sidebar.caption("Changing a weight re-ranks customers instantly (deterministic, no AI call).")
    raw = {c: st.sidebar.slider(CRITERION_LABELS[c], 0, 100, int(DEFAULT_WEIGHTS[c] * 100), 5, key=f"w_{c}")
           for c in CRITERIA}
    try:
        weights = normalize_weights(raw)
    except ValueError as exc:
        st.sidebar.error(f"{exc} – using default weights.")
        weights = normalize_weights(DEFAULT_WEIGHTS)
    st.sidebar.caption("Normalised: " + ", ".join(f"{CRITERION_LABELS[c]} {w:.0%}" for c, w in weights.items()))

    st.sidebar.header("🗂️ Previous runs")
    runs = list_runs(settings.runs_dir)
    if runs:
        chosen = st.sidebar.selectbox("Load a saved run", ["—"] + [r.name for r in runs])
        if chosen != "—" and st.sidebar.button("Load run"):
            loaded = load_run(Path(settings.runs_dir) / chosen)
            if loaded:
                st.session_state.result = loaded
                st.session_state.email_results = {}
            else:
                st.sidebar.error("That run file could not be read.")
    else:
        st.sidebar.caption("No saved runs yet.")
    return scenario, weights


# --- run -----------------------------------------------------------------------------

def run_analysis(product: ProductInput, scenario: str, weights: dict[str, float]) -> None:
    faults = SCENARIOS[scenario]
    with st.status("Running analysis…", expanded=True) as status:
        progress = st.progress(0.0)
        table = st.empty()

        def on_progress(step: StepLog, result: AnalysisResult) -> None:
            done = sum(s.status not in ("pending", "running") for s in result.steps)
            progress.progress(done / len(STEPS), text=f"{step.name} – {step.status}")
            table.markdown("\n".join(
                f"{STATUS_ICON[s.status]} **{s.name}** {f'({s.duration_s}s)' if s.duration_s else ''} "
                f"{'– ' + s.message if s.message and s.status != 'ok' else ''}" for s in result.steps))

        try:
            result = Pipeline(settings, faults, progress=on_progress).run(product, weights)
        except Exception as exc:  # the pipeline already guards each step; this is a final safety net
            logging.exception("pipeline crashed")
            st.session_state.run_error = f"Unexpected error: {type(exc).__name__}: {exc}"
            status.update(label="Analysis crashed", state="error")
            return

        label = {"complete": "Analysis complete", "partial": "Analysis finished with problems (PARTIAL)",
                 "failed": "Analysis failed"}[result.status]
        status.update(label=label, state={"complete": "complete", "partial": "error", "failed": "error"}[result.status],
                      expanded=result.status != "complete")

    save_run(result, settings.runs_dir)
    if result.errors:
        append_failure_log(settings.runs_dir, {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": result.run_id,
            "scenario": result.fault_scenario, "status": result.status, "errors": result.errors})

    previous = st.session_state.get("result")
    if result.status == "failed" and previous and previous.status != "failed":
        # Keep the last good analysis visible; show the failed one only as an error report.
        st.session_state.failed_run = result
    else:
        st.session_state.result = result
        st.session_state.failed_run = None
    st.session_state.email_results = {}
    st.session_state.run_error = None


# --- result views --------------------------------------------------------------------

def fmt_value(v: float | None) -> str:
    """Criterion value as text, so 'unknown' and numbers can share a table column."""
    return "unknown" if v is None else f"{v:.2f}"


def src_links(ids: list[str], result: AnalysisResult) -> str:
    links = [f"[{i}]({result.sources[i].url})" for i in ids if i in result.sources]
    return ", ".join(links) or "no source"


def claim_line(c: Claim, result: AnalysisResult) -> str:
    return f"{c.text}  \n<small>{VERIF_ICON[c.status.value]} · {src_links(c.source_ids, result)}" \
           f"{' · ' + c.note if c.note else ''}</small>"


def show_status_banner(r: AnalysisResult) -> None:
    if r.status == "complete":
        st.success(f"✅ Complete analysis · run `{r.run_id}` · {r.created_at}")
    elif r.status == "partial":
        st.warning(f"⚠️ PARTIAL analysis – some steps failed or minimums were not met. Do not treat this as a "
                   f"complete market analysis. · run `{r.run_id}`")
    else:
        st.error(f"❌ Analysis FAILED · run `{r.run_id}`")
    if r.fault_scenario and SCENARIOS.get(r.fault_scenario, None) and SCENARIOS[r.fault_scenario].active:
        st.info(f"🧪 This run used the failure simulation: **{r.fault_scenario}**")
    for e in r.errors:
        st.error(e)
    for w in r.warnings:
        st.warning(w)


def tab_overview(r: AnalysisResult) -> None:
    st.subheader("Process log")
    st.dataframe(pd.DataFrame([{"Step": s.name, "Status": f"{STATUS_ICON[s.status]} {s.status}",
                                "Duration (s)": s.duration_s, "Details": s.message} for s in r.steps]),
                 hide_index=True, width="stretch")
    cols = st.columns(5)
    cols[0].metric("Markets", len(r.markets))
    cols[1].metric("Competitors", len(r.competitors))
    cols[2].metric("Customers", len(r.customers))
    cols[3].metric("Sources", len(r.sources))
    cols[4].metric("Accessible sources", sum(bool(s.accessible) for s in r.sources.values()))
    if r.duplicates_removed:
        with st.expander(f"De-duplication log ({len(r.duplicates_removed)})"):
            st.markdown("\n".join(f"- {d}" for d in r.duplicates_removed))


def tab_product(r: AnalysisResult) -> None:
    p = r.product
    if not p:
        st.info("No product profile – the product research step did not complete.")
        return
    st.subheader(p.name)
    st.write(p.summary)
    cols = st.columns(3)
    for col, (title, claims) in zip(cols, (("Features", p.features), ("Applications", p.applications),
                                           ("Customer industries", p.industries))):
        col.markdown(f"**{title}**")
        for c in claims:
            col.markdown("- " + claim_line(c, r), unsafe_allow_html=True)
    if p.keywords:
        st.caption("Search keywords: " + ", ".join(p.keywords))


def company_table(companies, role: str) -> pd.DataFrame:
    rows = []
    for c in companies:
        row = {"Company": c.name, "Country": c.country or "UNKNOWN", "Website": c.website,
               "Activity": c.activity or "UNKNOWN", "Record status": VERIF_ICON[c.status.value],
               "Website reachable": {True: "yes", False: "no", None: "—"}[c.website_reachable],
               "Missing data": ", ".join(c.missing_fields) or "—"}
        if role == "customer":
            row["Industry"] = c.industry or "UNKNOWN"
        rows.append(row)
    return pd.DataFrame(rows)


def tab_markets(r: AnalysisResult) -> None:
    st.subheader("🌍 Suggested target markets")
    if not r.markets:
        st.info("No markets identified.")
    for m in r.markets:
        with st.container(border=True):
            st.markdown(f"**{m.name}** · {VERIF_ICON[m.rationale.status.value]}")
            st.write(m.rationale.text or "_no rationale given_")
            st.caption(f"Sources: {src_links(m.rationale.source_ids, r)}"
                       f"{' · ' + m.rationale.note if m.rationale.note else ''}")

    st.subheader("🏭 Competitors")
    if not r.competitors:
        st.info("No competitors identified.")
        return
    st.dataframe(company_table(r.competitors, "competitor"), hide_index=True, width="stretch",
                 column_config={"Website": st.column_config.LinkColumn()})
    for c in r.competitors:
        if c.reason:
            st.markdown(f"- **{c.name}** – " + claim_line(c.reason, r), unsafe_allow_html=True)


def tab_customers(r: AnalysisResult, weights: dict[str, float]) -> None:
    ranking = rank(r, weights)
    if not ranking:
        st.info("No ranked customers – the customer discovery or scoring step did not complete.")
        return
    by_name = {c.name: c for c in r.customers}
    st.subheader("🎯 Potential customers – ranked")
    st.caption("Score = 100 × Σ(weightᵢ × valueᵢ). Unknown criteria contribute 0 points; *coverage* shows how much "
               "of the weight is backed by real data. Ties: coverage ↓, evidence count ↓, name ↑.")
    rows = []
    for s in ranking:
        c = by_name[s.name]
        row = {"Rank": s.rank, "Company": c.name, "Score": s.score, "Coverage": f"{s.coverage:.0%}",
               "Country": c.country or "UNKNOWN", "Industry": c.industry or "UNKNOWN", "Website": c.website,
               "Record status": VERIF_ICON[c.status.value]}
        row |= {CRITERION_LABELS[b.criterion]: fmt_value(b.value) for b in s.breakdown}
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
                                "Website": st.column_config.LinkColumn()})

    st.subheader("Why this ranking?")
    for s in ranking:
        c = by_name[s.name]
        with st.expander(f"#{s.rank} {c.name} — {s.score:.1f}/100 (coverage {s.coverage:.0%})"):
            if c.reason:
                st.markdown("**Fit:** " + claim_line(c.reason, r), unsafe_allow_html=True)
            if c.activity:
                st.markdown(f"**Activity:** {c.activity}")
            st.markdown("**Evidence of need**")
            if c.evidence:
                for e in c.evidence:
                    st.markdown("- " + claim_line(e, r), unsafe_allow_html=True)
            else:
                st.caption("No evidence found – this criterion is unknown and earns 0 points.")
            st.dataframe(pd.DataFrame([{
                "Criterion": CRITERION_LABELS[b.criterion],
                "Value (0–1)": fmt_value(b.value),
                "Weight": round(b.weight, 3), "Points": b.points, "Reason": b.reason} for b in s.breakdown]),
                hide_index=True, width="stretch")
            st.code(s.explain(), language=None)
            if c.missing_fields:
                st.caption("Missing data: " + ", ".join(c.missing_fields))
            if c.merged_from:
                st.caption("Merged duplicates: " + ", ".join(c.merged_from))


def tab_messages(r: AnalysisResult, weights: dict[str, float], scenario: str) -> None:
    st.subheader("✉️ Personalised sales messages")
    ranking = rank(r, weights)
    current_top = [s.name for s in ranking[:N_MESSAGES]]
    have = [m.company_name for m in r.messages]
    if ranking and current_top != have:
        st.info(f"With the current weights the top {N_MESSAGES} are **{', '.join(current_top)}**, "
                f"but messages exist for **{', '.join(have) or 'nobody'}**.")
        if st.button("Regenerate messages for the current top customers"):
            by_name = {c.name: c for c in r.customers}
            with st.spinner("Generating messages…"):
                r.messages, errors = generate_messages(LLMClient(settings, SCENARIOS[scenario]),
                                                       [by_name[n] for n in current_top], r.product)
            for e in errors:
                st.error(e)
            save_run(r, settings.runs_dir)
            st.rerun()
    if not r.messages:
        st.info("No messages were generated.")
        return

    st.caption(f"Emails are only ever sent to **{ALLOWED_TEST_RECIPIENT}** and only after you approve them. "
               "The prospect's real address is never used.")
    results = st.session_state.setdefault("email_results", {})
    for i, m in enumerate(r.messages):
        with st.container(border=True):
            tag = "🤖 AI-generated" if m.generated_by == "ai" else "📄 TEMPLATE FALLBACK – not AI-personalised"
            st.markdown(f"### To: {m.company_name}  \n<small>{tag}</small>", unsafe_allow_html=True)
            if m.error:
                st.warning(m.error)
            subject = st.text_input("Subject", m.subject, key=f"subj_{r.run_id}_{i}")
            body = st.text_area("Body (you may edit before sending)", m.body, height=280, key=f"body_{r.run_id}_{i}")
            if m.facts_used:
                with st.expander("Facts the message is based on"):
                    st.markdown("\n".join(f"- {f}" for f in m.facts_used))
            st.text_input("Recipient (fixed)", ALLOWED_TEST_RECIPIENT, disabled=True, key=f"to_{r.run_id}_{i}")
            approved = st.checkbox("I have reviewed this email and approve sending it to the test inbox",
                                   key=f"ok_{r.run_id}_{i}")
            if st.button("📤 Send Test Email", key=f"send_{r.run_id}_{i}", disabled=not approved, type="primary"):
                draft = type(m)(**{**asdict(m), "subject": subject, "body": body})
                with st.spinner("Sending…"):
                    results[i] = send_test_email(settings, draft, ALLOWED_TEST_RECIPIENT, approved=approved,
                                                 faults=SCENARIOS[scenario])
            if res := results.get(i):
                (st.success if res.ok else st.error)(f"{'✅ Sent' if res.ok else '❌ Not sent'} ({res.sent_at}): {res.detail}")


def tab_sources(r: AnalysisResult) -> None:
    st.subheader("🔗 Sources & validation")
    if not r.sources:
        st.info("No sources collected.")
        return
    srcs = list(r.sources.values())
    cols = st.columns(4)
    cols[0].metric("Sources", len(srcs))
    cols[1].metric("Accessible", sum(bool(s.accessible) for s in srcs))
    cols[2].metric("Official", sum(s.source_type.value == "official" for s in srcs))
    cols[3].metric("Independent", sum(s.source_type.value == "independent" for s in srcs))
    st.dataframe(pd.DataFrame([{
        "ID": s.id, "Type": SOURCE_ICON[s.source_type.value],
        "Accessible": {True: "✅ yes", False: "❌ no", None: "— not checked"}[s.accessible],
        "HTTP": s.http_status, "Error": s.error or "", "Title": s.title[:90], "URL": s.url,
        "Checked at": s.checked_at, "Found by": s.query} for s in srcs]),
        hide_index=True, width="stretch", column_config={"URL": st.column_config.LinkColumn()})

    claims: list[tuple[str, Claim]] = []
    if r.product:
        claims += [("Product feature", c) for c in r.product.features]
        claims += [("Product application", c) for c in r.product.applications]
        claims += [("Product industry", c) for c in r.product.industries]
    claims += [(f"Market: {m.name}", m.rationale) for m in r.markets]
    for c in r.competitors + r.customers:
        if c.reason:
            claims.append((f"{c.role.title()}: {c.name}", c.reason))
        claims += [(f"Need evidence: {c.name}", e) for e in c.evidence]
    if claims:
        st.subheader("Claim verification")
        st.caption("verified = relevant text in an accessible official/independent source (or ≥2 supporting sources); "
                   "probable = one weaker source; unverified = no usable evidence – do not rely on it.")
        df = pd.DataFrame([{"About": who, "Claim": c.text, "Status": VERIF_ICON[c.status.value],
                            "Relevance": c.relevance, "Sources": ", ".join(c.source_ids), "Note": c.note}
                           for who, c in claims])
        only_unverified = st.toggle("Show only claims without sufficient evidence")
        if only_unverified:
            df = df[df["Status"] != VERIF_ICON["verified"]]
        st.dataframe(df, hide_index=True, width="stretch")


def tab_failures(scenario: str) -> None:
    st.subheader("🧪 External-service failure handling")
    st.markdown(
        "Pick a scenario in the sidebar and press **Start Analysis** (or **Send Test Email**). The fault is injected "
        "at the client layer, so the real error path runs: typed error → step marked failed/skipped → data from "
        "earlier steps kept → status **partial/failed** → readable message here.")
    st.markdown("| Scenario | Expected behaviour |\n|---|---|\n"
                "| Internet down | search & pages fail → run FAILED, clear message, previous good result kept |\n"
                "| Search timeout | same as above, timeout message |\n"
                "| AI model unavailable | sources still collected; AI steps skipped; run FAILED/PARTIAL |\n"
                "| AI invalid response | malformed answer rejected, not shown as data |\n"
                "| SMTP failure | email not sent, error shown and logged |")
    if st.button("▶️ Quick demo: run the pipeline with the internet 'down'"):
        demo = Pipeline(settings, SCENARIOS["Internet down (search + web pages unreachable)"]).run(
            ProductInput(name=DEFAULTS["name"], target_region=DEFAULTS["region"]))
        append_failure_log(settings.runs_dir, {"at": demo.created_at, "run_id": demo.run_id,
                                               "scenario": demo.fault_scenario, "status": demo.status,
                                               "errors": demo.errors})
        st.error(f"Run status: {demo.status.upper()} – the app did not crash.")
        st.dataframe(pd.DataFrame([{"Step": s.name, "Status": s.status, "Message": s.message} for s in demo.steps]),
                     hide_index=True, width="stretch")

    for name, title in (("failure_log.jsonl", "Failure log"), ("email_log.jsonl", "Email send log")):
        path = Path(settings.runs_dir) / name
        if path.exists():
            st.markdown(f"**{title}** (`{path}`)")
            lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()[-20:] if x.strip()]
            st.dataframe(pd.DataFrame(lines[::-1]), hide_index=True, width="stretch")


# --- page ----------------------------------------------------------------------------

def main() -> None:
    scenario, weights = sidebar()
    st.title("📊 AI Market Intelligence for Industrial Products")
    st.caption("Enter a product → the system researches the web, finds target markets, competitors and potential "
               "customers, ranks them with a transparent algorithm and drafts personalised sales emails.")

    with st.form("input"):
        c1, c2 = st.columns([2, 1])
        name = c1.text_input("Product name", DEFAULTS["name"])
        region = c2.text_input("Target country / region", DEFAULTS["region"])
        description = st.text_area("Product description", DEFAULTS["description"], height=90)
        url = st.text_input("Product page URL (manufacturer website, optional)", DEFAULTS["url"],
                            placeholder="https://…")
        start = st.form_submit_button("🚀 Start Analysis", type="primary")

    if start:
        if not (name.strip() or description.strip() or url.strip()):
            st.error("Please enter at least a product name, a description or a product URL.")
        else:
            run_analysis(ProductInput(name.strip(), description.strip(), url.strip(), region.strip()),
                         scenario, weights)

    if err := st.session_state.get("run_error"):
        st.error(err)
    if failed := st.session_state.get("failed_run"):
        with st.container(border=True):
            st.error("❌ The latest analysis failed. The previous valid result is still shown below.")
            for e in failed.errors:
                st.markdown(f"- {e}")

    result: AnalysisResult | None = st.session_state.get("result")
    tabs = st.tabs(["Overview", "Product", "Markets & Competitors", "Customers & Scores", "Sales Emails",
                    "Sources & Validation", "Failure handling"])
    with tabs[6]:
        tab_failures(scenario)
    if not result:
        with tabs[0]:
            st.info("No analysis yet. Fill in the form and press **Start Analysis**.")
        return

    with tabs[0]:
        show_status_banner(result)
        tab_overview(result)
        md = to_markdown(result, rank(result, weights))
        d1, d2 = st.columns(2)
        d1.download_button("⬇️ Download report (Markdown)", md, f"report-{result.run_id}.md", "text/markdown")
        d2.download_button("⬇️ Download raw data (JSON)", json.dumps(result.to_dict(), ensure_ascii=False,
                           indent=2, default=str), f"run-{result.run_id}.json", "application/json")
    with tabs[1]:
        tab_product(result)
    with tabs[2]:
        tab_markets(result)
    with tabs[3]:
        tab_customers(result, weights)
    with tabs[4]:
        tab_messages(result, weights, scenario)
    with tabs[5]:
        tab_sources(result)


main()
