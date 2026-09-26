# Assumptions

Every judgment call the spec leaves open, with the reason and the config key that changes it.
Grouped by area. Stage 8 will tidy this into its final form.

## General

- **Pinned dependency versions** are the ones installed at Stage 1 (pandas 3.0, numpy 2.5,
  Streamlit 1.64). *Why:* reproducible installs on Streamlit Cloud. *Change:* `requirements.txt`.
- **One generator, drawn in a fixed order.** A single `numpy.random.default_rng(seed)` is
  created once and passed through network → rates → diesel → shipments → accessorials →
  reweighs, then on through Stage 2 (originals -> follow-ups -> errors -> duplicates ->
  phantoms -> ids -> rendering). Later stages draw after earlier ones, so adding Stage 2 did
  not change Stage 1 files, with one exception: the late-authorization trap rewrites some
  `authorized_at` dates in `authorizations.csv`. *Change:* `seed`.
- **Every ID column is read as text** (`bol`, `pro_number`, `invoice_number`, `control_id`,
  `shipment_id`, ...) through `freight_audit_lab/csv_io.py`; about 10% of BOLs, and every
  invoice number, start with a zero. Nothing else may call `pd.read_csv` (a test enforces it).
- **Pricing lives in `freight_audit_lab/contract.py`**, not under `generate/`, so audit and
  accrual code never import the generator (a test enforces it). Money is rounded by one helper,
  `cents()`, which rounds a plain Python float: `round()` on a numpy float can differ by a cent
  from Python's at exact half-cent ties (e.g. 78.325), and the generator and re-rater must agree.
- **Dates in CSVs are ISO `YYYY-MM-DD`** in `data/reference/`. Messy formats appear only in
  the Stage 2 raw carrier files.

## Network

- **Cities come from a fixed list in `network.py`** (real US cities, approximate lat/lon,
  public knowledge). Origins are the first `n_origins` of the origin pool and destinations the
  first `n_destinations` of the destination pool; origins and destinations never overlap, so
  every lane has positive miles. *Why:* coordinates are facts, not tunable assumptions, and a
  fixed order is easy to explain. *Change:* `network.n_origins`, `network.n_destinations`
  (validated against the pool sizes).
- **Lanes** are `n_lanes` distinct origin-destination pairs sampled without replacement from
  all pairs. Some destinations may be served from only one or two origins. *Change:*
  `network.n_lanes`.
- **Carrier-lane assignment:** each lane picks `carriers_per_lane.LTL` LTL carriers and
  `carriers_per_lane.TL` TL carriers uniformly without replacement, so carriers end up with
  different lane counts. *Change:* `carriers_per_lane`.
- **`carriers.csv` omits `error_multiplier` and `lag_median_days`.** The shipper knows who
  its carriers are, not how often the simulator makes them err. Writing the multiplier to
  `data/reference/` would leak injection metadata. *Change:* not configurable (leakage rule).

## Rates

- **Carrier factor is one draw per carrier; lane noise is one draw per carrier-lane.**
  *Change:* `rates.carrier_factor_range`, `rates.lane_noise_range`.
- **TL rates also get the carrier factor and lane noise.** The spec only says so for LTL,
  but identical TL rates across carriers would be unrealistic. The TL minimum charge stays
  at the config value. *Change:* set both ranges to `[1.0, 1.0]` to switch the noise off.
- **The rate card stores one $/cwt column per weight break** (`cwt_0_499`, `cwt_500_999`, ...),
  already multiplied out and rounded to cents, plus `rate_per_mile` for TL and `min_charge`.
  *Why:* `contract_linehaul(rate_row, weight_lbs, miles)` then needs nothing but the row,
  which is how a real contract reads, and pricing lives in one place. *Change:*
  `rates.ltl.weight_breaks`.
- **Weight-break lookup:** the break with the largest `min` ≤ weight applies. Weights above
  the last break's `max` (possible after a reweigh or inflation) use the last break.
