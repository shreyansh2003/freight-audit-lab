"""freight-audit-lab dashboard: streamlit run streamlit_app.py

Layout only. What the numbers are and how the findings are worded lives in freight_audit_lab/dashboard.py;
the chart specs live in freight_audit_lab/charts.py. Every figure comes from `outputs/` (the Data tab also reads
the pipeline's normalized files, the diesel series, and config.yaml).
"""

import html
import json

import pandas as pd
import streamlit as st

from freight_audit_lab import charts
from freight_audit_lab.audit.engine import OUTPUT_DIR
from freight_audit_lab.config import REPO_ROOT, get, load_config
from freight_audit_lab.csv_io import read_csv
from freight_audit_lab.dashboard import (demote_headings, error_label, key_findings, load_outputs, md_escape, outputs_ready,
                                         overview_metrics, pct, recoverable_by_carrier, recoverable_by_error_type,
                                         strongest_systemic_carrier, usd)

st.set_page_config(page_title="Freight audit lab", page_icon=None, layout="wide")

CSS = f"""
<style>
.block-container {{ max-width: 1500px; padding: 2.2rem 2.5rem 3rem 2.5rem; }}
h1 {{ font-size: 1.7rem !important; font-weight: 650 !important; letter-spacing: -0.01em; }}
h3 {{ font-size: 1.05rem !important; font-weight: 600 !important; }}
.byline {{ color: {charts.MUTED}; font-size: 0.95rem; margin: -6px 0 10px 0; }}
.byline a {{ color: {charts.ACCENT}; margin-left: 10px; }}
.banner {{ border-left: 3px solid {charts.ACCENT}; background: #F6F8FA; padding: 8px 14px; margin: 4px 0 18px 0;
          color: {charts.INK}; font-size: 0.92rem; }}
.kpi {{ border: 1px solid #E3E7EB; border-radius: 8px; padding: 14px 16px; min-height: 140px; }}
.kpi .label {{ font-size: 0.82rem; color: {charts.MUTED}; }}
.kpi .value {{ font-size: 1.85rem; font-weight: 650; line-height: 1.2; color: {charts.INK}; }}
.kpi.accent .value {{ color: {charts.ACCENT}; }}
.kpi .sub {{ font-size: 0.82rem; color: {charts.MUTED}; margin-top: 2px; }}
.findings {{ background: #F6F8FA; border-radius: 8px; padding: 14px 20px 6px 20px; margin: 18px 0 8px 0; }}
.findings .head {{ font-size: 0.82rem; font-weight: 600; color: {charts.MUTED}; text-transform: uppercase; letter-spacing: .04em; }}
.findings ul {{ margin: 8px 0 8px 0; padding-left: 1.1rem; }}
.findings li {{ margin-bottom: 8px; color: {charts.INK}; font-size: 0.95rem; line-height: 1.45; }}
</style>
"""

TOLERANCES = {"rate_pct": ("Rate overcharge check", "Tolerance: billed linehaul above contract"),
              "fsc_ltl_pp": ("Fuel surcharge check, LTL carriers", "Tolerance: percentage points above the fuel table"),
              "fsc_tl_pct": ("Fuel surcharge check, TL carriers", "Tolerance: share of expected fuel dollars"),
              "weight_pct": ("Weight check", "Tolerance: billed weight above shipment weight")}


GITHUB_URL = "#"        # placeholder: replace with the repository link before publishing

# ---------------------------------------------------------------- loading


@st.cache_data(show_spinner=False)
def cached_outputs():
    return load_outputs()


@st.cache_data(show_spinner=False)
def cached_config():
    return load_config()


@st.cache_data(show_spinner=False)
def cached_data_file(relative_path):
    return read_csv(REPO_ROOT / relative_path)


def ensure_outputs():
    """Run the pipeline (with a spinner) if outputs/ has not been built yet."""
    if not outputs_ready():
        from freight_audit_lab.run import main
        with st.spinner("outputs/ is empty, running the pipeline (about a minute)..."):
            main()
        st.cache_data.clear()


def tile(label, value, sub, accent=False):
    """One headline tile. All arguments are strings built from output files."""
    label, value, sub = (html.escape(t).replace("$", "&#36;") for t in (label, value, sub))     # "$" would start LaTeX
    st.markdown(f'<div class="kpi{" accent" if accent else ""}"><div class="label">{label}</div>'
                f'<div class="value">{value}</div><div class="sub">{sub}</div></div>', unsafe_allow_html=True)


