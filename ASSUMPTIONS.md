# Assumptions

Every judgment call the spec leaves open, with the reason and the config key that changes it.
Grouped by area, and within an area by the stage that introduced it. `SPEC.md` is the original build plan;
where the build departed from it, the departure is logged here with the reason. Result numbers are not
quoted here: they live in `outputs/` (start with `outputs/summary.json`).

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
  later diesel price is *lower* (a negative "overcharge"), so the observed random wrong-week rate
  is a bit under config in the falling-price months.
- **Several errors on one invoice** are applied in the order weight -> rate -> FSC -> accessorial,
  each building on the last; impacts are additive and follow the Stage 4 decomposition
  (weight, then rate on the billed-weight linehaul, then FSC on the billed linehaul).
- **FSC sub-tolerance** is defined in the audit's own units: LTL adds 0.05-0.20 percentage points
  (tolerance 0.25); TL adds $0.25-$1.75 (tolerance $2.00). *Change:* `errors.magnitudes`.
- **TL `wrong_step`** inflates the diesel price by 5-15%, since TL has no step table.
  *Change:* `errors.magnitudes.fsc_tl_price_inflation`.
- **Systemic issues** take precedence over the base draw: while the *ship date* is inside the
  window (inclusive), an invoice for that carrier and error type gets the systemic mode with
  probability `rate`, and `rate: 1.0` means every invoice does (no base-rate draw can dilute
  it). Invoices outside the window, and the `1 - rate` remainder, use the base rates.
  *Change:* `errors.systemic_issues`.
- **CARF `wrong_table` (the one systemic issue).** From 2025-08-01 to 2025-10-31 CARF (TL) bills
  its fuel surcharge at 5.5 mpg instead of the contract's 6.0, on every original invoice shipped
  in the window: FSC $ = max(0, diesel − peg) / 5.5 × miles, so it is overbilled by a constant
  ~9% of the FSC line. `wrong_table` is a TL-only mode (LTL FSC is a step table, not an mpg), and
  the generator raises if it is configured for an LTL carrier. Exempt by construction: superseded
  originals (errors are never injected there), rebills (they carry true contract charges),
  balance-due invoices (no FSC line), and phantoms. The random `wrong_week` errors are unchanged
  and still occur for every carrier at base rate. *Change:* `errors.magnitudes.fsc_tl_wrong_table_mpg`
  (5.5) and the `systemic_issues` entry.
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
  invoice date; none has the received date, which lives only in `ap_receipt_log.csv`. Wide layouts
  (A, C) state the total once per invoice; long layouts (B `invoice_total`, D `Invoice Total`)
  repeat it on every charge line, as long exports usually do, so the Stage 3 totals check covers
  all four layouts. The stated total is the sum of the rendered lines (including the $0.00
  unmapped line), so on generated data it always ties out; the check exists for real-file damage.
  Files are named by the month the invoice was **received**, and a re-run deletes `data/raw/`
  and `data/ground_truth/` first so stale files never linger.


## Normalization and matching (Stage 3)

- **Layouts live in `normalize.py`, not config, and are detected by exact header match.** The
  set of column names must equal one layout's signature; the folder name is never used to pick a
  parser. A file that matches none is skipped and logged as `unrecognized_header`. *Why:* the
  layouts are what an AP team learns about each carrier's format, not tunable assumptions; a
  parser that imported the generator's `HEADERS` would be circular (and a test forbids it).
  A test checks that each generator header is detected as its own layout.
- **Carrier id comes from the folder, except format C**, whose `Carrier` column is mapped through
  `carriers[].name` and `name_variants`. An unknown name drops the row (`unknown_carrier_name`).
  *Why:* formats A, B, D print no carrier at all, so the source folder is the only evidence.
- **Row dropped vs invoice kept.** An invoice is dropped and logged when its invoice date, ship
  date, weight, carrier, or invoice type cannot be read (nothing downstream can use it). A bad BOL,
  unknown city, unreadable total, missing receipt, or unreadable/unmapped charge line is logged and
  the invoice is kept, with that field blank or that line not loaded. *Change:* the `fatal` mask in
  `parse_file`.
- **`pro_number` is the base PRO**, with the `-C` / `-BD` / `R` / `B` suffix removed after it is read
  as the invoice type. Suffix meaning is per layout: A `-C`/`-BD`, C `R`/`B`. Formats B and D use
  their explicit type column (`original`, `rebill`, `balance_due`; `ORIGINAL`, `REBILL`, `BAL DUE`).
