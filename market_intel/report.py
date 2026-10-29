"""Render an AnalysisResult as a Markdown report for download."""
from __future__ import annotations

from market_intel.core.models import AnalysisResult, Claim
from market_intel.scoring.ranking import CRITERION_LABELS, ScoredCustomer

_BADGE = {"verified": "✅ verified", "probable": "🟡 probable", "unverified": "⚠️ unverified"}


def _cite(claim: Claim | None) -> str:
    if not claim:
        return ""
    ids = ", ".join(claim.source_ids) or "no source"
    return f" _[{ids} · {_BADGE[claim.status.value]}]_"


def _v(x) -> str:
    return x if x else "UNKNOWN"


def to_markdown(r: AnalysisResult, ranking: list[ScoredCustomer]) -> str:
    out = [f"# Market Intelligence Report – {r.input.label}", ""]
    out += [f"- Run: `{r.run_id}` · {r.created_at}",
            f"- Target region: {r.input.target_region or '(open)'}",
            f"- Product URL: {r.input.url or '(none)'}",
            f"- **Status: {r.status.upper()}**" + (" – this is NOT a complete analysis." if r.status != "complete" else ""),
            ""]
    if r.errors or r.warnings:
        out += ["## Problems during the run", ""] + [f"- ❌ {e}" for e in r.errors] + [f"- ⚠️ {w}" for w in r.warnings] + [""]

    if p := r.product:
        out += ["## 1. Product", "", p.summary, ""]
        for title, claims in (("Features", p.features), ("Applications", p.applications), ("Industries", p.industries)):
            out += [f"**{title}**", ""] + [f"- {c.text}{_cite(c)}" for c in claims] + [""]

    out += ["## 2. Target markets", ""]
    out += [f"### {m.name}\n{m.rationale.text}{_cite(m.rationale)}\n" for m in r.markets] or ["_No markets identified._", ""]

    out += ["## 3. Competitors", "", "| Company | Country | Website | Activity | Status |", "|---|---|---|---|---|"]
    out += [f"| {c.name} | {_v(c.country)} | {_v(c.website)} | {_v(c.activity)} | {_BADGE[c.status.value]} |"
            for c in r.competitors]
    out += [""] + [f"- **{c.name}** – {c.reason.text}{_cite(c.reason)}" for c in r.competitors if c.reason] + [""]

    by_name = {c.name: c for c in r.customers}
    out += ["## 4. Potential customers (ranked)", "",
            "| # | Company | Score | Coverage | Country | Industry | Website | Status |", "|---|---|---|---|---|---|---|---|"]
    for s in ranking:
        c = by_name[s.name]
        out.append(f"| {s.rank} | {c.name} | {s.score:.1f} | {s.coverage:.0%} | {_v(c.country)} | {_v(c.industry)} "
                   f"| {_v(c.website)} | {_BADGE[c.status.value]} |")
    out.append("")
    for s in ranking:
        c = by_name[s.name]
        out += [f"### {s.rank}. {c.name} – {s.score:.1f}/100", ""]
        if c.reason:
            out.append(f"Fit: {c.reason.text}{_cite(c.reason)}")
        out += [f"- {e.text}{_cite(e)}" for e in c.evidence]
        out += [f"- {CRITERION_LABELS[b.criterion]}: {'unknown' if b.value is None else f'{b.value:.2f}'} "
                f"× w {b.weight:.2f} = {b.points:.1f} pts – {b.reason}" for b in s.breakdown]
        out += [f"- Calculation: `{s.explain()}`", ""]

    out += ["## 5. Sales messages", ""]
    for m in r.messages:
        tag = "AI-generated" if m.generated_by == "ai" else "TEMPLATE FALLBACK (AI unavailable)"
        out += [f"### To {m.company_name} ({tag})", f"**Subject:** {m.subject}", "", m.body, ""]

    out += ["## 6. Sources & validation", "", "| ID | Type | Accessible | URL |", "|---|---|---|---|"]
    for s in r.sources.values():
        acc = {True: "yes", False: f"no ({s.error})", None: "not checked"}[s.accessible]
        out.append(f"| {s.id} | {s.source_type.value} | {acc} | {s.url} |")
    return "\n".join(out) + "\n"
