"""Adaptive geographic analysis for the Meril Analytics MCP server.

Deterministic (non-LLM) logic that:
  1. detects the granularity of a location field (country / state / city-point / zone),
  2. checks whether boundary data actually exists before promising a map,
  3. picks a color encoding (sequential vs categorical),
  4. falls back to hub-point bubbles or a sortable table rather than a misleading map,
  5. de-emphasizes low-n locations, and
  6. normalizes mixed currencies before any value is colored.

Rendering is delegated to a self-contained D3 + TopoJSON page (see geo_html.py-style
template below); this module only decides WHAT to draw and builds the data + warnings.
"""
import re
import json
import math

# Static FX table — no live rate service is available on-prem. Values are USD per 1
# unit of the currency. Update FX_DATE/FX_RATES when refreshing.
FX_DATE = "2026-01-15 (static reference table — no live FX service on-prem)"
FX_RATES = {
    "USD": 1.0, "EUR": 1.08, "GBP": 1.27, "INR": 0.012, "JPY": 0.0068,
    "AUD": 0.66, "CAD": 0.74, "SGD": 0.74, "AED": 0.27, "SAR": 0.27,
    "CNY": 0.14, "CHF": 1.12, "ZAR": 0.053, "BRL": 0.17, "MXN": 0.058,
}
CURRENCY_SYMBOLS = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP", "¥": "JPY"}

# Country aliases → the exact name used in world-atlas countries-110m properties.name.
COUNTRY_CANON = {
    "united states of america": "United States of America", "united states": "United States of America",
    "usa": "United States of America", "us": "United States of America", "u.s.": "United States of America",
    "u.s.a.": "United States of America", "america": "United States of America",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "u.k.": "United Kingdom",
    "great britain": "United Kingdom", "england": "United Kingdom",
    "india": "India", "china": "China", "japan": "Japan", "germany": "Germany",
    "france": "France", "italy": "Italy", "spain": "Spain", "canada": "Canada",
    "australia": "Australia", "brazil": "Brazil", "mexico": "Mexico", "russia": "Russia",
    "south africa": "South Africa", "singapore": "Singapore", "saudi arabia": "Saudi Arabia",
    "united arab emirates": "United Arab Emirates", "uae": "United Arab Emirates",
    "argentina": "Argentina", "netherlands": "Netherlands", "switzerland": "Switzerland",
    "sweden": "Sweden", "norway": "Norway", "poland": "Poland", "indonesia": "Indonesia",
    "south korea": "South Korea", "korea": "South Korea", "new zealand": "New Zealand",
    "ireland": "Ireland", "portugal": "Portugal", "belgium": "Belgium", "austria": "Austria",
    "egypt": "Egypt", "nigeria": "Nigeria", "kenya": "Kenya", "turkey": "Turkey",
    "thailand": "Thailand", "vietnam": "Vietnam", "malaysia": "Malaysia",
    "philippines": "Philippines", "chile": "Chile", "colombia": "Colombia", "peru": "Peru",
}
# ISO-A2 / A3 → canonical
COUNTRY_ISO = {
    "us": "United States of America", "usa": "United States of America",
    "gb": "United Kingdom", "gbr": "United Kingdom", "in": "India", "ind": "India",
    "cn": "China", "chn": "China", "jp": "Japan", "jpn": "Japan", "de": "Germany",
    "deu": "Germany", "fr": "France", "fra": "France", "au": "Australia", "aus": "Australia",
    "ca": "Canada", "can": "Canada", "sg": "Singapore", "sgp": "Singapore",
    "ae": "United Arab Emirates", "are": "United Arab Emirates", "sa": "Saudi Arabia",
    "sau": "Saudi Arabia", "br": "Brazil", "bra": "Brazil", "za": "South Africa",
    "zaf": "South Africa", "jp3": "Japan",
}

