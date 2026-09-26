"""Facilities, destination cities, lanes, and which carriers serve each lane.

A lane is an origin-destination pair. Freight is priced by lane because distance drives
cost, so every later step (rates, shipments, re-rating) keys on lane_id.
"""

import math

import pandas as pd

# Real US cities with approximate coordinates (public knowledge). Origins and destinations
# are separate pools so no lane starts and ends in the same city.
ORIGIN_POOL = [
    ("Chicago", "IL", 41.88, -87.63),
    ("Atlanta", "GA", 33.75, -84.39),
    ("Dallas", "TX", 32.78, -96.80),
    ("Columbus", "OH", 39.96, -83.00),
    ("Memphis", "TN", 35.15, -90.05),
    ("Harrisburg", "PA", 40.27, -76.88),
    ("Reno", "NV", 39.53, -119.81),
]
DESTINATION_POOL = [
    ("New York", "NY", 40.71, -74.01),
    ("Philadelphia", "PA", 39.95, -75.17),
    ("Boston", "MA", 42.36, -71.06),
    ("Charlotte", "NC", 35.23, -80.84),
    ("Miami", "FL", 25.76, -80.19),
    ("Orlando", "FL", 28.54, -81.38),
    ("Nashville", "TN", 36.16, -86.78),
    ("Detroit", "MI", 42.33, -83.05),
    ("Minneapolis", "MN", 44.98, -93.27),
    ("St. Louis", "MO", 38.63, -90.20),
    ("Kansas City", "MO", 39.10, -94.58),
    ("Denver", "CO", 39.74, -104.99),
    ("Houston", "TX", 29.76, -95.37),
    ("Phoenix", "AZ", 33.45, -112.07),
    ("Los Angeles", "CA", 34.05, -118.24),
    ("Seattle", "WA", 47.61, -122.33),
    ("Salt Lake City", "UT", 40.76, -111.89),
    ("Indianapolis", "IN", 39.77, -86.16),
    ("Pittsburgh", "PA", 40.44, -79.99),
    ("Louisville", "KY", 38.25, -85.76),
    ("Milwaukee", "WI", 43.04, -87.91),
    ("Birmingham", "AL", 33.52, -86.80),
    ("Richmond", "VA", 37.54, -77.44),
    ("Omaha", "NE", 41.26, -95.93),
    ("Portland", "OR", 45.52, -122.68),
]

EARTH_RADIUS_MILES = 3958.8


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def build_facilities(cfg):
    """Origin facilities. Each facility is a cost center, so freight expense can be booked
    to the site that shipped it."""
    n = cfg["network"]["n_origins"]
    if n > len(ORIGIN_POOL):
        raise ValueError(f"network.n_origins={n} but only {len(ORIGIN_POOL)} origin cities exist")
    rows = []
    for i, (city, state, lat, lon) in enumerate(ORIGIN_POOL[:n], start=1):
        rows.append({"facility_id": f"FAC{i:02d}", "city": city, "state": state,
                     "lat": lat, "lon": lon, "cost_center": f"CC-{100 + i}"})
    return pd.DataFrame(rows)


def build_destinations(cfg):
    """Destination cities (customers' delivery points)."""
    n = cfg["network"]["n_destinations"]
    if n > len(DESTINATION_POOL):
        raise ValueError(f"network.n_destinations={n} but only {len(DESTINATION_POOL)} "
                         f"destination cities exist")
    rows = []
    for i, (city, state, lat, lon) in enumerate(DESTINATION_POOL[:n], start=1):
        rows.append({"destination_id": f"DST{i:02d}", "city": city, "state": state,
                     "lat": lat, "lon": lon})
    return pd.DataFrame(rows)


def build_lanes(facilities, destinations, cfg, rng):
    """Sample n_lanes distinct origin-destination pairs and compute lane miles.

    Trucks don't drive great circles, so lane miles = haversine distance x circuity_factor,
    rounded to whole miles the way carriers' mileage guides quote them.
    """
    net = cfg["network"]
    pairs = [(o, d) for o in range(len(facilities)) for d in range(len(destinations))]
    picked = sorted(rng.choice(len(pairs), size=net["n_lanes"], replace=False))
    rows = []
    for lane_no, pair_index in enumerate(picked, start=1):
        o, d = pairs[pair_index]
        org, dst = facilities.iloc[o], destinations.iloc[d]
        crow = haversine_miles(org["lat"], org["lon"], dst["lat"], dst["lon"])
        rows.append({
            "lane_id": f"L{lane_no:03d}",
            "origin_id": org["facility_id"],
            "origin_city": org["city"],
            "origin_state": org["state"],
            "destination_id": dst["destination_id"],
            "destination_city": dst["city"],
            "destination_state": dst["state"],
            "miles": int(round(crow * net["circuity_factor"])),
        })
    return pd.DataFrame(rows)


def build_carriers(cfg):
    """The carrier master a shipper would keep: id, name, mode, invoice layout.

    error_multiplier and lag_median_days are simulator settings, not facts the shipper
    knows, so they are deliberately left out of this reference file.
    """
    return pd.DataFrame([{"carrier_id": c["id"], "carrier_name": c["name"],
                          "mode": c["mode"], "invoice_format": c["format"]}
                         for c in cfg["carriers"]])


def assign_carriers(lanes, cfg, rng):
    """Pick which carriers hold a contract on each lane.

    Shippers usually keep a primary and backup LTL carrier per lane and one TL carrier, so
    each lane gets carriers_per_lane.LTL LTL carriers and carriers_per_lane.TL TL carriers.
    """
    by_mode = {mode: [c["id"] for c in cfg["carriers"] if c["mode"] == mode]
               for mode in ("LTL", "TL")}
    rows = []
    for lane_id in lanes["lane_id"]:
        for mode in ("LTL", "TL"):
            k = cfg["carriers_per_lane"][mode]
            for carrier_id in sorted(rng.choice(by_mode[mode], size=k, replace=False)):
                rows.append({"carrier_id": str(carrier_id), "lane_id": lane_id, "mode": mode})
    return pd.DataFrame(rows)