- **Deficit weight rating is applied (spec gap).** Plain weight breaks make a 999 lb shipment
  cost more than a 1,000 lb one (999 × 1.15 > 1,000 × 1.00). Real LTL tariffs fix this by
  charging the lower of the actual-weight charge and the charge at the next break's minimum
  weight. Without it, inflating a weight across a break can *lower* the bill, which gives
  negative "weight overbilling" impacts. Linehaul is therefore
  `max(min_charge, min(as-weight charge, deficit charge))`. *Change:*
  `rates.ltl.deficit_weight_rating: false` for the spec's literal formula.
- **Amendments** hit `round(share_of_carrier_lanes × carrier-lanes)` carrier-lanes (LTL and
  TL alike), sampled without replacement. The old row ends the day before `effective_date`;
  the new row starts on it. Every $/cwt break (or the TL $/mile) scales by the same draw from
  `change_range`; the minimum charge does not change. A `version` column (1 = original,
  2 = amended) is included because the shipper knows its own contract history. *Change:*
  `rates.amendments`.
- **Open-ended rate rows end 2099-12-31** rather than blank, so lookups never deal with
  missing dates. First rows start on `period.start`. *Change:* `rates.open_ended_to`.
- **Linehaul is rounded to cents once**, after `rate × weight / 100` (or `rate × miles`).

## Diesel and FSC

- **Diesel weeks:** the first Monday is 4 weeks before the Monday on or before
  `period.start`; the last is the last Monday on or before `period end + runoff_days`.
