> Original build plan. Where the build departed from it, ASSUMPTIONS.md records the change and why.

# freight-audit-lab: build spec

## What this project is

A shipper pays thousands of carrier invoices a year. Some are wrong. The finance team also
has to book freight expense at month-end for deliveries the carriers haven't billed yet.
This project simulates both jobs end to end on **synthetic data** with a known answer key:

1. Generate a year of shipments, contracts, diesel prices, and carrier invoices, with
   injected billing errors **and realistic traps** (cases that look like errors but aren't).
2. Render the invoices as messy carrier files in 4 different layouts.
3. Normalize the files, match them to shipments, re-rate them against the contract, and audit
   them with one rule per error type.
4. Score the audit against the answer key, compare it with a naive baseline, and sweep the
   tolerances to choose an operating point.
5. Produce the output an operations associate would work from: a ranked exception queue
   and dispute summaries for each carrier.
6. Book month-end freight accruals with journal entries, reverse them, and measure accrual
   accuracy once the invoices arrive.
7. Show it all in a Streamlit dashboard, with a one-page README memo.

The traps are the point. A rule that flags every injected error is easy. A rule that also
leaves legitimate rebills, contract amendments, and documented reweighs alone is what
real audit looks like, and the naive baseline shows the difference.

---

## Repo layout

```
freight-audit-lab/
  SPEC.md  README.md  ASSUMPTIONS.md
  config.yaml  requirements.txt  .gitignore  streamlit_app.py
  freight_audit_lab/
    __init__.py  config.py  run.py
    generate/   __init__.py  network.py  diesel.py  rates.py  shipments.py
                invoices.py  inject.py  render.py
    normalize.py      # raw carrier files -> clean invoice tables + match to shipments
    rerate.py         # expected charges per invoice from the contract
    audit/      __init__.py  rules.py  engine.py  baseline.py
    evaluate.py       # the only modules allowed to read ground truth:
    sweep.py          #   evaluate.py and sweep.py
    exceptions.py     # exception queue + carrier dispute summaries
    accruals.py
  data/
    README.md                      # "SYNTHETIC DATA ..."
    public/                        # optional EIA diesel CSV the author downloads
    reference/                     # what the shipper legitimately knows
    raw/invoices/<carrier>/<YYYY-MM>.csv   # messy carrier files
    ground_truth/                  # answer key; audit code must never read this
    normalized/                    # output of normalize.py
  outputs/                         # audit, eval, sweep, accrual results, summary.json
  tests/
```

`data/` and `outputs/` are small and synthetic. Commit them so the deployed dashboard loads
instantly and reviewers can browse the CSVs on GitHub. Keep the total under 25 MB.

---

## Stage 1: Scaffold, config, reference data

**Build**

- `config.yaml` using the skeleton in the appendix. `config.py` loads it into a dict and
  validates required keys (fail loudly with a helpful message).
- `data/README.md` with the synthetic-data notice.
- **Network (`network.py`).** 5 origin facilities (each one is a cost center) and 20
  destination cities, as real US city names with approximate lat/lon (public knowledge).
  Lane miles = haversine distance × `circuity_factor`, rounded to whole miles. Sample
  `n_lanes` (60) distinct origin-destination pairs. Write `lanes.csv` and `facilities.csv`.
- **Carriers.** 8 invented carriers from config: 5 LTL and 3 TL. Each has an id, a display
  name, name variants (used later in messy files), an invoice file format (A-D), an invoice
  lag median, and an `error_multiplier`. Assign carriers to lanes: each lane gets
  `carriers_per_lane.LTL` LTL carriers and `carriers_per_lane.TL` TL carriers.
- **Rate card (`rates.py`).** One row per carrier-lane with `effective_from` and `effective_to`.
  - LTL: price per hundredweight (cwt = 100 lb). The base rate for the 1,000-1,999 lb break
    is `intercept + per_mile × miles`, times a carrier factor and a lane noise factor.
    Other weight breaks apply their multipliers. Linehaul = max(min_charge,
    cwt_rate(weight) × weight / 100).
  - TL: rate per mile × miles, with a minimum charge.
  - Amendments: for `share_of_carrier_lanes` of carrier-lanes, split the row at
    `effective_date` and change the rate by a draw from `change_range` (some go up, some go
    down). Write `rate_card.csv`.
  - One function, `contract_linehaul(rate_row, weight_lbs, miles)`. The generator, the
    re-rater, and accruals all use it. Pricing logic must exist in exactly one place.
- **Diesel (`diesel.py`).** A weekly Monday-dated series from 4 weeks before the period
  start through the end of runoff. Synthetic by default: a mean-reverting random walk
  bounded to [min, max], plus the optional shock. `load_eia_csv(path)` reads an EIA weekly
  diesel CSV. The EIA series is EMD_EPD2D_PTE_NUS_DPG, "Weekly U.S. No 2 Diesel Retail
  Prices", downloadable from
  https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=EMD_EPD2D_PTE_NUS_DPG&f=W.
  The loader skips non-data header lines, finds the date and price columns, and fails
  clearly if the file doesn't cover the period. `diesel.source` chooses which series is
  used. Write `diesel_weekly.csv` with a `source` column (`synthetic` or `eia`).
- **FSC schedule.** LTL: FSC % = max(0, floor((diesel − base_price) / step)) × pct_per_step,
  applied to linehaul. TL: FSC $/mile = max(0, (diesel − peg_price) / mpg), times miles.
  The diesel price used is the one for the **ship week** (the Monday on or before the
  ship date). Write `fsc_ltl_table.csv` for readability. Put the FSC functions in `rates.py`.
- **Shipments (`shipments.py`).** `shipments.n` shipments spread across the 12 months using
  `monthly_weights`. For each one: a lane, a mode (TL with probability `tl_share`), a
  carrier among that lane's carriers of that mode, and a weight (LTL: lognormal, clipped;
  TL: uniform). Transit days = ceil(miles / miles_per_day) + a random 0-1 days. Assign a
  cost center from the origin facility and a canonical 8-digit BOL number (unique). Write
  `shipments.csv`.
