"""Load and validate config.yaml.

Every tunable number lives in config.yaml. The pipeline loads it once here and passes the
resulting dict down, so a reviewer can see every assumption in one file.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

# Dotted paths that must exist. Later stages add their own keys to this list.
REQUIRED_KEYS = [
    "seed",
    "period.start", "period.months", "period.runoff_days", "period.audit_as_of",
    "network.n_origins", "network.n_destinations", "network.n_lanes", "network.circuity_factor",
    "carriers", "carriers_per_lane.LTL", "carriers_per_lane.TL",
    "shipments.n", "shipments.tl_share", "shipments.ltl_weight_lbs", "shipments.tl_weight_lbs",
    "shipments.monthly_weights", "shipments.miles_per_day", "shipments.transit_extra_days",
    "rates.ltl.base_cwt.intercept", "rates.ltl.base_cwt.per_mile", "rates.ltl.weight_breaks",
    "rates.ltl.min_charge", "rates.ltl.deficit_weight_rating",
    "rates.tl.rate_per_mile", "rates.tl.min_charge",
    "rates.carrier_factor_range", "rates.lane_noise_range", "rates.open_ended_to",
    "rates.amendments.share_of_carrier_lanes", "rates.amendments.effective_date",
    "rates.amendments.change_range",
    "diesel.source", "diesel.eia_csv_path", "diesel.lead_weeks", "diesel.price_decimals",
    "diesel.synthetic", "diesel.shock",
    "fsc.reference_week", "fsc.ltl.base_price", "fsc.ltl.step", "fsc.ltl.pct_per_step",
    "fsc.tl.peg_price", "fsc.tl.mpg",
    "accessorials.ltl.liftgate", "accessorials.ltl.residential", "accessorials.tl.detention",
    "traps.reweigh_share_ltl", "traps.reweigh_factor_range",
    # Stage 2
    "accessorials.late_authorization_share", "accessorials.late_authorization_max_days",
    "invoicing.lag_sigma", "invoicing.max_lag_days", "invoicing.receipt_delay_days",
    "invoicing.rebill_lag_days", "invoicing.balance_due_lag_days",
    "errors.duplicate_invoice", "errors.rate_overcharge", "errors.fsc_mismatch",
    "errors.unauthorized_accessorial", "errors.weight_overbilling", "errors.phantom_invoice",
    "errors.sub_tolerance_share", "errors.duplicate_lag_days", "errors.duplicate_resend_share",
    "errors.stale_rate_share", "errors.fsc_wrong_week_share", "errors.max_redraws",
    "errors.magnitudes.rate_markup", "errors.magnitudes.rate_sub_tolerance",
    "errors.magnitudes.weight_inflation", "errors.magnitudes.weight_sub_tolerance",
    "errors.magnitudes.fsc_wrong_week_offset", "errors.magnitudes.fsc_wrong_steps",
    "errors.magnitudes.fsc_tl_price_inflation", "errors.magnitudes.fsc_sub_tolerance_pp",
    "errors.magnitudes.fsc_sub_tolerance_tl_dollars", "errors.magnitudes.fsc_tl_wrong_table_mpg",
    "errors.systemic_issues",
    "traps.rebill_share", "traps.balance_due_share", "traps.bol_format_noise_share",
    "traps.bol_typo_share", "traps.rounding_noise_share", "traps.rounding_noise_pct",
    "traps.rebill_original_overstate", "traps.unknown_charge_lines",
    "traps.unknown_charge_description",
    "normalization.charge_code_map", "normalization.fallback_match", "normalization.totals_tolerance",
    # Stage 4
    "audit.tolerances.duplicate_amount", "audit.tolerances.duplicate_window_days",
    "audit.tolerances.rate_pct", "audit.tolerances.rate_abs", "audit.tolerances.fsc_ltl_pp",
    "audit.tolerances.fsc_min_dollars", "audit.tolerances.fsc_tl_abs", "audit.tolerances.fsc_tl_pct",
    "audit.tolerances.weight_pct",
    # Stage 5
    "evaluation.sweep.rate_pct", "evaluation.sweep.fsc_ltl_pp", "evaluation.sweep.weight_pct",
    "evaluation.sweep.fsc_tl_pct", "evaluation.min_precision", "evaluation.review_minutes_per_flag",
    "evaluation.analyst_cost_per_hour", "evaluation.false_dispute_cost",
    "evaluation.systemic.window_months", "evaluation.systemic.multiple",
    "evaluation.systemic.min_invoices", "evaluation.systemic.min_flags", "evaluation.systemic.alpha",
    "evaluation.min_material_gain",
    # Stage 6
    "accruals.trailing_days", "accruals.default_accessorial_per_shipment.LTL",
    "accruals.default_accessorial_per_shipment.TL", "accruals.accounts.expense", "accruals.accounts.liability", "evaluation.top_n_invoices",
]

CARRIER_KEYS = ["id", "name", "mode", "format", "lag_median_days", "error_multiplier",
                "name_variants"]


class ConfigError(ValueError):
    """Raised when config.yaml is missing a key or has an inconsistent value."""


def get(cfg, dotted):
    """Return cfg["a"]["b"] for dotted="a.b", raising ConfigError if any part is missing."""
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"config.yaml is missing required key '{dotted}' "
                              f"(stopped at '{part}'). See the skeleton in SPEC.md.")
        node = node[part]
    return node


def validate(cfg):
    """Fail loudly on missing keys or values that would silently break generation."""
    for key in REQUIRED_KEYS:
        get(cfg, key)

    for i, carrier in enumerate(cfg["carriers"]):
        missing = [k for k in CARRIER_KEYS if k not in carrier]
        if missing:
            raise ConfigError(f"carriers[{i}] is missing {missing}")
        if carrier["mode"] not in ("LTL", "TL"):
            raise ConfigError(f"carrier {carrier['id']}: mode must be LTL or TL")

    weights = cfg["shipments"]["monthly_weights"]
    if len(weights) != cfg["period"]["months"]:
        raise ConfigError(f"shipments.monthly_weights has {len(weights)} entries but "
                          f"period.months is {cfg['period']['months']}")
    if abs(sum(weights) - 1.0) > 1e-9:
        raise ConfigError(f"shipments.monthly_weights must sum to 1 (got {sum(weights)})")

    net = cfg["network"]
    if net["n_lanes"] > net["n_origins"] * net["n_destinations"]:
        raise ConfigError("network.n_lanes exceeds the number of origin-destination pairs")

    for mode, needed in cfg["carriers_per_lane"].items():
        available = sum(1 for c in cfg["carriers"] if c["mode"] == mode)
        if needed > available:
            raise ConfigError(f"carriers_per_lane.{mode}={needed} but only {available} "
                              f"{mode} carriers are configured")

    if cfg["diesel"]["source"] not in ("synthetic", "eia_csv"):
        raise ConfigError("diesel.source must be 'synthetic' or 'eia_csv'")
    if cfg["fsc"]["reference_week"] != "ship_week":
        raise ConfigError("fsc.reference_week: only 'ship_week' is supported")
    return cfg


def load_config(path=DEFAULT_CONFIG_PATH):
    """Read config.yaml into a plain dict and validate it."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} did not parse to a mapping")
    return validate(cfg)