# US states/abbr → name used in us-atlas states-10m properties.name
US_STATES = {
    "alabama": "Alabama", "al": "Alabama", "alaska": "Alaska", "ak": "Alaska",
    "arizona": "Arizona", "az": "Arizona", "arkansas": "Arkansas", "ar": "Arkansas",
    "california": "California", "ca": "California", "colorado": "Colorado", "co": "Colorado",
    "connecticut": "Connecticut", "ct": "Connecticut", "delaware": "Delaware", "de": "Delaware",
    "florida": "Florida", "fl": "Florida", "georgia": "Georgia", "ga": "Georgia",
    "hawaii": "Hawaii", "hi": "Hawaii", "idaho": "Idaho", "id": "Idaho",
    "illinois": "Illinois", "il": "Illinois", "indiana": "Indiana", "in": "Indiana",
    "iowa": "Iowa", "ia": "Iowa", "kansas": "Kansas", "ks": "Kansas",
    "kentucky": "Kentucky", "ky": "Kentucky", "louisiana": "Louisiana", "la": "Louisiana",
    "maine": "Maine", "me": "Maine", "maryland": "Maryland", "md": "Maryland",
    "massachusetts": "Massachusetts", "ma": "Massachusetts", "michigan": "Michigan", "mi": "Michigan",
    "minnesota": "Minnesota", "mn": "Minnesota", "mississippi": "Mississippi", "ms": "Mississippi",
    "missouri": "Missouri", "mo": "Missouri", "montana": "Montana", "mt": "Montana",
    "nebraska": "Nebraska", "ne": "Nebraska", "nevada": "Nevada", "nv": "Nevada",
    "new hampshire": "New Hampshire", "nh": "New Hampshire", "new jersey": "New Jersey", "nj": "New Jersey",
    "new mexico": "New Mexico", "nm": "New Mexico", "new york": "New York", "ny": "New York",
    "north carolina": "North Carolina", "nc": "North Carolina", "north dakota": "North Dakota", "nd": "North Dakota",
    "ohio": "Ohio", "oh": "Ohio", "oklahoma": "Oklahoma", "ok": "Oklahoma",
    "oregon": "Oregon", "or": "Oregon", "pennsylvania": "Pennsylvania", "pa": "Pennsylvania",
    "rhode island": "Rhode Island", "ri": "Rhode Island", "south carolina": "South Carolina", "sc": "South Carolina",
    "south dakota": "South Dakota", "sd": "South Dakota", "tennessee": "Tennessee", "tn": "Tennessee",
    "texas": "Texas", "tx": "Texas", "utah": "Utah", "ut": "Utah",
    "vermont": "Vermont", "vt": "Vermont", "virginia": "Virginia", "va": "Virginia",
    "washington": "Washington", "wa": "Washington", "west virginia": "West Virginia", "wv": "West Virginia",
    "wisconsin": "Wisconsin", "wi": "Wisconsin", "wyoming": "Wyoming", "wy": "Wyoming",
    "district of columbia": "District of Columbia", "dc": "District of Columbia",
}
# Names that are BOTH a US state and a country (ambiguous without a country column)
AMBIGUOUS_STATE_COUNTRY = {"georgia"}

# Approximate geographic centroid (lat, lon) for every canonical country name
# COUNTRY_CANON/COUNTRY_ISO can resolve to. Used ONLY for positioning bubbles in
# the downloadable Excel's native chart — the chat choropleth draws by boundary
# shape and never needs these. Not population-weighted, just "somewhere sensible
# inside the country" — precise enough for a whole-country bubble marker.
COUNTRY_CENTROIDS = {
    "United States of America": (39.8, -98.6), "United Kingdom": (54.0, -2.0),
    "India": (22.0, 79.0), "China": (35.0, 103.0), "Japan": (36.5, 138.0),
    "Germany": (51.0, 9.0), "France": (46.6, 2.2), "Italy": (42.8, 12.5),
    "Spain": (40.0, -4.0), "Canada": (56.0, -106.0), "Australia": (-25.0, 133.0),
    "Brazil": (-10.0, -55.0), "Mexico": (23.6, -102.5), "Russia": (61.5, 105.0),
    "South Africa": (-29.0, 24.0), "Singapore": (1.35, 103.82),
    "Saudi Arabia": (24.0, 45.0), "United Arab Emirates": (24.0, 54.0),
    "Argentina": (-38.4, -63.6), "Netherlands": (52.2, 5.3), "Switzerland": (46.8, 8.2),
    "Sweden": (62.0, 15.0), "Norway": (61.0, 8.5), "Poland": (52.0, 19.0),
    "Indonesia": (-2.5, 118.0), "South Korea": (36.5, 127.8), "New Zealand": (-41.0, 174.0),
    "Ireland": (53.4, -8.0), "Portugal": (39.5, -8.0), "Belgium": (50.8, 4.5),
    "Austria": (47.5, 14.5), "Egypt": (26.0, 30.0), "Nigeria": (9.1, 8.7),
    "Kenya": (0.0, 38.0), "Turkey": (39.0, 35.0), "Thailand": (15.9, 101.0),
    "Vietnam": (16.0, 108.0), "Malaysia": (4.2, 102.0), "Philippines": (13.0, 122.0),
    "Chile": (-30.0, -71.0), "Colombia": (4.6, -74.3), "Peru": (-9.2, -75.0),
}