- **Accessorials and authorizations.** Draw legitimate accessorials by mode using the
  config probabilities (LTL liftgate and residential; TL detention = billable hours × hourly
  rate, where billable hours = total hours − free hours). Each legitimate accessorial gets an
  authorization row: auth_id, shipment_id, code, authorized_amount, authorized_at. Normally
  authorized_at falls on or before the delivery date. Set it after the invoice date later,
  in Stage 2, for the late-authorization trap. Write `authorizations.csv`.
- **Reweigh certificates.** For `reweigh_share_ltl` of LTL shipments, the carrier reweighs
  the freight: certified_weight = shipment weight × U(1.05, 1.20), with a certificate_id and
  certified_at. Write `reweigh_certificates.csv`. (It's a trap: billing the certified weight
  is legitimate.)
- All of the above goes to `data/reference/`.

**Tests**: config loads; same seed gives identical reference files (hash compare);
`contract_linehaul` hits the min charge, weight breaks, and TL minimums correctly on
hand-computed cases; the effective-dated lookup returns the right row on either side of an
amendment; FSC math matches hand-computed values at three diesel prices; the EIA loader
parses a tiny fixture CSV with junk header lines; every shipment's delivery date is after
its ship date.

---

## Stage 2: Invoices, traps, errors, messy files

Build **canonical invoices** first (true values, one row per invoice plus charge lines),
inject traps and errors on them, and only then render the messy files.

**Canonical invoices (`invoices.py`)**

- One original invoice per shipment. Charge lines: LH (contract linehaul at the billed
  weight), FSC, and any authorized accessorials.
- Invoice date = delivery date + lag, where lag ~ lognormal(median = the carrier's
  lag_median_days, sigma = lag_sigma), capped at `max_lag_days`. Received date = invoice
  date + U(receipt_delay_days). Some shipments are therefore still unbilled at each
  month-end, and every invoice arrives before runoff ends.
- Each invoice gets a carrier invoice_number, a pro_number (the carrier's shipment
  reference), and a **control_id**. The control_id is a unique transmission ID, like an EDI
  control number or a portal document ID, and a resent duplicate gets a new one. The
  normalized `invoice_id` is `<carrier_id>:<control_id>`, and ground truth keys on it.

**Traps (legitimate, labeled `trap` in ground truth)**

