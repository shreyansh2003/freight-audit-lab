"""Traps: legitimate oddities that look like billing errors but are not.

A rule that flags every injected error is easy. The traps are what make the audit honest:
a corrected rebill, a supplemental balance-due invoice, a documented reweigh, an authorization
recorded late, a BOL with formatting noise, and carrier rounding must all be left alone.
(`documented_reweigh` and `rate_amendment` are applied while building the originals.)
"""

import copy

import pandas as pd

from freight_audit_lab.contract import cents, fsc_ltl_amount
from freight_audit_lab.generate.invoices import get_line, new_invoice, period_bounds

BOL_PATTERNS = ["zeros_dropped", "prefix_dash", "prefix_hash", "spaces", "dashes", "lowercase"]
BOL_TRAP_LABELS = ("bol_format", "bol_typo")   # copied onto invoices that repeat the same BOL text
REBILL_KEEPS = ("documented_reweigh", "rate_amendment") + BOL_TRAP_LABELS


def days(n):
    return pd.Timedelta(days=int(n))


def bol_traps(inv):
    """The BOL-noise trap labels on an invoice."""
    return [t for t in inv["traps"] if t[0] in BOL_TRAP_LABELS]


def add_derived_invoice(invoices, orig, invoice_type, lines, lag_range, cfg, rng):
    """Append an invoice for the same shipment received `lag_range` days after the original.

    It repeats the original's shipment details and BOL text. Its invoice date is the day it
    was sent, a few days before the AP stamp, and never before the original's.
    """
    lo, hi = lag_range
    received = orig["received_date"] + days(rng.integers(lo, hi + 1))
    d_lo, d_hi = cfg["invoicing"]["receipt_delay_days"]
    sent = max(received - days(rng.integers(d_lo, d_hi + 1)), orig["invoice_date"])
    keep = ("carrier_id", "shipment_id", "pro_base", "bol_digits", "bol_raw", "bol_noise", "bol_typo",
            "ship_date", "origin_city", "origin_state", "dest_city", "dest_state", "weight_lbs")
    inv = new_invoice(len(invoices), orig["carrier_id"], invoice_type=invoice_type,
                      parent_uid=orig["uid"], invoice_date=sent, received_date=received,
                      lines=lines, **{k: orig[k] for k in keep if k != "carrier_id"})
    inv["traps"] = bol_traps(orig)
    invoices.append(inv)
    return inv


# ------------------------------------------------------------ choosing what gets a follow-up


def select_followups(invoices, ctx, cfg, rng):
    """Pick the originals that will be superseded by a rebill and those that get a balance-due.

    Counts are `share x originals`. A candidate must have room for its follow-up to arrive
    before the runoff window closes. A balance-due original needs an authorized accessorial:
    the original omitted it and the supplemental invoice bills it later. A rebilled original
    is superseded, so it is excluded from error injection (logged in ASSUMPTIONS).
    """
    n = len(invoices)
    _, _, runoff_end = period_bounds(cfg)
    inv_cfg, t = cfg["invoicing"], cfg["traps"]

    def room(inv, lag_range):
        return inv["received_date"] + days(lag_range[1]) <= runoff_end

    pool = [i for i in range(n) if room(invoices[i], inv_cfg["rebill_lag_days"])]
    chosen = rng.choice(pool, size=min(round(t["rebill_share"] * n), len(pool)), replace=False)
    for i in sorted(chosen):
        invoices[i]["superseded"] = True

    pool = [i for i in range(n) if not invoices[i]["superseded"] and ctx[i]["auth_codes"]
            and room(invoices[i], inv_cfg["balance_due_lag_days"])]
    chosen = rng.choice(pool, size=min(round(t["balance_due_share"] * n), len(pool)), replace=False)
    for i in sorted(chosen):
        codes = sorted(ctx[i]["auth_codes"])
        line = get_line(invoices[i], codes[int(rng.integers(len(codes)))])
        invoices[i]["lines"].remove(line)              # the original leaves this charge out
        ctx[i]["balance_due_line"] = line


def build_rebills(invoices, ctx, cfg, rng):
    """A corrected invoice supersedes each selected original.

    The rebill carries the true contract charges. The original it replaces overstated
    linehaul by a small fraction (carrier corrected its own mistake before payment); its fuel
    surcharge is recomputed on the overstated linehaul, as the carrier's system would.
    """
    lo, hi = cfg["traps"]["rebill_original_overstate"]
    for i in [i for i in range(len(invoices)) if invoices[i]["superseded"]]:
        orig = invoices[i]
        rebill = add_derived_invoice(invoices, orig, "rebill", copy.deepcopy(orig["lines"]),
                                     cfg["invoicing"]["rebill_lag_days"], cfg, rng)
        rebill["traps"] = [("rebill", "")] + [t for t in orig["traps"] if t[0] in REBILL_KEEPS]
        lh, fsc = get_line(orig, "LH"), get_line(orig, "FSC")
        lh[1] = cents(lh[1] * (1 + rng.uniform(lo, hi)))
        if ctx[i]["shp"].mode == "LTL":
            fsc[1] = fsc_ltl_amount(lh[1], ctx[i]["diesel"], cfg)


def build_balance_dues(invoices, ctx, cfg, rng):
    """A later supplemental invoice for the same BOL carrying only the omitted accessorial."""
    for i in sorted(i for i in ctx if "balance_due_line" in ctx[i]):
        bd = add_derived_invoice(invoices, invoices[i], "balance_due",
                                 [list(ctx[i]["balance_due_line"])],
                                 cfg["invoicing"]["balance_due_lag_days"], cfg, rng)
        bd["traps"] = [("balance_due", "")] + bd["traps"]