# Approximate geographic centroid (lat, lon) for every US state US_STATES can
# resolve to — same "Excel bubble chart only" purpose as COUNTRY_CENTROIDS.
US_STATE_CENTROIDS = {
    "Alabama": (32.8, -86.8), "Alaska": (64.2, -149.4), "Arizona": (34.2, -111.9),
    "Arkansas": (34.9, -92.4), "California": (37.2, -119.7), "Colorado": (39.0, -105.5),
    "Connecticut": (41.6, -72.7), "Delaware": (39.0, -75.5), "Florida": (27.8, -81.7),
    "Georgia": (32.6, -83.4), "Hawaii": (20.3, -156.3), "Idaho": (44.4, -114.6),
    "Illinois": (40.0, -89.2), "Indiana": (39.9, -86.3), "Iowa": (42.0, -93.5),
    "Kansas": (38.5, -98.4), "Kentucky": (37.5, -85.3), "Louisiana": (31.0, -92.0),
    "Maine": (45.4, -69.2), "Maryland": (39.0, -76.7), "Massachusetts": (42.3, -71.8),
    "Michigan": (44.3, -85.4), "Minnesota": (46.3, -94.3), "Mississippi": (32.7, -89.7),
    "Missouri": (38.5, -92.5), "Montana": (47.0, -109.6), "Nebraska": (41.5, -99.8),
    "Nevada": (39.3, -117.0), "New Hampshire": (43.7, -71.6), "New Jersey": (40.1, -74.7),
    "New Mexico": (34.5, -106.1), "New York": (42.9, -75.5), "North Carolina": (35.5, -79.4),
    "North Dakota": (47.5, -100.5), "Ohio": (40.3, -82.8), "Oklahoma": (35.5, -97.5),
    "Oregon": (43.9, -120.6), "Pennsylvania": (40.9, -77.7), "Rhode Island": (41.7, -71.5),
    "South Carolina": (33.9, -80.9), "South Dakota": (44.4, -100.2), "Tennessee": (35.9, -86.4),
    "Texas": (31.5, -99.3), "Utah": (39.3, -111.7), "Vermont": (44.0, -72.7),
    "Virginia": (37.5, -78.9), "Washington": (47.4, -120.4), "West Virginia": (38.6, -80.6),
    "Wisconsin": (44.6, -89.9), "Wyoming": (43.0, -107.5), "District of Columbia": (38.9, -77.0),
}