| Trap | What happens | What a naive audit does wrong |
|---|---|---|
| `rebill` | 10-30 days after the original, the carrier sends a corrected invoice that supersedes it, with a small legitimate change. The original becomes `superseded`. | Flags it as a duplicate |
| `balance_due` | A later supplemental invoice for the same BOL carrying only an authorized accessorial. | Flags it as a duplicate |
| `rate_amendment` | Invoices on amended carrier-lanes shipped after the effective date use the new rate. | Uses the old rate and flags an overcharge |
| `documented_reweigh` | Billed weight = certified reweigh weight. | Flags weight overbilling |
| `late_authorization` | For `late_authorization_share` of legitimate accessorials, authorized_at is after the invoice date but before `audit_as_of`. | Checks authorization as of the invoice date and flags it |
| `bol_format` | The BOL in the carrier file has formatting noise: leading zeros dropped, `BOL-` or `BOL#` prefixes, spaces or dashes, lowercase. | Can't match it and calls it a phantom |
| `bol_typo` | Two adjacent BOL digits transposed; the shipment still exists. | Calls it a phantom |
| `rounding_noise` | LH and/or FSC is off by up to ±`rounding_noise_pct` of the line (carrier system rounding). | Flags it when tolerances are too tight |

**Errors (labeled `error`, each with a mode and a true dollar impact)**

Inject onto **original** invoices only (simpler; log it in ASSUMPTIONS). Rates come from
config, times the carrier's `error_multiplier`. An invoice can have more than one error.

| Error type | Modes | True dollar impact |
|---|---|---|
| `duplicate_invoice` | Resend with the same invoice number, or re-keyed with a new invoice number; received 5-45 days after the original | Duplicate's total |
| `rate_overcharge` | `markup` (LH × U(rate_markup)); `stale_rate` (the pre-amendment rate billed after an amendment that *lowered* the rate); `sub_tolerance` (a tiny markup) | (billed LH − contract LH at billed weight) × (1 + FSC%) for LTL; billed LH − contract LH for TL |
| `fsc_mismatch` | `wrong_week` (diesel from 1-3 weeks *later*); `wrong_step` (1-3 extra LTL steps; TL uses an inflated price); `sub_tolerance` | Billed FSC − expected FSC |
| `unauthorized_accessorial` | Liftgate or residential (LTL), or detention (TL), with no authorization | Line amount |
| `weight_overbilling` | LTL only, no reweigh certificate; billed weight = actual × U(weight_inflation); `sub_tolerance` mode too. LH is re-rated at the billed weight | (LH at billed weight − LH at actual weight) × (1 + FSC%) |
| `phantom_invoice` | An invoice for a BOL that matches no shipment, on one of the carrier's lanes, with a random date and weight. Re-draw if any shipment would satisfy the fallback match. | Total |

- **Systemic issues.** Apply each `systemic_issues` entry: during its date window, that
  carrier's rate for that error type and mode is set to `rate`. This is what the dispute
  summary should detect later.
- **Zero-impact rule.** Compute the true impact from true values. If an injection produces
  ≤ $0.01 of impact (for example, a weight inflation that stays at the min charge, or a
  "wrong week" with the same diesel step), re-draw it or skip it. Never label a $0 error.
- `sub_tolerance` errors are real errors that the default tolerances will miss by design.
  They make recall honest and give the tolerance sweep something to trade off.

**Ground truth**: `data/ground_truth/labels.csv` with one row per (invoice_id, label):
`invoice_id, label_kind (error|trap|superseded|clean), label, mode, true_dollar_impact`.

**Messy files (`render.py`)**: write each carrier's invoices to
`data/raw/invoices/<carrier_id>/<YYYY-MM of received_date>.csv` in that carrier's format.
Every layout carries an invoice date (in its own date format). Also write
`data/reference/ap_receipt_log.csv` (carrier_id, control_id, received_date), the shipper's
AP date stamp for each invoice as it arrives. Nothing from ground truth may appear in these
files.

| Format | Layout | Messiness |
|---|---|---|
| A | Wide: one row per invoice; columns like `Invoice No`, `Pro #`, `BOL`, `EDI Ctrl #`, `Ship Date`, `Origin`, `Dest`, `Weight (lbs)`, `Linehaul`, `Fuel Surcharge`, `Acc1 Code`, `Acc1 Amt`, `Acc2 Code`, `Acc2 Amt`, `Total` | Dates MM/DD/YYYY; rebill and balance due marked only by pro suffix (`-C`, `-BD`) |
| B | Long: one row per charge line; `invoice_number`, `pro_number`, `bill_of_lading`, `doc_id`, `ship_date`, `origin_city`, `dest_city`, `weight_cwt`, `charge_code`, `amount`, `invoice_type`, `supersedes` | ISO dates; weight in cwt; explicit type column |
| C | Wide, like A but with a `Carrier` column holding a name variant | Amounts as strings (`"$1,234.56"`); timestamp dates; weights like `"1,250 LB"`; pro suffix `R` / `B` |
| D | Long, with free-text charge descriptions (`FUEL SURCHARGE`, `LIFTGATE SERVICE`, `RESIDENTIAL DELIVERY`, `DETENTION - DRIVER`) | Dates like `04-Mar-25`; `BOL#` prefixes; explicit type column; city names in inconsistent case and punctuation |