- **Cities resolve against the shipper's own lanes** (`lanes.csv`): lowercase, drop punctuation,
  then try the whole text and again without a trailing two-letter state. Output is the canonical
  city name without state, because format B prints none. A city not in any lane is logged
  (`unknown_city`) and left blank; the invoice can then only match by exact BOL.
- **Fix counts** (`normalization_report.csv`) count only values the normalizer actually changed,
  so a layout that is already clean for a fix (e.g. ISO dates in format B) has no row for it. The
  `unit` column says what is counted: an `invoice`, a `line`, a `date field`, a `city field`, or an
  `amount field`. Two-digit years (format D) read as 20xx.
- **Received date comes only from `ap_receipt_log.csv`** joined on carrier + control id. A missing
  row leaves `received_date` blank and is logged (`missing_receipt`).
- **A charge line whose description is not in `normalization.charge_code_map` is not loaded.** It is
  logged with its raw text and amount (`unmapped_charge`). If it carries dollars, the invoice also
  fails the totals check; the generator's `MISC ADJ` lines are $0.00, so they do not.
- **Totals check** compares the sum of loaded lines with the stated invoice total, within
  `normalization.totals_tolerance` ($0.01, inclusive). A mismatch is logged (`totals_mismatch`) and
  `totals_ok` is false; the invoice is kept. Long layouts repeat the total on every line; if the
  lines disagree, the first is used and `inconsistent_total` is logged. `total` is the *stated*
  total, `lines_total` the recomputed one.
- **Superseding.** Rebills are processed in received order. Each supersedes the latest *earlier*
  (by received date), not-yet-superseded invoice of the same carrier that is not a balance-due: the
  one whose invoice number it cites (B, D) or, when only a suffix marks it (A, C), the one with the
  same base PRO. So a rebill of a rebill supersedes the earlier rebill, and a balance-due invoice is
  never superseded. If nothing qualifies, `rebill_target_not_found` is logged. `supersedes_invoice_number`
  is filled with the target's invoice number for every rebill, including A and C where the file has none.
- **Fallback match weight is within `weight_pct` of the *shipment's* weight, using the billed
  weight.** This is tighter than the generator's phantom guard (which uses the larger of the two
  weights), so a generated phantom can never fall back onto a real shipment. The cost: a typo'd BOL
  on a shipment billed at a certified reweigh weight (5-20% heavier) or an inflated weight cannot
  fall back and stays `unmatched`. In the default run this happens once. Two or more qualifying
  shipments also leave the invoice unmatched. *Change:* `normalization.fallback_match`.
- **Unmatched invoices have a blank `shipment_id`** (NaN once read back), and `match_method` is the
  field to test.


## Re-rating and audit (Stage 4)

- **Audit scope.** Only non-superseded invoices are audited. Rebills are audited like originals
  (rate, fuel, weight, accessorial, phantom, and duplicate checks). Balance-due invoices go through the
  accessorial and phantom checks only: they carry no linehaul or fuel line, and they are a different
  charge on the same shipment, not a copy of it. Unmatched invoices can only be phantom or duplicate
  candidates, since there is no shipment to price. *Change:* not configurable (it is what the checks mean).
- **Known limitation: one typo'd-BOL invoice is called a phantom.** Stage 3's fallback match is
  deliberately tight (weight within `weight_pct` of the shipment weight, billed weight used), so a
  transposed BOL on a shipment billed at a certified reweigh weight cannot fall back and stays
  `unmatched`. The audit then flags it as a phantom and counts its whole total as recoverable, which is
  a false positive. It happens once in the default run and is left as it is: loosening the fallback would
  let generated phantoms match real shipments, and a real AP team would resolve it by hand.
  *Change:* `normalization.fallback_match`.
- **Duplicate pool** is live originals and rebills. The earliest received is kept (received on the same day: the lower `invoice_id` is kept, which is a
  string sort with no business meaning, so a same-day pair is flagged in an arbitrary but repeatable direction); a later copy is a
  duplicate if its total is within `duplicate_amount` (inclusive, compared at cent precision) of an
  earlier copy in the group and it was received within `duplicate_window_days` (inclusive). Groups are the
  matched shipment, or carrier + canonical BOL for unmatched invoices. A repeat of the same carrier +
  invoice number is flagged whatever the amount. *Change:* `audit.tolerances.duplicate_*`.
- **Prices and diesel come from the shipper's shipment record**, not the carrier's printed ship date, so a
  fallback-matched invoice with a date a day off is still priced on the real ship date. Certificates and
  authorizations recorded after `audit_as_of` are ignored, in line with the no-look-ahead rule.