def show(chart):
    st.altair_chart(chart, width="stretch")


def blank_zero(amount):
    """A journal amount as text ($1,234.56), or an empty cell when the line has none on that side."""
    return usd(amount, cents=True) if amount else ""


def month_label(month_end):
    return f"{pd.Timestamp(month_end):%b '%y}"


# ---------------------------------------------------------------- tabs


def overview_tab(o, names):
    m = overview_metrics(o)
    cols = st.columns(4, gap="medium")
    with cols[0]:
        tile("Invoices audited", f"{m['invoices_audited']:,}", f"{usd(m['billed_spend'])} billed")
    with cols[1]:
        tile("Precision: engine vs baseline", pct(m["engine_precision"]),
             f"baseline {pct(m['baseline_precision'])}; recall {pct(m['engine_recall'])} vs {pct(m['baseline_recall'])}",
             accent=True)
    with cols[2]:
        tile("False disputes avoided", f"{m['false_disputes_avoided']:,}",
             f"baseline {m['baseline_false_positives']:,} false flags, engine {m['engine_false_positives']:,}")
    with cols[3]:
        tile("Recoverable, estimate (synthetic)", usd(m["recoverable_estimate"]),
             f"{pct(m['recoverable_pct_of_spend_estimate'], 2)} of billed; {m['invoices_flagged']:,} invoices flagged")

    bullets = "".join(f"<li>{md_to_html(t)}</li>" for t in key_findings(o, names))
    st.markdown(f'<div class="findings"><div class="head">Key findings</div><ul>{bullets}</ul></div>',
                unsafe_allow_html=True)

    left, right = st.columns(2, gap="large")
    with left:
        by_carrier = recoverable_by_carrier(o, names)
        show(charts.bar_by_category(by_carrier, "carrier", "recoverable_estimate",
                                    "Recoverable estimate by carrier (USD, synthetic)"))
    with right:
        by_type = recoverable_by_error_type(o)
        show(charts.bar_by_category(by_type, "label", "recoverable_estimate",
                                    "Recoverable estimate by error type (USD, synthetic)", height=30 * len(by_carrier) + 10))
    st.caption("Recoverable dollars are the audit's estimate: whole invoices for duplicates and phantoms, the overcharge "
               "for the rest, each dollar counted once. The error rates behind them were injected, so the total is circular.")


def md_to_html(text):
    """The findings are `**bold** rest`; render the bold span as <strong> for the HTML box."""
    bold, tail = text[2:].split("**", 1)
    return f"<strong>{html.escape(bold)}</strong>{html.escape(tail)}"