- **Synthetic walk:** `p[t+1] = p[t] + mean_reversion × (long_run_mean − p[t]) + N(0, weekly_sd)`.
  The **shock is an overlay** on the walk (added after, not fed into it, so mean reversion
  doesn't erase it): it rises linearly by `total_rise` over `weeks_up` weeks from the first
  Monday on or after `shock.start`, then falls linearly back to zero over `weeks_down` weeks.
  The sum is clipped to [min, max] and rounded to 3 decimals (EIA publishes 3). *Change:*
  `diesel.synthetic`, `diesel.shock`.
- **Source naming:** config says `synthetic | eia_csv`; the `source` column in
  `diesel_weekly.csv` says `synthetic` or `eia`, as the spec asks.
- **EIA loader:** a row counts as data when its first cell parses as a date and its second
  as a number. Anything else (titles, source keys, blank lines, the column header) is skipped.
  Dates are snapped to the Monday on or before (EIA occasionally publishes on a Tuesday after
  a holiday). A week missing inside the covered range carries the last published price
  forward, which uses only prices published on or before that week. It fails if the file
  starts after the first required Monday or ends before the last one. *Change:*
  `diesel.source`, `diesel.eia_csv_path`.
- **Floating-point guard on the LTL step:** `(diesel − base) / step` is rounded to 6 decimals
  before `floor`, because e.g. `(3.30 − 1.20) / 0.10` is `20.999…` in floating point and
  would drop a step. *Change:* not configurable (arithmetic correctness).
- **FSC is not rounded mid-way.** LTL FSC $ = round(linehaul × FSC%, 2); TL FSC $ =
  round(max(0, (diesel − peg) / mpg) × miles, 2).
- **`fsc.reference_week` only supports `ship_week`**; config validation rejects anything else.
- **`fsc_ltl_table.csv`** lists the 10-cent bands from the lowest to the highest diesel price
  in the series actually used, with the FSC % for each.

## Shipments

- **Monthly counts use largest-remainder rounding** of `n × monthly_weights`, so they sum to
  exactly `n` and don't vary with the seed. Within a month, ship dates are uniform over all
  calendar days (weekends included, for simplicity). *Change:* `shipments.monthly_weights`.
- **Weights are whole pounds.** LTL: lognormal with `log(median)` and `sigma`, clipped to
  [min, max]. TL: uniform on [min, max]. *Change:* `shipments.ltl_weight_lbs`,
  `shipments.tl_weight_lbs`.
- **Transit days** = `ceil(miles / miles_per_day[mode])` + an integer drawn from `transit_extra_days` (0 or 1).
  Every lane has positive miles, so delivery is always after ship. *Change:*
  `shipments.miles_per_day`, `shipments.transit_extra_days`.
- **BOL numbers** are drawn without replacement from 00000000-99999999, so about 10% start
  with a zero, which the Stage 2 "leading zeros dropped" noise needs.
- **Shipment ids** (`SHP00001`, ...) follow ship-date order.
- **Cost center** = the origin facility's cost center code (`CC-101` ... `CC-105`).

## Accessorials and authorizations

- **LTL liftgate and residential are independent draws**, so a shipment can have both.
- **TL detention:** total hours are a whole number drawn uniformly from `free_hours + 1` to
  `max_hours`, so billable hours are 1 to `max_hours − free_hours` and every detention is
  billable. `max_hours` is read as the maximum *total* hours. *Change:*
  `accessorials.tl.detention`.
- **authorized_at** is uniform between ship date and delivery date inclusive. Stage 2 moves
  some past the invoice date for the late-authorization trap.
- **`authorizations.csv` is the only record of legitimate accessorials** (no separate
  accessorials file). The canonical invoices in Stage 2 bill from it.

## Reweigh certificates

- **The reweigh factor range [1.05, 1.20] moved into config** (`traps.reweigh_factor_range`),
  because the spec states it as a number and the rules forbid magic numbers in code.
- **Certified weight is rounded to whole pounds; certified_at** is uniform between ship date
  and delivery date (the carrier reweighs at a terminal in transit).
- **Reweigh shipments are sampled by count:** `round(reweigh_share_ltl × LTL shipments)`.
  *Change:* `traps.reweigh_share_ltl`.

## Spend sanity check

- The appendix says roughly $14M a year of synthetic spend. The actual figure depends on the
  city list (lane lengths). Stage 1 prints the contract linehaul + FSC total so the check is
  visible rather than assumed; it is not tuned to hit $14M.


## Invoices (Stage 2)

- **One original invoice per shipment**, billed at the reference weight (certified reweigh weight
  if a certificate exists, else shipment weight): contract LH + FSC + every authorized accessorial.
- **Invoice lag** = round(lognormal(median = carrier `lag_median_days`, `lag_sigma`)), clipped to
  [1, `max_lag_days`]. Received = invoice date + a uniform integer from `receipt_delay_days`.
  Anything that would land after the runoff window closes (period end + `runoff_days`, which
  equals `audit_as_of`) is pulled back to its last day, so a handful of late-December
  shipments pile onto the final day. *Change:* `invoicing`.
- **Follow-up invoices (duplicate, rebill, balance due) are only drawn where they can arrive
  before runoff ends**, so those counts are slightly below `share x originals` for late months.
- **Invoice numbers** are 7-digit zero-padded per-carrier sequences in invoice-date order; a
  resent duplicate reuses its original's number. **PRO numbers** are 9 random digits shared by an
  invoice and its follow-ups. **Control ids** are 8 random digits, unique across all carriers.
- **Errors are injected on non-superseded originals only.** A rebilled original is excluded, since
  it is replaced anyway.
- **Injection is Bernoulli per invoice** with probability `errors.<type> x error_multiplier`
  (phantoms: one chance per shipment slot, using that shipment's carrier). Mode shares:
  `sub_tolerance_share` sub-tolerance; otherwise `stale_rate_share` stale-rate (only where the
  lane's amendment lowered the rate and the shipment is after it) else markup; FSC splits
  `fsc_wrong_week_share` / rest wrong_step; weight is inflation or sub-tolerance. Very few
  invoices qualify for `stale_rate` (about 4 in the default run). *Change:* `errors`.
- **Zero-impact rule = re-draw, then skip.** A magnitude is re-drawn up to `errors.max_redraws`
  times until impact > $0.01, then the error is skipped. This also skips wrong-week draws whose
  later diesel price is *lower* (a negative "overcharge"), so the observed FSC rate is a bit under
  config and systemic wrong-week errors fade in the falling-price months.
- **Several errors on one invoice** are applied in the order weight -> rate -> FSC -> accessorial,
  each building on the last; impacts are additive and follow the Stage 4 decomposition
  (weight, then rate on the billed-weight linehaul, then FSC on the billed linehaul).
- **FSC sub-tolerance** is defined in the audit's own units: LTL adds 0.05-0.20 percentage points
  (tolerance 0.25); TL adds $0.25-$1.75 (tolerance $2.00). *Change:* `errors.magnitudes`.
- **TL `wrong_step`** inflates the diesel price by 5-15%, since TL has no step table.
  *Change:* `errors.magnitudes.fsc_tl_price_inflation`.
- **Systemic issues** override the probability of that one mode for that carrier while the
  *ship date* is inside the window (inclusive); other modes keep their base rates.
- **Duplicates come only from originals with no other injected error**, and each duplicate is a
  copy of the original's billed lines, so its label is just `duplicate_invoice` (Stage 4 tests
  the "duplicate that also has a rate error" case on hand-built fixtures). A resend keeps the
  original's invoice date; a re-keyed copy gets a new one.
- **Phantoms** are priced like a normal invoice (contract LH + FSC, no accessorials) at a random
  date and weight on one of the carrier's lanes. Up to `4 x max_redraws` attempts to avoid a
  fallback match, then skipped.
- **Unauthorized accessorial** picks a code the shipment has no authorization for at all;
  detention draws 1 to (`max_hours` - `free_hours`) billable hours. Its `mode` label is the code.
- **Rebill:** the rebill carries the true contract charges and supersedes the original, which
  overstated linehaul by `traps.rebill_original_overstate` (FSC recomputed on it). The rebill
  inherits the original's `documented_reweigh`, `rate_amendment`, and BOL-noise labels.
  Superseded originals carry only the `superseded` label.
- **Balance due:** the original leaves out one authorized accessorial; the supplemental invoice
  bills only that line. Its authorization is excluded from the late-authorization trap.
- **Late authorization:** for each authorization (probability `late_authorization_share`) on a
  non-superseded, non-balance-due invoice dated before `audit_as_of`, `authorized_at` becomes
  invoice date + 1 to `late_authorization_max_days` days, never past `audit_as_of`.
- **BOL noise** has six patterns (zeros dropped, `BOL-`, `BOL#`, spaces, dashes, lowercase); the
  zeros-dropped pattern is only used on BOLs that start with 0. A typo swaps two adjacent
  different digits and is re-picked so it is not another shipment's BOL. Duplicates, rebills,
  and balance-due invoices repeat the original's BOL text and its BOL trap labels; no other trap
  label is copied to a duplicate.
- **Rounding noise** hits LH, FSC, or both (equal chance) with a uniform factor in
  +/- `rounding_noise_pct`, on top of any error, on non-superseded originals. Its dollars are
  *not* part of any true impact. *Change:* `traps.rounding_noise_*`.
- **Unmapped charge lines** are `MISC ADJ` at $0.00 on `traps.unknown_charge_lines` format D
  originals, so invoice totals and impacts stay exact while normalization still has to report
  them. *Change:* `traps.unknown_charge_lines`, `traps.unknown_charge_description`.
- **Ground truth** has one `clean` row for an invoice with no error, trap, or supersession.
  Trap rows have impact 0. `mode` is the sub-type: BOL pattern, accessorial code, rounding
  target (`lh`, `fsc`, `lh+fsc`), or blank.
- **Raw layouts.** Format A dates `MM/DD/YYYY`, pro suffix `-C`/`-BD`; B ISO dates, weight in cwt
  (2 decimals, exact for whole pounds), explicit `invoice_type` and `supersedes`; C timestamps
  (midnight), `$1,234.56` amounts, `1,250 LB` weights, one of the carrier's name variants, pro
  suffix `R`/`B`; D `04-Mar-25` dates, `BOL#` prefix, uppercase-and-punctuation-inconsistent cities
  (always recoverable by lowercasing and dropping punctuation), explicit type in caps
  (`ORIGINAL`/`REBILL`/`BAL DUE`) and an `Orig Invoice Ref` column for rebills. Every layout has an
  invoice date; none has the received date, which lives only in `ap_receipt_log.csv`. Long
  layouts (B, D) have no stated total column, so the totals check applies to A and C only.
  Files are named by the month the invoice was **received**, and a re-run deletes `data/raw/`
  and `data/ground_truth/` first so stale files never linger.