- **Rate rule** compares the *linehaul* gap (billed LH minus contract LH at the billed weight) with
  `max(rate_abs, rate_pct x contract LH)`, strictly greater. The dollar estimate then adds the LTL fuel
  knock-on, `gap x (1 + FSC%)`, so the flag threshold is a linehaul number and the estimate matches the
  Stage 2 impact definition. *Change:* `audit.tolerances.rate_pct`, `rate_abs`.
- **Fuel rule.** LTL: implied FSC % minus expected % greater than `fsc_ltl_pp` points *and* dollar gap
  greater than `fsc_min_dollars`. TL: dollar gap greater than `max(fsc_tl_abs, fsc_tl_pct x expected FSC)`,
  strictly greater. *Why the percentage:* carrier rounding noise is a share of the fuel line, so on a long
  TL lane a flat $2.00 floor flags noise; at `fsc_tl_pct` = 0.005 the allowance is $2.00 up to a $400 fuel
  line and 0.5% above it (just over the generator's +/-0.4% rounding noise). The LTL dollar floor is still
  flat, so a wrong fuel week worth less than the floor is missed by design. Expected FSC is computed on the
  *billed* linehaul so a rate error is never counted a second time. *Change:*
  `audit.tolerances.fsc_*` (`fsc_tl_pct` is also swept in Stage 5).
- **Weight rule** flags when billed weight exceeds the reference weight by more than `weight_pct`
  (LTL only). It has no dollar floor, so a flag can be worth little when the min charge applies.
  *Change:* `audit.tolerances.weight_pct`.
- **Accessorials are checked for presence only**: an authorization for that shipment and code recorded on
  or before `audit_as_of`. The authorized amount is not compared with the billed amount.
- **Only overbilling is flagged.** A negative rate or fuel gap (an undercharge) is never a flag.
- **Recoverable estimate.** Each flag has a `dollar_impact_estimate`. For a duplicate or phantom invoice the
  whole invoice total is recoverable and only that flag counts (`counted_in_recoverable` is false for other
  flags on the same invoice, kept for information). For any other invoice the counted impacts are summed
  and capped at the invoice total. Every dollar figure here is an estimate.
- **Process metric.** Accessorials authorized after the invoice date but before `audit_as_of` are counted in
  `outputs/audit_process_metrics.csv`, not flagged.
- **Baseline** (`audit/baseline.py`) is a spreadsheet pass *designed to lack business context*: it is built to fail on the
  traps, so the point of the comparison is which context matters and how much (the false-flag causes), not that spreadsheets are
  bad. It is not handicapped on string formatting. It runs the *same rule functions and tolerances* as
  the engine on deliberately naive inputs. What it does like an analyst: strip every non-digit
  character from the BOL text and match carrier + those digits to the shipment's BOL (so `BOL#`, `BOL-`,
  spaces, dashes, and case are all handled). What it still gets wrong: no zero-padding and no fallback
  match (a BOL with its leading zeros dropped, or with two digits swapped, is a phantom); no supersession,
  so every invoice is audited; any two invoices with the same carrier + BOL digits are duplicates whatever
  their type; the first rate row per carrier-lane at the shipment weight, ignoring effective dates; no
  reweigh certificates; and authorization asked as of the *invoice* date. It also runs the fuel check, which
  the spec does not list, so it is not handicapped by a missing check. Because it never separates weight
  from rate, a weight-inflated invoice can trip both checks and baseline dollars can overlap. Baseline
  recoverable dollars use the engine's counting rule. *Change:* the shortcuts are code, not config
  (they are the point); tolerances are shared with the engine.


## Evaluation, sweep, exception queue, disputes (Stage 5)

Result numbers are not quoted here; read them from `outputs/` (eval_*.csv, sweep.csv,
recommended_tolerances.json, systemic_findings.csv).

- **Unit of scoring is (invoice_id, error_type).** A flag with a matching error label is a TP, a flag
  without one an FP, and a label without a flag an FN. Several flags of one type on one invoice (e.g. two
  unauthorized accessorial lines) count once. Superseded invoices are dropped from both sides, so the
  baseline's flags on them are not scored (its raw flag count in `baseline_flags.csv` is therefore higher
  than the evaluated count).
- **"Flagged $" counts only flags that count toward recoverable** (`counted_in_recoverable`), so a duplicate
  that also carries a rate flag is not counted twice. It is the audit's *estimate*; "true $" and the TP
  dollars behind dollar recall come from the answer key. Dollar recall = true $ of the TPs / all true $.