def audit_quality_tab(o):
    ev = o["eval_engine_vs_baseline"]
    per_type = ev[ev["error_type"] != "ALL"].copy()
    per_type["label"] = per_type["error_type"].map(error_label)

    st.markdown("### Engine vs baseline")
    table = ev.assign(error_type=ev["error_type"].map(lambda t: "All types" if t == "ALL" else error_label(t)))
    table = table[["error_type", "engine_precision", "baseline_precision", "engine_recall", "baseline_recall",
                   "engine_fp", "baseline_fp"]]
    for col in ("engine_precision", "baseline_precision", "engine_recall", "baseline_recall"):
        table[col] = table[col] * 100
    table.columns = ["Error type", "Precision, engine", "Precision, baseline", "Recall, engine", "Recall, baseline",
                     "False flags, engine", "False flags, baseline"]
    pct_col = st.column_config.NumberColumn(format="%.1f%%")
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={c: pct_col for c in table.columns if "Precision" in c or "Recall" in c})
    left, right = st.columns(2, gap="large")
    for side, measure, title in ((left, "precision", "Precision by error type (share of flags that are right)"),
                                 (right, "recall", "Recall by error type (share of injected errors found)")):
        long = pd.concat([per_type[["label", f"{s}_{measure}"]].rename(columns={f"{s}_{measure}": "value"}).assign(
            system=s.capitalize()) for s in ("engine", "baseline")])
        with side:
            show(charts.engine_vs_baseline_bars(long, "label", title, ".0%"))

    st.markdown("### Where the baseline goes wrong: false flags by cause")
    causes = o["baseline_fp_causes"]
    show(charts.bar_by_category(causes.assign(cause=causes["description"].str.capitalize()), "cause", "false_flags",
                                "Baseline false flags by cause (each flag counted once, so the bars add up)", ",d",
                                color=charts.GREY, tip="False flags"))
    st.caption("Each false flag is attributed to one cause, using the rule that raised it and the first matching "
               "reason in this order: rate amendment, reweigh, weight error misread as rate, late authorization, "
               "rebill / balance due, BOL typo or dropped leading zero, other. \"Other\" is flags on resent "
               "duplicates, which do not carry the trap labels of the invoice they copy.")
    st.markdown("##### Detail: false flags by trap (an invoice with two traps counts under both)")
    traps = o["eval_traps"]
    detail = traps.assign(trap=traps["trap"].str.replace("_", " "),
                          engine_fp_by_type=traps["engine_fp_by_type"].fillna(""),
                          baseline_fp_by_type=traps["baseline_fp_by_type"].fillna(""))
    detail.columns = ["Trap", "Invoices with trap", "Engine false flags", "Engine flags by rule", "Baseline false flags",
                      "Baseline flags by rule"]
    st.dataframe(detail, hide_index=True, width="stretch", column_config={
        "Engine flags by rule": st.column_config.TextColumn(width="medium"),
        "Baseline flags by rule": st.column_config.TextColumn(width="large")})
    st.caption("A false flag is an (invoice, error type) flag with no matching injected error. Traps overlap, so the "
               "rows do not add up to the total; the chart above is the additive view. The last row is clean invoices, "
               "where neither system raises a false flag.")

    st.markdown("### Tolerance sweep")
    picks = list(TOLERANCES)
    pick = st.radio("Tolerance", picks, format_func=lambda k: TOLERANCES[k][0], horizontal=True, label_visibility="collapsed")
    sweep = o["sweep"][o["sweep"]["tolerance"] == pick].copy()
    is_pp = pick == "fsc_ltl_pp"
    sweep["label"] = sweep["value"].map(lambda v: f"{v:g} pp" if is_pp else f"{v * 100:g}%")
    sweep["note"] = ["" if not (c or r) else " and ".join(n for n, on in (("current", c), ("recommended", r)) if on)
                     for c, r in zip(sweep["is_current"], sweep["is_recommended"])]
    floor = float(json.loads((OUTPUT_DIR / "recommended_tolerances.json").read_text())[pick]["min_precision"])
    show(charts.sweep_chart(sweep, TOLERANCES[pick][1], floor, f"{TOLERANCES[pick][0]}: precision and recall by tolerance"))
    st.caption(json.loads((OUTPUT_DIR / "recommended_tolerances.json").read_text())[pick]["rationale"].replace("$", "\\$")
               + " Dollar figures are estimates.")


def exception_tab(o, names):
    queue = o["exception_queue"]
    st.markdown("### Open exceptions")
    c1, c2 = st.columns(2)
    carriers = c1.multiselect("Carrier", list(names), format_func=lambda c: f"{c}: {names[c]}", placeholder="All carriers")
    types = sorted({t for cell in queue["error_type"] for t in cell.split(";")})
    picked = c2.multiselect("Error type", types, format_func=error_label, placeholder="All error types")
    view = queue
    if carriers:
        view = view[view["carrier_id"].isin(carriers)]
    if picked:
        view = view[view["error_type"].map(lambda cell: bool(set(cell.split(";")) & set(picked)))]
    st.caption(f"{len(view):,} invoices, {usd(view['recoverable_estimate'].sum())} recoverable estimate. "
               "Click a column header to sort; scroll right for the reason (the CSV has it in full).")
    shown = view[["rank", "carrier_id", "invoice_number", "error_type", "recoverable_estimate", "dollar_impact_estimate",
                  "received_date", "days_open", "reason"]].assign(
        error_type=view["error_type"].map(lambda cell: ", ".join(error_label(t) for t in cell.split(";"))))
    shown.columns = ["Rank", "Carrier", "Invoice", "Error type", "Recoverable (estimate)", "Flagged (estimate)",
                     "Received", "Days open", "Reason"]
    st.dataframe(shown, hide_index=True, width="stretch", height=380, column_config={
        "Recoverable (estimate)": st.column_config.NumberColumn(format="dollar"),
        "Flagged (estimate)": st.column_config.NumberColumn(format="dollar"),
        "Received": st.column_config.DateColumn(format="YYYY-MM-DD"),
        "Reason": st.column_config.TextColumn(width=1000)})          # about 140 characters; scroll right for it
    st.download_button("Download these rows as CSV", view.to_csv(index=False), "exception_queue_filtered.csv", "text/csv")

    st.markdown("### Dispute summary")
    by_label = {f"{c}: {name}": c for c, name in names.items()}
    first = strongest_systemic_carrier(o)          # open on the carrier with the strongest systemic finding
    carrier = by_label[st.selectbox("Carrier dispute pack", list(by_label),
                                    index=list(by_label.values()).index(first) if first in by_label.values() else 0)]
    md_path = OUTPUT_DIR / "disputes" / f"{carrier}.md"
    text = md_path.read_text()
    with st.container(border=True):
        st.markdown(md_escape(demote_headings(text)))
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button("Download summary (.md)", text, f"{carrier}.md", "text/markdown")
    d2.download_button("Download invoices (.csv)", (OUTPUT_DIR / "disputes" / f"{carrier}.csv").read_text(),
                       f"{carrier}.csv", "text/csv")


