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
- **Duplicate pool** is live originals and rebills. The earliest received is kept; a later copy is a
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
- **Baseline** (`audit/baseline.py`) is a *careful spreadsheet* pass, not a strawman: the comparison is meant
  to be about business logic, not string formatting. It runs the *same rule functions and tolerances* as
  the engine on deliberately naive inputs. What it does like a careful analyst: strip every non-digit
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