Also include one or two unknown charge descriptions (e.g. `MISC ADJ`) in format D. They
should end up in normalization exceptions, not be silently dropped.

**Tests**: same seed gives identical raw files and labels; every labeled error has impact
> $0.01; invoice counts by type reconcile (originals = shipments; plus duplicates, rebills,
balance-due, and phantoms); no ground-truth column names appear in any raw file header; all
received dates fall within the runoff window; the observed injection rate is within a
sensible band of config for each error type.

---

## Stage 3: Normalization and matching (`normalize.py`)

- Read every raw file. **Detect the format from the header signature**, not the folder
  name. Map each layout to one schema:
  - `invoices`: invoice_id, carrier_id, invoice_number, pro_number, bol_raw, bol_canonical,
    invoice_type (original|rebill|balance_due), supersedes_invoice_number, invoice_date,
    received_date, ship_date, origin, destination, billed_weight_lbs, total, source_file
  - `invoice_lines`: invoice_id, charge_code (LH|FSC|LIFTGATE|RESIDENTIAL|DETENTION), amount
- Received date comes from joining `data/reference/ap_receipt_log.csv` on carrier +
  control_id, the way an AP team date-stamps what arrives. Carrier files don't carry it.
- Fixes, each counted: date parsing, cwt → lbs, currency strings → floats, carrier name
  variants → carrier_id (via config), charge descriptions → codes (via a config mapping),
  BOL canonicalization (strip non-digits, left-pad to 8), city name cleanup, invoice type
  from pro suffix.
- Rebills supersede the latest earlier non-superseded invoice they reference (by
  `supersedes`, or by base pro number when only a suffix marks them). Add
  `is_superseded`.
- **Matching** each invoice to a shipment: (1) exact on carrier + canonical BOL, giving
  `match_method = exact`; (2) otherwise a fallback on carrier + origin + destination + ship
  date ±1 day + weight within 2%, if it finds exactly one shipment, giving `fallback`; (3)
  otherwise `unmatched`.
- Outputs to `data/normalized/`: `invoices.csv`, `invoice_lines.csv`,
  `normalization_report.csv` (carrier × fix type × count), `normalization_exceptions.csv`
  (unparseable rows and unmapped charge descriptions, with source file and reason).
- **Totals check**: for every invoice, the sum of lines equals the stated total within
  $0.01. Otherwise it goes to exceptions.

**Tests**: each format parses a tiny hand-written fixture into the same normalized rows;
the BOL canonicalizer handles every noise pattern; fallback matching resolves a transposed
BOL and refuses when two shipments tie; the unknown charge goes to exceptions; rebills set
`is_superseded` on the right invoice.

---

## Stage 4: Re-rater, audit rules, baseline

**Re-rater (`rerate.py`).** For each non-superseded, matched invoice, compute:

- `reference_weight`: the certified reweigh weight if a certificate exists, otherwise the
  shipment weight
- `lh_contract_at_billed_wt` and `lh_contract_at_ref_wt`, using the rate row effective on
  the ship date
- `fsc_expected`: LTL = expected FSC% × **billed** LH; TL = expected $/mile × lane miles
- The authorization status of each accessorial line as of `audit_as_of`

This decomposition keeps the rules from double counting:
`rate impact` = billed LH − LH at billed weight; `weight impact` = LH at billed weight − LH
at reference weight; `fsc impact` = billed FSC − expected FSC on the billed LH. For LTL, the
first two also carry the FSC knock-on (× (1 + expected FSC%)). Explain this in a docstring.

**Rules (`audit/rules.py`).** Each is a function
`(normalized frames, rerated, reference, config) -> DataFrame[invoice_id, error_type,
reason, dollar_impact_estimate, evidence...]`. `reason` is a readable sentence with
the numbers, e.g. *"Linehaul $1,245.00 vs contract $1,120.50 (CHI→ATL, rate effective
2025-07-01): +$124.50 (+11.1%)"*.

1. `duplicate_invoice`: Among non-superseded `original` invoices for the same shipment (or
   the same carrier + canonical BOL when unmatched), if two or more have totals within
   `duplicate_amount` and were received within `duplicate_window_days`, flag all but the
   earliest received. Also flag any repeat of the same carrier + invoice_number. Rebills
   and balance-due invoices are never duplicates of their parents.