- **Trap table.** A false positive is any (invoice, error_type) flag with no matching error label, attributed
  to every trap the invoice carries, so traps that overlap (a BOL-noise invoice that is also on an amended
  lane) share their false positives; the columns are not additive. The last row is the same count on clean
  invoices. `*_fp_by_type` says which rule raised them. Because of the overlap it is kept as detail only: its
  rows add up to more than the false flags, so it is not the table to quote.
- **Baseline false-flag causes** (`outputs/baseline_fp_causes.csv`, `evaluate.baseline_fp_causes`). Each baseline false flag
  is attributed to exactly one cause, so the causes add up to the baseline's false flags (an assertion checks it). The cause
  comes from the rule that raised the flag and the invoice's trap and error labels. Precedence, first match wins:
  (1) rate flag on an invoice carrying the `rate_amendment` trap; (2) rate or weight flag on an invoice carrying
  `documented_reweigh`; (3) rate flag on an invoice with a `weight_overbilling` error label (one weight overbilling raises
  both checks in a baseline that never separates weight from rate); (4) accessorial flag on an invoice carrying
  `late_authorization`; (5) duplicate flag on a `rebill` or `balance_due` invoice; (6) phantom flag on an invoice with the
  `bol_typo` trap or a `bol_format` trap in `zeros_dropped` mode (the other BOL formats are cleaned by the baseline, so they
  cannot make a phantom); (7) anything else, `other`. Precedence only matters for rate flags, the one rule that can reach
  (1), (2) and (3): an amended lane that was also reweighed counts as an amendment. The trap labels are per invoice, not
  per line, so a late-authorization invoice's accessorial flag is credited to late authorization even if it was another
  line that was authorized late. `other` is flags on resent duplicates, which copy an original's BOL trap labels and no
  other, so their amended-lane or reweigh cause is not visible. *Change:* the precedence is code (`FP_CAUSES`, `fp_cause`), not config.
- **Each sweep scores only the rule (and carrier mode) its tolerance governs**: `rate_pct` on
  rate_overcharge, `weight_pct` on weight_overbilling, `fsc_ltl_pp` on fsc_mismatch for LTL carriers, and
  `fsc_tl_pct` on fsc_mismatch for TL carriers. Scoring the whole engine would hide the change under the
  unchanged flags of other rules and make the precision floor meaningless. The current config value is added
  to the grid if it is missing. *Change:* `evaluation.sweep`.