# Compact world-city gazetteer: name → (lat, lon). For point plotting + zone hubs.
CITY_GAZETTEER = {
    "new york": (40.71, -74.01), "los angeles": (34.05, -118.24), "chicago": (41.88, -87.63),
    "houston": (29.76, -95.37), "san francisco": (37.77, -122.42), "seattle": (47.61, -122.33),
    "boston": (42.36, -71.06), "miami": (25.76, -80.19), "atlanta": (33.75, -84.39),
    "dallas": (32.78, -96.80), "denver": (39.74, -104.99), "washington": (38.90, -77.04),
    "london": (51.51, -0.13), "paris": (48.85, 2.35), "berlin": (52.52, 13.40),
    "madrid": (40.42, -3.70), "rome": (41.90, 12.50), "amsterdam": (52.37, 4.90),
    "zurich": (47.38, 8.54), "dublin": (53.35, -6.26), "stockholm": (59.33, 18.06),
    "mumbai": (19.08, 72.88), "delhi": (28.61, 77.21), "new delhi": (28.61, 77.21),
    "bangalore": (12.97, 77.59), "bengaluru": (12.97, 77.59), "chennai": (13.08, 80.27),
    "hyderabad": (17.39, 78.49), "kolkata": (22.57, 88.36), "pune": (18.52, 73.86),
    "ahmedabad": (23.02, 72.57), "singapore": (1.35, 103.82), "tokyo": (35.68, 139.69),
    "hong kong": (22.32, 114.17), "shanghai": (31.23, 121.47), "beijing": (39.90, 116.41),
    "seoul": (37.57, 126.98), "sydney": (-33.87, 151.21), "melbourne": (-37.81, 144.96),
    "dubai": (25.20, 55.27), "riyadh": (24.71, 46.68), "abu dhabi": (24.45, 54.38),
    "toronto": (43.65, -79.38), "vancouver": (49.28, -123.12), "sao paulo": (-23.55, -46.63),
    "buenos aires": (-34.60, -58.38), "mexico city": (19.43, -99.13), "cairo": (30.04, 31.24),
    "johannesburg": (-26.20, 28.05), "nairobi": (-1.29, 36.82), "lagos": (6.52, 3.38),
    "bangkok": (13.76, 100.50), "jakarta": (-6.21, 106.85), "kuala lumpur": (3.14, 101.69),
    "manila": (14.60, 120.98), "istanbul": (41.01, 28.98),
}

# Informal zone label → representative hub city (real city within that zone).
ZONE_HUBS = {
    "apac": ("Singapore", 1.35, 103.82), "asia pacific": ("Singapore", 1.35, 103.82),
    "emea": ("London", 51.51, -0.13), "europe": ("Frankfurt", 50.11, 8.68),
    "latam": ("Sao Paulo", -23.55, -46.63), "latin america": ("Sao Paulo", -23.55, -46.63),
    "namer": ("New York", 40.71, -74.01), "north america": ("New York", 40.71, -74.01),
    "middle east": ("Dubai", 25.20, 55.27), "mena": ("Dubai", 25.20, 55.27),
    "west coast": ("Los Angeles", 34.05, -118.24), "east coast": ("New York", 40.71, -74.01),
    "midwest": ("Chicago", 41.88, -87.63), "south": ("Atlanta", 33.75, -84.39),
    "southeast": ("Atlanta", 33.75, -84.39), "northeast": ("Boston", 42.36, -71.06),
    "southwest": ("Phoenix", 33.45, -112.07), "northwest": ("Seattle", 47.61, -122.33),
    "pacific northwest": ("Seattle", 47.61, -122.33),
}
# Tokens that signal an informal zone (used only when a value matches nothing above)
ZONE_VOCAB = {"north", "south", "east", "west", "central", "region", "zone", "area",
              "sector", "district", "territory", "coast"}

TOPO_WORLD = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json"
TOPO_US = "https://cdn.jsdelivr.net/npm/us-atlas@3/states-10m.json"


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().lower())