# ------------------------------------------------------------ authorization timing


def apply_late_authorization(invoices, ctx, auths, cfg, rng):
    """For a share of legitimate accessorials, record the authorization after the invoice date.

    The accessorial was really approved (it is in authorizations.csv) but paperwork caught up
    only after the carrier billed. Audit must check authorization as of `audit_as_of`, not as
    of the invoice date. Returns the authorizations with the new authorized_at dates.
    """
    a = cfg["accessorials"]
    audit_as_of = pd.Timestamp(cfg["period"]["audit_as_of"])
    by_shipment = {inv["shipment_id"]: inv for inv in invoices}
    hit, offset = rng.random(len(auths)), rng.random(len(auths))
    auths = auths.copy()
    for pos, row in enumerate(auths.itertuples(index=False)):
        inv = by_shipment[row.shipment_id]
        skip = (hit[pos] >= a["late_authorization_share"] or inv["superseded"]
                or ctx[inv["uid"]].get("balance_due_line", [None])[0] == row.code)
        room = min(a["late_authorization_max_days"], (audit_as_of - inv["invoice_date"]).days)
        if skip or room < 1:
            continue
        auths.at[pos, "authorized_at"] = inv["invoice_date"] + days(1 + int(offset[pos] * room))
        inv["traps"].append(("late_authorization", row.code.lower()))
    return auths


# ------------------------------------------------------------ text and amount noise


def noisy_bol(digits, pattern):
    """The BOL as a carrier's system might print it: same digits, different dressing."""
    return {"zeros_dropped": digits.lstrip("0"),
            "prefix_dash": "BOL-" + digits,
            "prefix_hash": "BOL#" + digits,
            "spaces": digits[:4] + " " + digits[4:],
            "dashes": digits[:4] + "-" + digits[4:],
            "lowercase": "bol-" + digits}[pattern]


def transpose_bol(digits, existing, pick):
    """Swap two adjacent different digits so the result is not another real shipment's BOL.

    `pick` in [0, 1) chooses where to start looking. Returns None if no swap qualifies.
    """
    spots = [p for p in range(len(digits) - 1) if digits[p] != digits[p + 1]]
    start = int(pick * len(spots)) if spots else 0
    for k in range(len(spots)):
        p = spots[(start + k) % len(spots)]
        swapped = digits[:p] + digits[p + 1] + digits[p] + digits[p + 2:]
        if swapped not in existing:
            return swapped
    return None


def apply_bol_noise(invoices, shipment_bols, cfg, rng):
    """Add BOL formatting noise (`bol_format`) and transposed digits (`bol_typo`).

    A typo swaps two adjacent digits of a real BOL; the shipment still exists but an exact
    BOL match fails. Formatting noise keeps the digits and changes the dressing (prefix,
    spaces, dashes, lowercase, leading zeros dropped) so it canonicalizes back to the BOL.
    """
    t, n = cfg["traps"], len(invoices)
    typo_hit, fmt_hit = rng.random(n), rng.random(n)
    typo_pick, pattern_pick = rng.random(n), rng.random(n)
    existing = set(shipment_bols)
    for i, inv in enumerate(invoices):
        if typo_hit[i] < t["bol_typo_share"]:
            swapped = transpose_bol(inv["bol_digits"], existing, typo_pick[i])
            if swapped:
                inv["bol_digits"], inv["bol_typo"] = swapped, True
                inv["traps"].append(("bol_typo", ""))
        inv["bol_raw"] = inv["bol_digits"]
        if fmt_hit[i] < t["bol_format_noise_share"]:
            usable = [p for p in BOL_PATTERNS if p != "zeros_dropped" or inv["bol_digits"][0] == "0"]
            pattern = usable[int(pattern_pick[i] * len(usable))]
            inv["bol_raw"], inv["bol_noise"] = noisy_bol(inv["bol_digits"], pattern), pattern
            inv["traps"].append(("bol_format", pattern))


def apply_rounding_noise(invoices, cfg, rng):
    """Carrier system rounding: LH and/or FSC off by up to +/-rounding_noise_pct of the line.

    Small enough to sit inside sensible audit tolerances; too-tight tolerances flag it.
    """
    t, n = cfg["traps"], len(invoices)
    hit, which = rng.random(n), rng.integers(0, 3, size=n)
    factor = rng.uniform(-t["rounding_noise_pct"], t["rounding_noise_pct"], size=(n, 2))
    for i, inv in enumerate(invoices):
        if inv["superseded"] or hit[i] >= t["rounding_noise_share"]:
            continue
        codes = [c for j, c in enumerate(("LH", "FSC")) if which[i] in (j, 2)]
        changed = False
        for code in codes:
            line = get_line(inv, code)
            new = cents(line[1] * (1 + factor[i][("LH", "FSC").index(code)]))
            changed |= new != line[1]
            line[1] = new
        if changed:
            inv["traps"].append(("rounding_noise", "+".join(codes).lower()))


def add_unknown_charges(invoices, cfg, rng):
    """Put an unmapped, zero-dollar charge line on a couple of format D invoices.

    Normalization must report these as exceptions rather than silently dropping them. They
    are $0.00, so invoice totals and true dollar impacts are unaffected.
    """
    t = cfg["traps"]
    d_carriers = {c["id"] for c in cfg["carriers"] if c["format"] == "D"}
    pool = [inv for inv in invoices if inv["carrier_id"] in d_carriers and not inv["superseded"]]
    k = min(t["unknown_charge_lines"], len(pool))
    for i in sorted(rng.choice(len(pool), size=k, replace=False)):
        pool[i]["unknown_lines"].append([t["unknown_charge_description"], 0.0])