2. `phantom_invoice`: `match_method == unmatched`.
3. `rate_overcharge`: rate impact > max(`rate_abs`, `rate_pct` × contract LH).
4. `fsc_mismatch`: LTL: implied FSC% − expected FSC% > `fsc_ltl_pp` percentage points and
   impact > `fsc_min_dollars`. TL: impact > `fsc_tl_abs`.
5. `unauthorized_accessorial`: an accessorial line with no authorization for that shipment
   and code recorded on or before `audit_as_of`. Report authorizations recorded *after* the
   invoice date as a process metric, not as errors.
6. `weight_overbilling`: LTL; billed weight > reference weight × (1 + `weight_pct`).

**Engine (`audit/engine.py`).** Run all rules, then compute `recoverable_estimate` per
invoice: if duplicate or phantom, the invoice total (other flags kept for information at
$0); otherwise the sum of that invoice's impacts, capped at its total. Write
`outputs/audit_flags.csv` and `outputs/audit_invoice_summary.csv`.

**Baseline (`audit/baseline.py`).** What a quick spreadsheet pass does, with the same
tolerances: match on the raw BOL string (no normalization, no fallback); call two invoices
with the same BOL duplicates regardless of type; rate-check against the *first* rate row
per carrier-lane (ignoring effective dates) at the shipment weight; check authorization as
of the invoice date; ignore reweigh certificates. Write `outputs/baseline_flags.csv`.

**`run.py`**: `python -m freight_audit_lab.run` runs generate → normalize → rerate →
audit → baseline, and grows in later stages. It prints row counts and timings.

**Tests** (small hand-built fixtures in `tests/fixtures.py`, not the full generator). For
each rule: (a) catches a clear error; (b) ignores its matching trap; (c) the tolerance
boundary works, with a value just under not flagged and just over flagged; (d) the dollar
impact is right to the cent. Plus the recoverable dedupe (a duplicate that also has a rate
error counts once). Plus a **leakage test**: (1) none of `normalize.py`, `rerate.py`,
`audit/`, `exceptions.py`, or `accruals.py` contains the strings `ground_truth` or
`labels.csv`; (2) running normalize → rerate → audit with `data/ground_truth/` temporarily
moved away produces identical outputs.

---

## Stage 5: Evaluation, sweep, exception queue, disputes

**Evaluation (`evaluate.py`)**. Superseded invoices are excluded.

- Per error type, at the (invoice_id, error_type) level: TP, FP, FN, precision, recall,
  F1, flagged $, true $, and **dollar recall** (true $ of TPs / total true $), for both the
  engine and the baseline.
- Recall by error **mode**, which shows the sub_tolerance misses explicitly.
- A trap table: for each trap type, how many invoices had it and how many false positives
  each of the engine and the baseline raised on them.
- Outputs: `eval_by_type.csv`, `eval_by_mode.csv`, `eval_traps.csv`,
  `eval_engine_vs_baseline.csv`.

**Tolerance sweep (`sweep.py`)**. Vary one tolerance at a time over its grid, with the
others held at config values: `rate_pct`, `fsc_ltl_pp`, `weight_pct`. For each point:
flags, TP, FP, precision, recall, TP $, `review_cost_estimate` = flags ×
review_minutes_per_flag / 60 × analyst_cost_per_hour, `false_dispute_cost_estimate` = FP
× false_dispute_cost, `net_value_estimate` = TP $ − both costs. The recommended point
maximizes net value subject to precision ≥ `min_precision`. Write `sweep.csv` and
`recommended_tolerances.json`, including a 2-3 sentence rationale generated from the
numbers. **Don't** edit config.yaml automatically; report the recommendation next to
the current value.

**Exception queue (`exceptions.py`)**. `outputs/exception_queue.csv`: rank, invoice_id,
carrier, invoice_number, pro, BOL, ship date, received date, error_type, reason,
dollar_impact_estimate, recoverable_estimate, days_open (as of audit_as_of), status=`open`.
Sort by recoverable $, then oldest.

**Dispute summaries.** `outputs/disputes/<carrier_id>.md` and `.csv`: period, counts and $
by error type, top 10 invoices with reasons, and a **systemic pattern check**. For any
error type, if the carrier's flag rate in some 3-month window is ≥ 3× the all-carrier rate
for that type, add a line such as *"Possible systemic issue: FSC billed above schedule on
38% of invoices Aug-Oct 2025 vs 3% across carriers; pattern consistent with a later-week
diesel reference. Request corrected FSC table."* Build it from a template; no LLM calls.