- **Net value** = TP dollars (the answer key's true dollars on TPs) - review cost - false-dispute cost, all
  three costs *estimates* from `evaluation.*`. It assumes every TP is disputed and recovered in full and every
  FP costs `false_dispute_cost`. The *best* point maximizes it subject to precision >= `min_precision` (ties go
  to the point closest to the current setting; if no point meets the floor the most precise one is named and
  the rationale says so).
- **Materiality rule.** The best point is *recommended* only if its net value beats the current setting's by
  MORE than `evaluation.min_material_gain` ($1,000, an estimate); otherwise the recommendation is to keep the
  current value and the rationale says by how little the best point missed. Why: net value is an estimate
  built on estimated costs, and a gain of a few hundred dollars is not a reason to change a control.
  Exception: if the current setting itself is below the precision floor, the floor is a hard constraint and
  the best point is recommended whatever the gain. `sweep.csv` has both `is_best` and `is_recommended`; the
  JSON has `best_point`, `changed` and the two net values. `config.yaml` is never edited.
- **The synthetic data has no weight-measurement noise** (a billed weight is either the shipment weight, a
  documented reweigh, or an injected error), so the weight sweep prefers a zero tolerance and cannot calibrate
  the weight tolerance. The `weight_pct` rationale always carries that caveat. Real scale tickets differ by a
  few pounds. *Change:* `errors.magnitudes.weight_*`, or add a weight-noise trap.
- **Exception queue is one row per flagged invoice**, not per flag, because the invoice is the unit of work
  (one email to the carrier). `error_type` lists every flagged type in rule order joined by `;`, `reason`
  joins the reasons with ` | `, `dollar_impact_estimate` is the gross sum of the invoice's flags, and
  `recoverable_estimate` is the engine's de-duplicated figure. Ranked by recoverable, then earliest received.
  `days_open` runs from the received date to `audit_as_of` (as of the audit date, not today).
- **Dispute pack** = `disputes/<carrier>.md` (period, counts and $ by type, top invoices, systemic check) and
  `.csv` (every flagged invoice for that carrier, ranked within the carrier). "Period" is the range of ship
  dates of the carrier's audited invoices. Per-type dollars are estimates; an invoice with several flags is
  counted under each type. *Change:* `evaluation.top_n_invoices`.
- **Systemic check.** Rolling windows of `window_months` ship months (by the carrier-printed ship date). A
  carrier x error type x window is a finding only if all of these hold: at least `min_invoices` invoices and
  `min_flags` flags in the window; a flag rate at least `multiple` times the rate of the **other carriers of the
  same mode** (an effect-size filter, kept because a huge sample can make a trivial difference "significant");
  and a **one-sided binomial test** (P(X >= flags | invoices, peer rate)) with p <= `alpha` / number of tests
  (Bonferroni; the number of tests is every carrier x error type x window evaluated, so it does not depend on
  which ones look bad). The p-value is computed exactly in log space, with no extra dependency, and is shown
  in the finding text and `systemic_findings.csv`. If the peer rate is 0 the p-value is 0. Per carrier and
  type the most significant window is kept. Departures from the spec's "all-carrier rate": the carrier's own
  flags are left out of the peer rate (otherwise a very bad carrier inflates its own yardstick), and peers are
  the same mode only (weight checks exist only for LTL, fuel is priced differently for TL, accessorials
  differ). Limits of the test: the peer rate is treated as known (its own sampling error is ignored), and
  flags within a window are treated as independent. *Change:* `evaluation.systemic`.
- **Fuel-schedule diagnosis is not attempted.** The template says fuel was billed above schedule and by how
  much on average, and asks for the carrier's fuel table; it does not guess a cause such as a wrong diesel week.


## Month-end accruals (Stage 6)

Result numbers are not quoted here; read them from `outputs/accrual_accuracy.csv` and `outputs/accruals_detail.csv`.

- **Population: delivered, not billed.** At each month-end M the accrual covers shipments with
  `delivery_date <= M` and no matched original or rebill invoice received on or before M. **In-transit
  shipments are not accrued**: expense is recognized at delivery, so a shipment shipped on the 28th and
  delivered on the 3rd belongs to the next month. A shipment stays in the population, and is accrued again,
  at every month-end until an invoice for it is received. A balance-due invoice bills one extra charge, not the
  shipment, so it does not remove a shipment from the population. An unmatched (phantom-looking) invoice
  bills no shipment, so a shipment whose invoice could not be matched stays accrued for good; the default data
  has one such shipment (the typo'd-BOL invoice from the Stage 4 limitation), and accuracy counts it as
  billed $0 (`never_billed_shipments`). *Change:* not configurable (it is what the cutoff means).
- **The one never-billed shipment is the typo'd-BOL reweigh invoice, and it has three knock-ons.** Checked
  against the reference data (not the answer key): shipment SHP06693 (carrier C, BOL 81078675, ship
  2025-09-10, 263 lb, certified reweigh 279 lb on 2025-09-14) and invoice CARC:02574057 (BOL 81076875, two
  adjacent digits swapped, same lane and ship date, billed weight 279 lb) are the same freight. (1) The
  shipment is accrued at every month-end from delivery to the end of the period and never released, because the
  invoice that bills it is `unmatched`. (2) Accuracy counts its actual payable as $0, since `shipment_actuals`
  reads matched invoices only, so its accrual shows up as over-accrual in those months; filter
  `accruals_detail.csv` on a blank `invoices_billed` to see how much. (3) The same invoice is the phantom false
  positive from the Stage 4 limitation, so its whole total sits in the recoverable estimate. In real books AP
  would match it by hand, clearing the accrual and dropping the dispute, so this is a pipeline artifact, not
  a finding about accrual method. It is left in, not patched, for the same reason as the Stage 4 limitation.
- **"Billed at M" uses only what was known at M, including invoices superseded later.** An invoice received on
  or before M bills its shipment even if a rebill received after M replaces it: the shipper had the bill in hand
  at M. Supersession is used only if it was known at M. To support that, normalization records `superseded_on`
  (the received date of the replacing rebill) on the superseded invoice, and the accrual treats an invoice as
  superseded at M only if `superseded_on <= M`. `is_superseded` (the final state) is never read when accruing.
  It is read when measuring accuracy, which looks forward on purpose.
- **Estimate per shipment** = contract linehaul at the shipment's own weight and the rate row effective on
  the ship date + expected fuel surcharge on the ship-week diesel price + an accessorial allowance, each rounded
  to cents (`_estimate` columns). Reweigh certificates are not used: the shipper does not know a shipment will
  be reweighed when it accrues. Diesel is published by the start of the ship week, always before M for a
  delivered shipment, so it never looks ahead.
- **Invoices are booked the day they are received.** The accrual treats an invoice as in the books, and its shipment as
  billed, on `received_date`, with no AP approval or posting lag. A real close would have invoices received but not yet
  posted, which would leave more shipments in the accrual population. *Change:* not configurable; add a lag to the
  "billed at M" test in `billed_shipments`.
- **Accessorial allowance** = the carrier's authorized accessorial dollars per billed shipment over invoices
  *received* in the trailing `accruals.trailing_days` up to M: accessorial charge lines (liftgate,
  residential, detention) whose shipment and code have an authorization recorded on or before M, divided by
  the number of distinct shipments the carrier billed in the window (original and rebill invoices; balance-due
  lines count in the dollars, not the shipments). Invoices already known at M to be superseded are left out.
  Balance-due handling, stated once: a balance-due invoice adds its accessorial dollars to the numerator, but it is not
  counted as a shipment in the denominator, because it bills an extra charge on a shipment an original or rebill invoice
  already counts. (A balance-due line whose original was received outside the window therefore raises the average.) It
  is the only invoice type that can move the numerator without the denominator, and there are few of them.
  A carrier with no invoices in the window gets `accruals.default_accessorial_per_shipment` for its mode. The
  allowance is the same for every shipment of a carrier at M. Because 25% of authorizations are recorded late
  (`accessorials.late_authorization_share`), some real accessorials are not yet "authorized" at M and drop out
  of the history, so the allowance runs low: a real feature of accruing from what the books know at M.
- **Journal entries** (`journal_entries.csv`) are built in whole cents. At M: Dr `6100 Freight Expense`, one
  line per cost center, and Cr `2150 Accrued Freight` for the total (the credit line has no cost center). On
  day 1 of M+1 the exact mirror image, with `je_id` `ACR-YYYYMM` and `REV-YYYYMM`. The December accrual's
  reversal is dated 2026-01-01, after the period ends. Every memo starts "ESTIMATE – synthetic data".
  Posting the invoices to AP is out of scope: the reversal plus the invoice posting is the true-up.
  *Change:* `accruals.accounts`.
- **Accuracy looks forward.** For the shipments accrued at M, `actual_billed` is the sum of their final
  (non-superseded) matched invoices, balance-due included, and `actual_payable_estimate` is billed less the
  audit's recoverable estimate (the shipper owes the right amount, not the billed amount). It is an estimate
  because the recoverable dollars are. A recoverable dollar counts as accessorial if it comes from an
  unauthorized-accessorial flag, as linehaul + fuel if it comes from a rate, fuel or weight flag, and is
  split by the invoice's own lines when the whole invoice is recoverable (duplicate or phantom). Error =
  accrual - payable (positive = over-accrued), as a share of payable. Each month's accuracy covers that
  month's own accrual population, so a shipment accrued at three month-ends appears in three months.
  `accuracy_summary` reports MAPE (mean absolute monthly error %) and bias (mean signed monthly error %).
  Errors from injected errors the audit did not flag (below tolerance) stay in payable, by design.
- **Late-authorization sensitivity** (`outputs/accrual_sensitivity.csv`, computed in `accruals.py`). The same
  accruals are rerun on books where every authorization was recorded on the shipment's delivery date
  (`authorizations_at_delivery`), using the same no-look-ahead rules: the allowance still counts only invoices
  received and authorizations recorded on or before M. Which shipment-and-code pairs are authorized is unchanged,
  only when. The population and the "actual payable" side are identical in both runs (the audit is not
  rerun), so the only thing that moves is the accessorial allowance, and `late_auth_effect_estimate` is the
  scenario accrual minus the built accrual. The table gives error and error % overall and for accessorials
  alone, as built and in the scenario, for each month and an ALL row. *Change:* `accessorials.late_authorization_share`
  sets how much paperwork is late in the generated data.
- **Shipment-level accrual error** (`accuracy_by_month`, columns `shipment_*` in `accrual_accuracy.csv`). Monthly error is a net
  figure, so shipments over- and under-accrued cancel. The shipment columns take |accrual - payable| / payable for every accrued
  shipment-month with a payable above $0 (never-billed shipments have no percentage and are left out), and report the mean,
  the median, and the share above `accruals.large_shipment_error_pct` (0.10, an arbitrary line for "a large miss"). A
  shipment accrued at three month-ends is three rows. All three are estimates, because payable is.
- **Linehaul and fuel accrual accuracy is close to exact by construction.** The generator, the audit and the accrual all price
  from `freight_audit_lab/contract.py`, so a shipment's accrued linehaul and fuel differ from its billed contract charges
  only through weight (reweighs are not known at M), rate rows, and injected errors. It shows the pipeline is coherent; it does
  not show the method would be this accurate on a real book. The documents say so.

## Dashboard (Stage 7)

Result numbers are not quoted here; the dashboard reads them from `outputs/` and so should you.

- **Every number on the Overview tab is a sum, ratio or lookup over an output file** (`freight_audit_lab/dashboard.py`),
  and the three "Key findings" bullets are templates filled from those files, so a rerun changes the page with no
  edit. *Billed spend* is what the audited (non-superseded) invoices billed, duplicates and balance-due invoices
  included (`audit_invoice_summary.csv`). *False disputes avoided* is the baseline's false positives minus the
  engine's, at the (invoice, error type) level, from the ALL row of `eval_engine_vs_baseline.csv`. *Recoverable
  estimate* is the sum of `recoverable_estimate` in `audit_invoice_summary.csv`; it equals the sum of the engine's
  counted dollars by error type, which the by-type chart shows.
- **Systemic bullet** picks the finding with the smallest p-value in `systemic_findings.csv` and says how many other
  patterns were flagged. It is not hard-coded to CARF. **False-flag bullet** picks the cause with the most baseline false flags in `baseline_fp_causes.csv` (never `other`) and quotes
its share of the additive total. The one clause explaining *why* the baseline trips (for example, "ignores effective dates") is a
fixed sentence per cause (`CAUSE_WHY`) taken from the baseline's documented shortcuts above; a cause without an entry gets no
explanation. The Audit quality tab charts the causes and keeps the overlapping trap table below it as detail. **Accrual bullet** uses the ALL
  rows of `accrual_accuracy.csv` and `accrual_sensitivity.csv`: the accessorial share is accessorial error over the
  net (signed) error, and the *late-authorization share* is the sensitivity's accrual change over the net error. Shares
  are of the net error over the whole period, so months of opposite sign net against each other; the mean absolute
  monthly error is quoted next to it. The headline wording ("Accessorials drive...") switches to linehaul and fuel if
  accessorials are not more than `accruals.accessorial_driver_share` (0.5, half) of the net error.
- **The Data & assumptions tab is the one place that reads outside `outputs/`**: the normalization report and exceptions
  (`data/normalized/`), the diesel series (`data/reference/diesel_weekly.csv`) and `config.yaml`. They are pipeline
  inputs and diagnostics, not results. Carrier names come from `config.yaml`. The dashboard never reads
  `data/ground_truth/`.
- **Sweep chart.** One tolerance at a time, x-axis in the tolerance's own unit (percent, or percentage points for the
  LTL fuel check), points equally spaced whatever the grid gaps. A ring marks the current setting and the recommended
  one (often the same point, because of the materiality rule); the dashed line is `evaluation.min_precision`. The
  caption is the rationale text from `recommended_tolerances.json`.
- **Deploying.** `outputs/` is committed, so Streamlit Community Cloud serves it as is; if `outputs/` is missing the
  app runs the full pipeline once behind a spinner. Colours are one accent plus greys (light theme set in
  `.streamlit/config.toml`); engine and "as built" are the accent, baseline and what-if are grey.
- **Polish pass after the first review.** The page uses Streamlit's wide layout with the content capped at 1,500 px, so a
  laptop shows the full tables and an ultra-wide monitor stays readable. Bar charts reserve 64 px to the right of the plot
  (`LABEL_ROOM` in `charts.py`) so the value printed after the longest bar is never clipped. The exception queue's Reason
  column is 1,000 px wide (about 140 characters, the median reason), so it is the last column to scroll to; the longest
  multi-flag reasons still run past it, and the CSV download has them in full. The dispute viewer opens on the carrier with
  the smallest p-value in `systemic_findings.csv` (`strongest_systemic_carrier`), and the pack's Markdown headings are
  turned into bold lines so they do not out-shout the page. Journal amounts are shown as text with blank cells, because
  a null number cell prints as "None". Streamlit's toolbar and Deploy button are hidden with `client.toolbarMode = "minimal"`
  in `.streamlit/config.toml`. The byline links the author's LinkedIn and the "Code on GitHub" link
  (`LINKEDIN_URL`, `GITHUB_URL` in `streamlit_app.py`).


## Cost estimates (Stage 5)

Gathered here because the spec lists them as their own area. All are estimates and none is a measurement.

- **Review cost of a flag** = `evaluation.review_minutes_per_flag` (6) minutes at `evaluation.analyst_cost_per_hour` ($45,
  loaded). *Why:* a flag has to be opened, checked against the contract, and written up; six minutes is a plausible
  desk figure, not a benchmark.
- **Cost of a wrong dispute** = `evaluation.false_dispute_cost` ($25), carrier friction on top of the review time. *Why:*
  a rejected dispute costs goodwill and a follow-up, which the review minutes do not cover.
- **Precision floor** `evaluation.min_precision` (0.90) and **materiality threshold** `evaluation.min_material_gain`
  ($1,000) decide which tolerance the sweep recommends (see the Stage 5 notes above).
- **Accrual allowance defaults** `accruals.default_accessorial_per_shipment` ($12 LTL, $8 TL) are used only for a carrier
  with no invoices in the trailing window.


## Summary and documents (Stage 8)

Result numbers are not quoted here; read them from `outputs/summary.json`.

- **`outputs/summary.json` has no run date.** The spec lists one, but rule 5 says the same config gives byte-identical
  outputs, and a wall-clock date would change the file on every run. It carries `seed` and `audit_as_of` instead.
  *Change:* not configurable (it is what reproducibility means).
- **The summary is built only from `outputs/`.** Precision, recall, false positives and the answer-key dollars come from
  the `eval_*.csv` files that `evaluate.py` wrote; `summary.py` adds the normalized invoice table (counts and ship dates)
  and a handful of config values (cost estimates, systemic-test settings), and never opens the answer key.
- **README.md, WALKTHROUGH.md and `outputs/findings.md` are generated.** They are templates in `docs/`, with
  `{{path|format}}` placeholders filled from `summary.json` by `docs.py` at the end of every pipeline run. A placeholder
  that does not resolve stops the run, so a document cannot quote a number the summary lacks, and a rerun rewrites every
  quoted number. *Why:* rule 8 (never invent results) enforced by code, not by care. *Edit the template, not the output.*
  A test checks that the committed documents equal a fresh render.
- **Some sentences are still static prose whose truth depends on the config.** The main one is "a weaker pattern I did not
  plant" in the systemic-test paragraphs: only one systemic issue is configured (CARF), so any second finding is not planted.
  If `errors.systemic_issues` changes, reread the templates.
- **"Below its own tolerance"** (the engine's misses) is the count of injected errors whose mode is `sub_tolerance` in
  `eval_by_mode.csv`; the remaining misses are listed by type and mode in `engine_misses.other_detail`. Missed dollars are
  answer-key dollars, not estimates, and are labeled that way.
- **Baseline duplicate false flags "explained by" rebills and balance-due invoices** is the false duplicate flags attributed to
  the rebill / balance due cause in `baseline_fp_causes.csv`, divided by all false duplicate flags. (An earlier version summed the
  per-trap counts from `eval_traps.csv` and capped the result at 100%, which hid the overlap.)
- **False flags by cause in the README and WALKTHROUGH question 9** come from `baseline_fp_causes.csv` through `summary.json`
  (`baseline_fp_causes`), not from the overlapping trap table.
- **Accrual versus billed** (used in WALKTHROUGH question 6) is (accrual - `actual_billed`) / `actual_billed` over the same
  shipment-month population as the payable comparison. It is an estimate because the accrual is.
- **README length** is at most 550 words of prose, not counting the results table or the code block; a test enforces it.
- **One p-value style** (`exceptions.p_text`, used by the docs, the dashboard and the dispute packs): "p < 1e-300" below that
  value (an exact 1e-320 is a subnormal float and means nothing), otherwise "p = 1.6e-05". `systemic_findings.csv` and `summary.json`
  keep the raw p-value.
- **Sweep wording.** The docs say no tolerance change clears the materiality bar and quote the largest gain
  (`recommended_tolerances.largest_gain_estimate` in `summary.json`). That sentence is static prose: if a rerun ever recommends a
  change, reread the README, WALKTHROUGH question 4 and `findings.md`. The bar is a judgment, not a finding.
- **"Accruals" totals.** `accrual_estimate` in `accrual_accuracy.csv` and `summary.json` is the sum of the month-end balances, so
  a shipment still unbilled at the next month-end is counted again. The documents call it that and never as one accrual amount.
- **Author details** (LinkedIn, GitHub) are in `docs/README.template.md` and `GITHUB_URL` / the byline in `streamlit_app.py`.
  The live dashboard link (https://shreyansh-freight-audit-lab.streamlit.app) is in `docs/README.template.md`; it was filled in after deploying to Streamlit Community Cloud.