def accruals_tab(o):
    acc = o["accrual_accuracy"]
    monthly = acc[acc["month_end"] != "ALL"].copy()
    total = acc[acc["month_end"] == "ALL"].iloc[0]
    sens = o["accrual_sensitivity"]
    sens_monthly = sens[sens["month_end"] != "ALL"]
    monthly["month"] = monthly["month_end"].map(month_label)

    bars = pd.concat([monthly[["month", "accrual_estimate"]].rename(columns={"accrual_estimate": "value"}).assign(
        series="Accrued"), monthly[["month", "actual_payable_estimate"]].rename(
        columns={"actual_payable_estimate": "value"}).assign(series="Payable")])
    show(charts.month_bars(bars, "Accrued vs eventual payable by month-end (USD, estimate)"))

    lines = pd.concat([
        pd.DataFrame({"month": monthly["month"], "error_pct": monthly["error_pct_estimate"], "series": "As built"}),
        pd.DataFrame({"month": monthly["month"], "error_pct": sens_monthly["error_pct_estimate_auth_at_delivery"].to_numpy(),
                      "series": "Authorized at delivery"})])
    show(charts.error_lines(lines, "Accrual error, % of eventual payable (estimate; below zero = under-accrued)"))
    split = pd.DataFrame({
        "Component": ["Linehaul + fuel", "Accessorials", "Total"],
        "Accrued (estimate)": [total["lh_fsc_accrual_estimate"], total["accessorial_accrual_estimate"], total["accrual_estimate"]],
        "Payable (estimate)": [total["lh_fsc_payable_estimate"], total["accessorial_payable_estimate"], total["actual_payable_estimate"]],
        "Error (estimate)": [total["lh_fsc_error_estimate"], total["accessorial_error_estimate"], total["error_estimate"]],
        "Error % of payable": [100 * total["lh_fsc_error_pct_estimate"], 100 * total["accessorial_error_pct_estimate"],
                               100 * total["error_pct_estimate"]]})
    st.dataframe(split, hide_index=True, width="stretch", column_config={
        **{c: st.column_config.NumberColumn(format="dollar") for c in split.columns[1:4]},
        "Error % of payable": st.column_config.NumberColumn(format="%.2f%%")})
    st.caption("All months together. Linehaul and fuel are priced from the contract, so they land close; the accessorial "
               "allowance is the weak spot, and authorizations recorded after month-end leave it short. "
               "The grey line reruns the same accruals with every authorization recorded at delivery "
               "(outputs/accrual_sensitivity.csv).")

    st.markdown("### Journal entry preview")
    months = {month_label(m): m for m in monthly["month_end"]}
    chosen = months[st.selectbox("Month-end", list(months), index=len(months) - 1)]
    je = o["journal_entries"]
    stamp = f"{pd.Timestamp(chosen):%Y%m}"
    entry = je[je["je_id"].isin([f"ACR-{stamp}", f"REV-{stamp}"])]
    shown = entry.assign(type=entry["type"].str.capitalize(), cost_center=entry["cost_center"].fillna(""),
                         debit=entry["debit"].map(blank_zero), credit=entry["credit"].map(blank_zero))       # text: a null number shows as "None"
    shown.columns = ["Entry", "Date", "Type", "Account", "Cost center", "Debit", "Credit", "Memo"]
    st.dataframe(shown, hide_index=True, width="stretch", column_config={"Memo": st.column_config.TextColumn(width="large")})
    st.caption("The accrual is reversed in full on day 1 of the next month; posting the real invoices then produces the true-up.")