**Tests**: metrics are right on a tiny hand-labeled example; the sweep's recommended
point satisfies the precision floor; the systemic check fires on a fixture with a
concentrated pattern and stays quiet on a uniform one.

---

## Stage 6: Month-end accruals (`accruals.py`)

For each of the 12 month-ends M:

- **Population**: shipments delivered on or before M that have no matched, non-superseded
  original or rebill invoice *received* on or before M. (Balance-due invoices don't count
  as billing the shipment.) In-transit shipments are not accrued: expense is recognized at
  delivery. Log that in ASSUMPTIONS.
- **Estimate per shipment**: contract LH at the shipment weight using the rate effective on
  the ship date, + expected FSC (ship-week diesel), + an accessorial allowance = that
  carrier's average authorized accessorial $ per shipment on invoices received in the
  trailing `trailing_days` up to M (config default when there's no history). Every column
  is suffixed `_estimate`.
- **Journal entries** (`outputs/journal_entries.csv`: je_id, date, type, account,
  cost_center, debit, credit, memo):
  - At M: Dr `Freight Expense` (one line per cost center), Cr `Accrued Freight` (total).
  - On day 1 of M+1: the exact reversal.
  - Memo says "ESTIMATE – synthetic data". Posting the invoices to AP is out of scope. In
    the walkthrough notes, explain that the reversal plus the invoice posting produces the true-up.
- **Accuracy**: for the shipments accrued at M, compare the accrual with what they
  eventually cost: `actual_billed` (non-superseded invoices incl. balance due) and
  `actual_payable` (billed − recoverable_estimate, since you owe the right amount, not the
  billed amount). Report error $ and % vs payable by month, split into linehaul+FSC versus
  accessorials. Write `accruals_detail.csv` and `accrual_accuracy.csv`.

**Tests**: every JE balances; each accrual is reversed exactly once in the next month and
the Accrued Freight balance is 0 after reversal; cutoff (delivered on M → accrued;
delivered M+1 → not; invoice received on M → not accrued; received M+1 → accrued); **no
look-ahead** (adding an invoice received after M changes neither the population nor the
accessorial allowance at M); a hand-computed accrual for one LTL and one TL shipment
matches to the cent.

---

## Stage 7: Streamlit dashboard (`streamlit_app.py`)

- On load: read `outputs/` with `st.cache_data`. If outputs are missing, run the pipeline
  with a spinner.
- A persistent banner: **"Synthetic data. Every dollar figure is an estimate from a
  simulated dataset."**
- Tabs:
  1. **Overview**: 4 metrics (total billed freight spend, invoices flagged, recoverable
     $ estimate, recoverable as % of spend); a bar chart by carrier; a bar chart by error
     type.
  2. **Audit quality**: engine vs baseline precision/recall table, the trap false-positive
     table, and the tolerance sweep chart (precision and recall vs tolerance, with the
     recommended point marked).
  3. **Exception queue**: filter by carrier and error type, sortable table, CSV download,
     and a dispute-summary viewer for each carrier.
  4. **Month-end accruals**: accrued vs actual payable by month, accrual error % over time,
     and a JE preview for a selected month.
  5. **Data & assumptions**: the normalization report, normalization exceptions, the diesel
     series with its source, key config values, and a link to ASSUMPTIONS.md.
- Use Altair or Streamlit's built-in charts, with a clean, minimal look and no chart junk.
- **Deploy-ready for Streamlit Community Cloud**: `requirements.txt` with pinned versions;
  no absolute paths; `streamlit_app.py` at the root; a full pipeline run under ~60 s.
- **Test**: `tests/test_app_smoke.py` uses `streamlit.testing.v1.AppTest` to run the app
  and asserts no exceptions.

---

## Stage 8: Docs and summary

- `outputs/summary.json`: every headline number (spend, invoices, flags, recoverable $ and
  %, engine and baseline precision/recall overall and by type, false positives from traps,
  recommended tolerances, accrual MAPE and bias, the systemic issue detected, run date,
  seed).
- **README.md as a one-page memo** (≤ 550 words + one small results table), with every
  number pulled from `summary.json`:
  - **Problem**: where freight spend leaks, and why month-end accruals are hard.
  - **Approach**: generator with traps → normalization → re-rating → rules → evaluation
    vs baseline → accruals.
  - **Results**: precision/recall vs baseline, the traps the engine handles, the systemic
    issue found, and accrual accuracy.
  - **Limitations**: synthetic data (error rates were *injected*, so recoverable $ is
    circular); invented formats; no undercharges; no in-transit accruals.
  - **With real data I would**: EDI 210 and PDF ingestion, calibrating tolerances on dispute
    outcomes, carrier-specific FSC tables, undercharge liability, and learning which flags
    carriers actually accept.
  - Then: live dashboard link (placeholder), how to run in 3 commands, repo map.
- **ASSUMPTIONS.md**: every assumption grouped by area (network, rates, FSC, invoicing,
  errors, traps, audit, accruals, cost estimates), each with its value, why, and its config
  key.
- **Walkthrough notes (kept local, not published)**: a plain-English tour of the pipeline for the author, then **12
  questions a skeptical reviewer would ask, with answers**: why precision matters more
  than recall in disputes; how leakage is prevented; why recall is below 100%; how the
  impacts avoid double counting; why accrue at payable rather than billed; what changes with
  real EDI 210s; why rules instead of ML here; and so on.
- **Findings for outreach**: in `outputs/findings.md`, list 3 candidate one-sentence
  findings about **method**, not about injected error rates, each backed by numbers from
  summary.json. For example: "the naive baseline's duplicate flags were X% false, all
  from rebills and balance-due invoices"; or "accessorials drove Y% of accrual error".

---

## Appendix: config.yaml skeleton

Defaults were sanity-checked. A 1,200 lb LTL shipment over 500 miles costs about $375-425
all-in, and TL runs about $2.10-2.40/mile all-in, for roughly $14M/yr of synthetic spend.

```yaml
seed: 42

period:
  start: 2025-01-01
  months: 12
  runoff_days: 90
  audit_as_of: 2026-03-31

network:
  n_origins: 5            # each origin facility is a cost center
  n_destinations: 20
  n_lanes: 60
  circuity_factor: 1.2

carriers:                  # invented names; format = invoice file layout
  - {id: CARA, name: "Carrier A Freight", mode: LTL, format: A, lag_median_days: 10, error_multiplier: 1.0,
     name_variants: ["CARRIER A FREIGHT", "Carrier A Freight LLC", "Carr A Frt"]}
  - {id: CARB, name: "Carrier B Lines",   mode: LTL, format: B, lag_median_days: 14, error_multiplier: 0.7,
     name_variants: ["CARRIER B LINES", "Carrier B Lines Inc"]}
  - {id: CARC, name: "Carrier C Express", mode: LTL, format: C, lag_median_days: 8,  error_multiplier: 1.4,
     name_variants: ["CARRIER C EXPRESS", "Carrier C Exp.", "C Express"]}
  - {id: CARD, name: "Carrier D Transport", mode: LTL, format: D, lag_median_days: 18, error_multiplier: 1.0,
     name_variants: ["CARRIER D TRANSPORT", "Carrier D Trans"]}
  - {id: CARE, name: "Carrier E Freightways", mode: LTL, format: A, lag_median_days: 12, error_multiplier: 0.8,
     name_variants: ["CARRIER E FREIGHTWAYS", "Carrier E Fwy"]}
  - {id: CARF, name: "Carrier F Trucking", mode: TL, format: B, lag_median_days: 9,  error_multiplier: 1.0,
     name_variants: ["CARRIER F TRUCKING", "Carrier F Trkg"]}
  - {id: CARG, name: "Carrier G Logistics", mode: TL, format: C, lag_median_days: 15, error_multiplier: 1.2,
     name_variants: ["CARRIER G LOGISTICS", "Carrier G Log."]}
  - {id: CARH, name: "Carrier H Carriers", mode: TL, format: D, lag_median_days: 20, error_multiplier: 0.9,
     name_variants: ["CARRIER H CARRIERS", "Carrier H"]}
carriers_per_lane: {LTL: 2, TL: 1}

shipments:
  n: 10000
  tl_share: 0.20
  ltl_weight_lbs: {median: 1200, sigma: 0.8, min: 100, max: 7500}
  tl_weight_lbs: {min: 15000, max: 42000}
  monthly_weights: [0.07, 0.07, 0.08, 0.08, 0.09, 0.09, 0.08, 0.08, 0.09, 0.09, 0.09, 0.09]
  miles_per_day: {LTL: 450, TL: 550}

rates:
  ltl:
    base_cwt: {intercept: 8.0, per_mile: 0.035}   # $/cwt for the 1,000-1,999 lb break
    weight_breaks:
      - {min: 0,    max: 499,  mult: 1.35}
      - {min: 500,  max: 999,  mult: 1.15}
      - {min: 1000, max: 1999, mult: 1.00}
      - {min: 2000, max: 4999, mult: 0.85}
      - {min: 5000, max: 7500, mult: 0.72}
    min_charge: 110.0
  tl: {rate_per_mile: 2.25, min_charge: 450.0}
  carrier_factor_range: [0.90, 1.10]
  lane_noise_range: [0.95, 1.05]
  amendments: {share_of_carrier_lanes: 0.20, effective_date: 2025-07-01, change_range: [-0.06, 0.08]}

diesel:
  source: synthetic                     # synthetic | eia_csv
  eia_csv_path: data/public/eia_diesel_weekly.csv
  synthetic: {start_price: 3.65, weekly_sd: 0.05, mean_reversion: 0.05, long_run_mean: 3.80, min: 3.25, max: 5.75}
  shock: {enabled: true, start: 2025-09-01, weeks_up: 6, total_rise: 0.60, weeks_down: 12}

fsc:
  reference_week: ship_week
  ltl: {base_price: 1.20, step: 0.10, pct_per_step: 0.009}   # ~23% at $3.70, ~32% at $4.80
  tl:  {peg_price: 1.25, mpg: 6.0}                           # ~$0.41/mi at $3.70

accessorials:
  ltl:
    liftgate:    {prob: 0.12, amount: 95.0}
    residential: {prob: 0.06, amount: 125.0}
  tl:
    detention:   {prob: 0.10, hourly: 75.0, free_hours: 2, max_hours: 6}
  late_authorization_share: 0.25

invoicing:
  lag_sigma: 0.6
  max_lag_days: 80
  receipt_delay_days: [0, 5]

errors:                    # share of eligible original invoices (phantom: share of shipments)
  duplicate_invoice: 0.015
  rate_overcharge: 0.03
  fsc_mismatch: 0.025
  unauthorized_accessorial: 0.02
  weight_overbilling: 0.03
  phantom_invoice: 0.005
  sub_tolerance_share: 0.15
  magnitudes:
    rate_markup: [0.03, 0.15]
    rate_sub_tolerance: [0.002, 0.008]
    weight_inflation: [0.05, 0.30]
    weight_sub_tolerance: [0.003, 0.015]
    fsc_wrong_week_offset: [1, 3]
    fsc_wrong_steps: [1, 3]
  systemic_issues:
    - {carrier: CARF, error_type: fsc_mismatch, mode: wrong_week, start: 2025-08-01, end: 2025-10-31, rate: 0.5}

traps:
  rebill_share: 0.01
  balance_due_share: 0.01
  reweigh_share_ltl: 0.015
  bol_format_noise_share: 0.15
  bol_typo_share: 0.003
  rounding_noise_share: 0.10
  rounding_noise_pct: 0.004

normalization:
  charge_code_map:
    "FUEL SURCHARGE": FSC
    "LIFTGATE SERVICE": LIFTGATE
    "RESIDENTIAL DELIVERY": RESIDENTIAL
    "DETENTION - DRIVER": DETENTION
    "LINEHAUL": LH
  fallback_match: {ship_date_days: 1, weight_pct: 0.02}

audit:
  tolerances:
    duplicate_amount: 1.00
    duplicate_window_days: 120
    rate_pct: 0.01
    rate_abs: 2.00
    fsc_ltl_pp: 0.25          # percentage points
    fsc_min_dollars: 1.00
    fsc_tl_abs: 2.00
    weight_pct: 0.02

evaluation:
  sweep:
    rate_pct:   [0.0, 0.0025, 0.005, 0.01, 0.02, 0.03, 0.05]
    fsc_ltl_pp: [0.0, 0.1, 0.25, 0.5, 1.0, 2.0]
    weight_pct: [0.0, 0.005, 0.01, 0.02, 0.03, 0.05]
  min_precision: 0.90
  review_minutes_per_flag: 6       # ESTIMATE
  analyst_cost_per_hour: 45.0      # ESTIMATE, loaded cost
  false_dispute_cost: 25.0         # ESTIMATE, carrier friction per wrong dispute

accruals:
  trailing_days: 90
  default_accessorial_per_shipment: {LTL: 12.0, TL: 8.0}
  accounts: {expense: "6100 Freight Expense", liability: "2150 Accrued Freight"}
```