def _to_number(v):
    """Parse a possibly-formatted number (₹1,240 / $2.5 / 1,240.5) → float or None.
    Non-finite values (NaN/±inf) return None so they never propagate into the
    aggregation, the color scale, or the embedded JSON."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = re.sub(r"[^0-9.\-]", "", str(v or ""))
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _looks_latlon(v) -> bool:
    m = re.match(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;]\s*(-?\d{1,3}(?:\.\d+)?)\s*$", str(v or ""))
    if not m:
        return False
    lat, lon = float(m.group(1)), float(m.group(2))
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _detect_currency_field(rows, currency_field):
    if currency_field:
        return currency_field
    for cand in ("currency", "curr", "ccy", "currency_code"):
        for r in rows:
            if cand in {k.lower() for k in r}:
                return next(k for k in r if k.lower() == cand)
    return None


def detect_granularity(rows, location_field, country_field=None):
    """Classify the location field into country / state / city / zone, honestly.

    Returns a dict: level, confidence, matched, unmatched, ambiguous, notes.
    """
    values = [r.get(location_field) for r in rows if r.get(location_field) not in (None, "")]
    uniq = sorted({str(v).strip() for v in values})
    if not uniq:
        return {"level": "none", "confidence": 0.0, "unmatched": [], "ambiguous": [],
                "matched": {}, "notes": "no usable location values found"}

    # lat/lon rows (either a 'lat,lon' field or separate lat & lon columns)
    keys = {k.lower() for r in rows for k in r}
    has_latlon_cols = ({"lat", "latitude"} & keys) and ({"lon", "lng", "long", "longitude"} & keys)
    latlon_vals = sum(1 for v in uniq if _looks_latlon(v))
    if has_latlon_cols or latlon_vals >= max(1, 0.6 * len(uniq)):
        return {"level": "city", "confidence": 1.0, "unmatched": [], "ambiguous": [],
                "matched": {"kind": "coordinates"}, "notes": "coordinate pairs detected"}

    n = len(uniq)
    country_hits, state_hits, city_hits, zone_hits, unmatched = [], [], [], [], []
    ambiguous = []
    have_country_col = bool(country_field) or bool({"country"} & keys)
    for v in uniq:
        nv = _norm(v)
        is_country = nv in COUNTRY_CANON or nv in COUNTRY_ISO
        is_state = nv in US_STATES
        is_city = nv in CITY_GAZETTEER
        is_zone = nv in ZONE_HUBS or any(tok in nv.split() for tok in ZONE_VOCAB)
        if nv in AMBIGUOUS_STATE_COUNTRY and not have_country_col:
            ambiguous.append(v)
        if is_country:
            country_hits.append(v)
        if is_state:
            state_hits.append(v)
        if is_city:
            city_hits.append(v)
        if is_zone:
            zone_hits.append(v)
        if not (is_country or is_state or is_city or is_zone):
            unmatched.append(v)

    frac = {"country": len(country_hits) / n, "state": len(state_hits) / n,
            "city": len(city_hits) / n, "zone": len(zone_hits) / n}
    # Zone labels are the strongest signal that a choropleth is WRONG — prefer zone
    # when a meaningful share are informal labels and few real admin matches exist.
    level = max(frac, key=frac.get)
    if frac["zone"] >= 0.4 and frac["zone"] >= frac["country"] and frac["zone"] >= frac["state"]:
        level = "zone"
    elif frac["country"] >= 0.6:
        level = "country"
    elif frac["state"] >= 0.6:
        level = "state"
    elif frac["city"] >= 0.6:
        level = "city"

    # Nothing matched ANY category → there is no usable location field. max() over
    # all-zero fractions returns the first key ("country"); left unchecked that would
    # render an empty choropleth. Classify as "none" so Step 5 (table) is used instead.
    if frac.get(level, 0.0) <= 0.0:
        level = "none"

    notes = ""
    if ambiguous:
        notes = (f"Ambiguous value(s) {ambiguous} could be a US state OR a country; "
                 f"no country column present to disambiguate — flagged for confirmation.")
    return {"level": level, "confidence": round(frac.get(level, 0.0), 2),
            "unmatched": unmatched, "ambiguous": ambiguous, "matched": frac, "notes": notes}


def normalize_currency(rows, value_field, currency_field, target="USD"):
    """Convert value_field to a single currency before any coloring. Returns
    (converted_rows, note, warnings). If no currency field → identity."""
    cf = _detect_currency_field(rows, currency_field)
    warnings = []
    if not cf:
        return rows, "", warnings
    # The target must exist in the FX table or the conversion math KeyErrors. Fall
    # back to USD (always present) rather than crashing on an unknown target code.
    target = (str(target).upper() if target else "USD")
    if target not in FX_RATES:
        warnings.append(f"Target currency '{target}' is not in the FX table; normalized to USD instead.")
        target = "USD"
    seen = {str(r.get(cf)).upper() for r in rows if r.get(cf)}
    if len(seen) <= 1:
        only = next(iter(seen), target)
        return rows, f"Single currency ({only}); no conversion needed.", warnings
    out = []
    unknown = set()
    for r in rows:
        r2 = dict(r)
        code = str(r.get(cf) or target).upper()
        val = _to_number(r.get(value_field))
        if val is None:
            out.append(r2)
            continue
        if code not in FX_RATES:
            unknown.add(code)
            out.append(r2)
            continue
        r2[value_field] = round(val * FX_RATES[code] / FX_RATES[target], 2)
        out.append(r2)
    if unknown:
        warnings.append(f"Currencies {sorted(unknown)} not in the FX table — those rows "
                        f"were left unconverted and should not be trusted on the color scale.")
    note = (f"Mixed currencies {sorted(seen)} converted to {target} using static rates "
            f"dated {FX_DATE}.")
    return out, note, warnings


def _aggregate(rows, location_field, value_field, group_field):
    """Group rows by location; return per-location value (sum), n, and ranked entities."""
    agg = {}
    for r in rows:
        loc = str(r.get(location_field) or "").strip()
        if not loc:
            continue
        val = _to_number(r.get(value_field)) or 0.0
        d = agg.setdefault(loc, {"value": 0.0, "n": 0, "entities": {}})
        d["value"] += val
        d["n"] += 1
        if group_field:
            ent = str(r.get(group_field) or "—").strip()
            d["entities"][ent] = d["entities"].get(ent, 0.0) + val
    for loc, d in agg.items():
        d["ranked"] = sorted(({"name": k, "value": round(v, 2)} for k, v in d["entities"].items()),
                             key=lambda x: x["value"], reverse=True)
        d["winner"] = d["ranked"][0]["name"] if d["ranked"] else None
        d.pop("entities", None)
    return agg


def build_geo_result(rows, location_field, value_field, group_field=None,
                     currency_field=None, country_field=None, color_mode="auto",
                     low_n=3, target_currency="USD"):
    """Run steps 1–7 and return a decision + render config + honest warnings.

    Also attaches a best-effort `centroid` {lat, lon} to every feature —
    regardless of which visual the chat display ended up using — so a
    downstream consumer (the Excel export's native bubble chart) can plot
    ALL rows by real coordinates even when the chat rendered a choropleth
    (which draws by boundary shape, not by point) or a table (no coordinates
    needed for a table). Never invents a coordinate: features that don't
    resolve to any known country/state/city/zone get centroid=None, exactly
    like the chat renderer drops/tables them instead of guessing."""
    result = _build_geo_result_core(
        rows, location_field, value_field, group_field=group_field,
        currency_field=currency_field, country_field=country_field,
        color_mode=color_mode, low_n=low_n, target_currency=target_currency,
    )
    label_key = "location" if result.get("visual") == "table" else "label"
    for f in result.get("features", []):
        loc = f.get(label_key)
        if "lat" in f and "lon" in f:
            f["centroid"] = {"lat": f["lat"], "lon": f["lon"]}
        else:
            c = _best_effort_centroid(loc) if loc else None
            f["centroid"] = {"lat": c[0], "lon": c[1]} if c else None
    return result


def _best_effort_centroid(loc):
    """Resolve a location string to a (lat, lon) centroid for the Excel bubble
    chart, trying country → US state → city/zone/lat-lon (via _resolve_point).
    Returns None rather than inventing a coordinate for anything unresolved."""
    nv = _norm(loc)
    canon = COUNTRY_CANON.get(nv) or COUNTRY_ISO.get(nv)
    if canon and canon in COUNTRY_CENTROIDS:
        return COUNTRY_CENTROIDS[canon]
    state = US_STATES.get(nv)
    if state and state in US_STATE_CENTROIDS:
        return US_STATE_CENTROIDS[state]
    pt = _resolve_point(loc)
    if pt:
        return (pt[0], pt[1])
    return None


def _build_geo_result_core(rows, location_field, value_field, group_field=None,
                     currency_field=None, country_field=None, color_mode="auto",
                     low_n=3, target_currency="USD"):
    """Run steps 1–7 and return a decision + render config + honest warnings."""
    warnings = []
    if not rows or not location_field or not value_field:
        return {"visual": "none", "why": "Missing dataset or field names.", "warnings": ["no input"]}

    rows, cur_note, cur_warn = normalize_currency(rows, value_field, currency_field, target_currency)
    warnings += cur_warn

    # country_field (a dedicated country column) lets Step 1 disambiguate values that
    # are BOTH a US state and a country (e.g. "Georgia") — NOT the currency field.
    gran = detect_granularity(rows, location_field, country_field=country_field)
    level = gran["level"]
    if gran["ambiguous"]:
        warnings.append(gran["notes"])
    if gran["unmatched"]:
        warnings.append(f"{len(gran['unmatched'])} location value(s) matched no boundary/gazetteer "
                        f"entry and are excluded from the map: {gran['unmatched'][:10]}"
                        + (" …" if len(gran['unmatched']) > 10 else ""))

    agg = _aggregate(rows, location_field, value_field, group_field)
    low_n_locs = [loc for loc, d in agg.items() if d["n"] < low_n]

    # STEP 3 color encoding
    if color_mode == "auto":
        color_encoding = "categorical" if group_field else "sequential"
    else:
        color_encoding = color_mode

    fx_note = cur_note

    # ---- Decision tree (Steps 2/4/5) ----
    # Shared context so the branch helpers stay small and this dispatcher flat.
    ctx = {"agg": agg, "location_field": location_field, "value_field": value_field,
           "group_field": group_field, "low_n": low_n, "low_n_locs": low_n_locs,
           "color_encoding": color_encoding, "gran": gran, "warnings": warnings,
           "fx_note": fx_note}

    if level == "none":
        return _table_result(agg, location_field, value_field, group_field, low_n,
                             "No usable location field after granularity detection — a map would be invented, not informed.",
                             warnings, fx_note)

    if level == "country":
        return _choropleth_or_table(
            ctx, lambda loc: COUNTRY_CANON.get(_norm(loc)) or COUNTRY_ISO.get(_norm(loc)),
            level="country", label="Countries",
            why="Values are countries; world topology is reliably available.",
            topo={"url": TOPO_WORLD, "object": "countries", "match": "name",
                  "projection": "geoNaturalEarth1"})

    if level == "state":
        # Only US subdivisions are reliably available here.
        us_like = all(_norm(loc) in US_STATES for loc in agg) or gran["confidence"] >= 0.6
        if us_like:
            return _choropleth_or_table(
                ctx, lambda loc: US_STATES.get(_norm(loc)),
                level="state-us", label="US states",
                why="Values are US states; us-atlas subdivision topology is available and name-matched.",
                topo={"url": TOPO_US, "object": "states", "match": "name",
                      "projection": "geoAlbersUsa"})
        warnings.append("Subdivision topology for this country is not bundled/verified — "
                        "falling back to hub-city bubbles rather than rendering an empty/wrong choropleth.")
        return _bubble_result(agg, location_field, value_field, group_field, color_encoding,
                              gran, low_n, warnings, fx_note, hub_note=True)

    if level in ("city", "zone"):
        return _bubble_result(agg, location_field, value_field, group_field, color_encoding,
                              gran, low_n, warnings, fx_note, hub_note=(level == "zone"))

    return _table_result(agg, location_field, value_field, group_field, low_n,
                         "Granularity could not be determined confidently.", warnings, fx_note)


def _choropleth_or_table(ctx, match_map, *, level, label, why, topo):
    """Build a choropleth from ctx['agg'] via match_map, or fall back to a TABLE when
    nothing matched the topology — so we never render an empty/misleading map (Step 2/5)."""
    feats, unmatched = [], []
    for loc, d in ctx["agg"].items():
        key = match_map(loc)
        if key is None:
            unmatched.append(loc)
            continue
        feats.append({"key": key, "label": loc, "value": round(d["value"], 2),
                      "n": d["n"], "lowN": d["n"] < ctx["low_n"], "winner": d["winner"],
                      "ranked": d["ranked"]})
    if unmatched:
        ctx["warnings"].append(f"{label} not matched to topology (excluded): {unmatched[:10]}")
    if not feats:
        return _table_result(ctx["agg"], ctx["location_field"], ctx["value_field"],
                             ctx["group_field"], ctx["low_n"],
                             f"No location matched {label.lower()} topology — a table is shown instead of an empty map.",
                             ctx["warnings"], ctx["fx_note"])
    return {"visual": "choropleth", "level": level, "why": why, "topology": topo,
            "color_encoding": ctx["color_encoding"], "features": feats,
            "granularity": ctx["gran"], "low_n_locations": ctx["low_n_locs"],
            "warnings": ctx["warnings"], "currency_note": ctx["fx_note"],
            "value_field": ctx["value_field"], "group_field": ctx["group_field"]}


def _resolve_point(loc):
    """Resolve a location string to (lat, lon, rep_label). Zone → hub city; city → gazetteer;
    'lat,lon' → itself. Returns None if unresolvable (never invents coordinates)."""
    nv = _norm(loc)
    if _looks_latlon(loc):
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)", str(loc))
        return float(m.group(1)), float(m.group(2)), None
    if nv in ZONE_HUBS:
        city, lat, lon = ZONE_HUBS[nv]
        return lat, lon, city
    if nv in CITY_GAZETTEER:
        lat, lon = CITY_GAZETTEER[nv]
        return lat, lon, None
    # informal single-word zone with no hub → unresolvable
    return None


def _bubble_result(agg, location_field, value_field, group_field, color_encoding,
                   gran, low_n, warnings, fx_note, hub_note):
    feats, unresolved, hub_map = [], [], {}
    for loc, d in agg.items():
        pt = _resolve_point(loc)
        if pt is None:
            unresolved.append(loc)
            continue
        lat, lon, rep = pt
        if rep:
            hub_map[loc] = rep
        feats.append({"lat": lat, "lon": lon, "label": loc, "rep": rep,
                      "value": round(d["value"], 2), "n": d["n"], "lowN": d["n"] < low_n,
                      "winner": d["winner"], "ranked": d["ranked"]})
    if unresolved:
        warnings.append(f"No coordinate/hub for {unresolved[:10]}"
                        + (" …" if len(unresolved) > 10 else "")
                        + " — geocoding is not available on-prem, so these were dropped (not invented).")
    if not feats:
        return _table_result(agg, location_field, value_field, group_field, low_n,
                             "No location could be resolved to a coordinate or hub — a table is safer than an empty map.",
                             warnings, fx_note)
    if hub_note and hub_map:
        warnings.append("Zone/region → representative hub city (approximation, NOT administrative precision): "
                        + ", ".join(f"{z}→{c}" for z, c in list(hub_map.items())[:12]))
    return {"visual": "bubble",
            "why": ("Zone/region labels have no real boundaries — plotted as representative hub-city bubbles."
                    if hub_note else "City/point data — plotted directly by coordinate."),
            "topology": {"url": TOPO_WORLD, "object": "countries", "projection": "geoNaturalEarth1"},
            "color_encoding": color_encoding, "features": feats, "granularity": gran,
            "hub_map": hub_map, "warnings": warnings, "currency_note": fx_note,
            "value_field": value_field, "group_field": group_field}


def _table_result(agg, location_field, value_field, group_field, low_n, why, warnings, fx_note):
    rows_out = []
    for loc, d in sorted(agg.items(), key=lambda kv: kv[1]["value"], reverse=True):
        rows_out.append({"location": loc, "value": round(d["value"], 2), "n": d["n"],
                         "lowN": d["n"] < low_n, "winner": d["winner"], "ranked": d["ranked"]})
    return {"visual": "table", "why": why, "features": rows_out, "warnings": warnings,
            "currency_note": fx_note, "value_field": value_field, "group_field": group_field,
            "location_field": location_field}