def data_tab(cfg):
    st.markdown("### Normalization")
    report = cached_data_file("data/normalized/normalization_report.csv")
    pivot = report.pivot_table(index=["fix_type", "unit"], columns="carrier_id", values="count", aggfunc="sum", fill_value=0)
    st.caption("Fixes applied to the raw carrier files, by carrier.")
    st.dataframe(pivot.reset_index().rename(columns={"fix_type": "Fix", "unit": "Unit"}), hide_index=True, width="stretch")
    exceptions = cached_data_file("data/normalized/normalization_exceptions.csv")
    st.caption(f"Normalization exceptions: {len(exceptions):,} lines that could not be loaded or mapped.")
    st.dataframe(exceptions, hide_index=True, width="stretch", height=220)

    st.markdown("### Diesel price series")
    diesel = cached_data_file("data/reference/diesel_weekly.csv")
    show(charts.diesel_line(diesel, "Weekly diesel price (USD per gallon)"))
    st.caption(f"Source: {', '.join(sorted(diesel['source'].unique()))}. Fuel surcharges are priced from the ship-week price.")

    st.markdown("### Key config values")
    keys = [("Seed", "seed", "{}"), ("Period start", "period.start", "{}"), ("Audit as-of date", "period.audit_as_of", "{}"),
            ("Shipments", "shipments.n", "{:,}"), ("Rate tolerance (share)", "audit.tolerances.rate_pct", "{}"),
            ("LTL fuel tolerance (pp)", "audit.tolerances.fsc_ltl_pp", "{}"), ("Weight tolerance (share)", "audit.tolerances.weight_pct", "{}"),
            ("Precision floor for the sweep", "evaluation.min_precision", "{}"),
            ("Review minutes per flag (estimate)", "evaluation.review_minutes_per_flag", "{}"),
            ("Analyst cost per hour (estimate)", "evaluation.analyst_cost_per_hour", "${}"),
            ("False dispute cost (estimate)", "evaluation.false_dispute_cost", "${}"),
            ("Accrual trailing window (days)", "accruals.trailing_days", "{}"),
            ("Late authorization share", "accessorials.late_authorization_share", "{}")]
    st.dataframe(pd.DataFrame([(label, fmt.format(get(cfg, key)), f"config.yaml: {key}") for label, key, fmt in keys],
                              columns=["Setting", "Value", "Where"]), hide_index=True, width="stretch")
    with st.expander("ASSUMPTIONS.md (every assumption, why, and how to change it in config.yaml)"):
        st.markdown(md_escape((REPO_ROOT / "ASSUMPTIONS.md").read_text()))


# ---------------------------------------------------------------- page


def main():
    st.markdown(CSS, unsafe_allow_html=True)
    st.title("Freight audit lab")
    st.markdown('<div class="byline">A simulated freight invoice audit and month-end accrual pipeline, built by Shrey.'
                f'<a href="{GITHUB_URL}">Code on GitHub</a></div>', unsafe_allow_html=True)
    st.markdown('<div class="banner"><strong>Synthetic data. Every dollar figure is an estimate from a simulated dataset.</strong></div>',
                unsafe_allow_html=True)
    ensure_outputs()
    outputs, cfg = cached_outputs(), cached_config()
    names = {c["id"]: c["name"] for c in cfg["carriers"]}
    tabs = st.tabs(["Overview", "Audit quality", "Exception queue", "Month-end accruals", "Data & assumptions"])
    with tabs[0]:
        overview_tab(outputs, names)
    with tabs[1]:
        audit_quality_tab(outputs)
    with tabs[2]:
        exception_tab(outputs, names)
    with tabs[3]:
        accruals_tab(outputs)
    with tabs[4]:
        data_tab(cfg)


main()
