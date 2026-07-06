"""End-to-end tests for the adaptive geo tool (render_geo_analysis).

Covers the four granularity types the spec requires — country, state, zone-label,
and mixed — plus city/point, an n=1-outlier dataset, mixed currencies, and the
ambiguity ("Georgia") flag. Verifies the decision logic (geo_analysis), the
rendered HTML (geo_html), and the MCP tool wiring / input parsing (server.py).

Pure-stdlib + a tiny fastmcp/mcp.types stub so it runs WITHOUT the container:
    python3 test_geo_analysis.py
Exit code 0 = all passed.
"""
import os
import sys
import types
import json
import asyncio
import tempfile

# --- run in an isolated CHART_DIR (read at server import time) ---
_TMP = tempfile.mkdtemp(prefix="geo-test-")
os.environ["CHART_DIR"] = _TMP

# --- stub fastmcp + mcp.types so server.py imports without the real packages ---
_fastmcp = types.ModuleType("fastmcp")


class _FakeMCP:
    def __init__(self, *a, **k):
        pass

    def tool(self, *a, **k):
        def deco(fn):
            return fn
        return deco


_fastmcp.FastMCP = _FakeMCP
sys.modules["fastmcp"] = _fastmcp

_mcp = types.ModuleType("mcp")
_mcp_types = types.ModuleType("mcp.types")


class _Res:
    def __init__(self, **k):
        self.__dict__.update(k)


class _TRC:
    def __init__(self, **k):
        self.__dict__.update(k)
        self.text = k.get("text")


_mcp_types.EmbeddedResource = _Res
_mcp_types.TextResourceContents = _TRC
_mcp.types = _mcp_types
sys.modules["mcp"] = _mcp
sys.modules["mcp.types"] = _mcp_types

# httpx is a real dep of server.py; if absent, stub the surface server.py touches.
try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    _httpx = types.ModuleType("httpx")
    for _n in ("ConnectError", "ConnectTimeout", "TimeoutException", "HTTPStatusError"):
        setattr(_httpx, _n, type(_n, (Exception,), {}))
    _httpx.AsyncClient = object
    _httpx.Timeout = lambda *a, **k: None
    sys.modules["httpx"] = _httpx

import server  # noqa: E402
from geo_analysis import build_geo_result, detect_granularity  # noqa: E402
from geo_html import make_geo_html  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def render_ok(result, label):
    """The rendered HTML must be self-contained: placeholders substituted, data & D3 present."""
    html = make_geo_html(result, label)
    check(f"[{label}] HTML has no leftover placeholders",
          "__GEO__" not in html and "__TITLE__" not in html)
    check(f"[{label}] HTML embeds D3 + topojson", "d3@7" in html and "topojson-client" in html)
    check(f"[{label}] HTML carries the decision (why/visual)",
          result.get("visual") in html or (result.get("why", "")[:20] in html))
    return html


def rows_multi(pairs, per):
    """Expand [(loc, val)] into `per` rows each, so n = per (well-sampled)."""
    out = []
    for loc, val in pairs:
        for _ in range(per):
            out.append({"loc": loc, "val": val / per})
    return out


# ---------------------------------------------------------------------------
print("\n== 1. COUNTRY level → choropleth (world topology) ==")
rows = rows_multi([("India", 1200), ("United States", 3400), ("Germany", 900),
                   ("Japan", 1500), ("Brazil", 600)], per=5)
r = build_geo_result(rows, "loc", "val")
check("visual == choropleth", r["visual"] == "choropleth", r.get("visual"))
check("level == country", r.get("level") == "country")
check("topology is world-atlas", "world-atlas" in r["topology"]["url"])
check("color encoding sequential (no group)", r["color_encoding"] == "sequential")
check("all countries matched (no unmatched warning)",
      not any("not matched to world topology" in w for w in r["warnings"]))
check("features carry drill-down fields (n, ranked)",
      all("n" in f and "ranked" in f for f in r["features"]))
render_ok(r, "country")

print("\n== 1b. COUNTRY + group_field → categorical winner-per-location ==")
grows = []
for c, vend, v in [("India", "AcmeCo", 800), ("India", "Globex", 400),
                   ("Germany", "Globex", 700), ("Germany", "AcmeCo", 200),
                   ("Japan", "Initech", 1500)]:
    grows.append({"loc": c, "val": v, "vendor": vend})
rg = build_geo_result(grows, "loc", "val", group_field="vendor")
check("color encoding categorical", rg["color_encoding"] == "categorical")
india = next(f for f in rg["features"] if f["label"] == "India")
check("India winner == AcmeCo (800 > 400)", india["winner"] == "AcmeCo", india.get("winner"))
check("ranked list present per location", len(india["ranked"]) == 2)
render_ok(rg, "country-categorical")

# ---------------------------------------------------------------------------
print("\n== 2. STATE level (US) → choropleth (us-atlas) ==")
rows = rows_multi([("California", 500), ("Texas", 420), ("New York", 610),
                   ("Florida", 300), ("Washington", 275)], per=4)
r = build_geo_result(rows, "loc", "val")
check("visual == choropleth", r["visual"] == "choropleth", r.get("visual"))
check("level == state-us", r.get("level") == "state-us", r.get("level"))
check("topology is us-atlas", "us-atlas" in r["topology"]["url"])
check("projection geoAlbersUsa", r["topology"]["projection"] == "geoAlbersUsa")
render_ok(r, "state-us")

# ---------------------------------------------------------------------------
print("\n== 3. ZONE-LABEL level → hub-bubble fallback (NOT choropleth) ==")
rows = rows_multi([("APAC", 1200), ("EMEA", 900), ("LATAM", 400),
                   ("North America", 2100)], per=6)
r = build_geo_result(rows, "loc", "val")
check("visual == bubble (never choropleth for informal zones)", r["visual"] == "bubble", r.get("visual"))
check("granularity level == zone", r["granularity"]["level"] == "zone")
check("hub_map documents zone→city", bool(r["hub_map"]))
check("warns it is an approximation, not admin precision",
      any("approximation" in w.lower() for w in r["warnings"]))
check("every zone resolved to a hub coordinate", all("lat" in f and "lon" in f for f in r["features"]))
render_ok(r, "zone")

# ---------------------------------------------------------------------------
print("\n== 4. MIXED granularity → does NOT force a choropleth ==")
# countries + informal zones + junk: zone share dominates → bubble, not a wrong map.
mrows = rows_multi([("India", 300), ("China", 400),
                    ("North", 250), ("West", 180), ("APAC", 900),
                    ("Atlantis", 50)], per=3)
r = build_geo_result(mrows, "loc", "val")
check("visual is bubble or table (not a misleading choropleth)",
      r["visual"] in ("bubble", "table"), r.get("visual"))
check("unmatched junk ('Atlantis') surfaced in warnings",
      any("Atlantis" in w for w in r["warnings"]) or
      any("atlantis" in w.lower() for w in r["warnings"]) or
      any("No coordinate/hub" in w for w in r["warnings"]),
      r["warnings"])
render_ok(r, "mixed")

# ---------------------------------------------------------------------------
print("\n== 5. CITY / POINT level → bubble by coordinate ==")
rows = rows_multi([("Mumbai", 400), ("London", 700), ("Singapore", 550),
                   ("New York", 900), ("Tokyo", 620)], per=4)
r = build_geo_result(rows, "loc", "val")
check("visual == bubble", r["visual"] == "bubble", r.get("visual"))
check("granularity level == city", r["granularity"]["level"] == "city")
check("no hub-approximation warning (real cities)",
      not any("approximation" in w.lower() for w in r["warnings"]))
render_ok(r, "city")

print("\n== 5b. lat/lon pairs → bubble, coordinates plotted directly ==")
llrows = [{"loc": "40.71,-74.01", "val": 100}, {"loc": "51.51,-0.13", "val": 200},
          {"loc": "1.35,103.82", "val": 150}]
r = build_geo_result(llrows, "loc", "val")
check("lat/lon detected as city/point", r["visual"] == "bubble")
check("granularity notes coordinate detection",
      "coordinate" in (r["granularity"].get("notes", "").lower()))

# ---------------------------------------------------------------------------
print("\n== 6. n=1 OUTLIERS → decision data still flags them (API), UI no longer shows it (by request) ==")
# most locations well-sampled (n=5), two are single-row outliers (n=1). The
# underlying decision (geo_analysis.py) still computes lowN — useful for any
# programmatic consumer of build_geo_result — but the rendered widget
# (geo_html.py) intentionally no longer surfaces a "low sample size" warning
# or dashed/faded styling: removed at the user's request as noisy/confusing.
mix = rows_multi([("India", 1000), ("Germany", 900), ("Japan", 800)], per=5)
mix += [{"loc": "Brazil", "val": 5000}]      # n=1 outlier, huge value
mix += [{"loc": "France", "val": 4000}]      # n=1 outlier
r = build_geo_result(mix, "loc", "val", low_n=3)
lown = {f["label"]: f["lowN"] for f in r["features"]}
check("Brazil (n=1) flagged lowN in decision data", lown.get("Brazil") is True)
check("France (n=1) flagged lowN in decision data", lown.get("France") is True)
check("India (n=5) NOT flagged lowN in decision data", lown.get("India") is False)
check("low_n_locations lists exactly the outliers",
      set(r["low_n_locations"]) == {"Brazil", "France"}, r["low_n_locations"])
html = render_ok(r, "n1-outliers")
check("[n1-outliers] HTML no longer shows the low-n warning/styling (removed by request)",
      "lown" not in html and "low sample size" not in html.lower())

# ---------------------------------------------------------------------------
print("\n== 7. MIXED CURRENCY → normalized before coloring, rate stated ==")
crows = [{"loc": "India", "val": 100000, "currency": "INR"},
         {"loc": "United States", "val": 2000, "currency": "USD"},
         {"loc": "Germany", "val": 1800, "currency": "EUR"},
         {"loc": "Japan", "val": 300000, "currency": "JPY"},
         {"loc": "Narnia", "val": 500, "currency": "XYZ"}]  # unknown currency
r = build_geo_result(crows, "loc", "val", currency_field="currency", target_currency="USD")
check("currency_note states conversion to USD + date",
      "USD" in r["currency_note"] and "2026" in r["currency_note"], r["currency_note"])
check("unknown currency (XYZ) warned, left unconverted",
      any("XYZ" in w for w in r["warnings"]), r["warnings"])
# India 100000 INR * 0.012 = 1200 USD — verify conversion actually happened
india = next(f for f in r["features"] if f["label"] == "India")
check("India value converted INR→USD (~1200)", abs(india["value"] - 1200) < 1, india["value"])

# ---------------------------------------------------------------------------
print("\n== 8. NO usable location → table fallback (never an invented map) ==")
jrows = [{"loc": "Widget-A", "val": 10}, {"loc": "Widget-B", "val": 20},
         {"loc": "SKU-1099", "val": 30}]
r = build_geo_result(jrows, "loc", "val")
check("visual == table", r["visual"] == "table", r.get("visual"))
check("table explains why a map was withheld", bool(r["why"]))
render_ok(r, "table")

# ---------------------------------------------------------------------------
print("\n== 9. AMBIGUOUS 'Georgia' (no country column) → flagged ==")
arows = rows_multi([("Georgia", 100), ("Texas", 200), ("Florida", 150)], per=3)
g = detect_granularity(arows, "loc", country_field=None)
check("ambiguous list contains Georgia", "Georgia" in g["ambiguous"], g["ambiguous"])
check("note explains state-vs-country ambiguity",
      "state" in g["notes"].lower() and "country" in g["notes"].lower())
g2 = detect_granularity(arows, "loc", country_field="country")
check("providing country_field clears the ambiguity flag", not g2["ambiguous"])

# ---------------------------------------------------------------------------
print("\n== 10. TOOL WIRING — server.render_geo_analysis (input parsing + widget) ==")


def run(coro):
    return asyncio.run(coro)


# 10a. list[dict] input → EmbeddedResource pointing at a written geo-*.html
res = run(server.render_geo_analysis(
    location_field="loc", value_field="val",
    data=rows_multi([("India", 1200), ("Germany", 900), ("Japan", 1500)], per=4),
    title="Revenue by country"))
check("returns an EmbeddedResource (UI widget)", hasattr(res, "resource"))
uri_text = getattr(res.resource, "text", "")
check("resource points at a geo-*.html on the webserver", "/charts/geo-" in uri_text, uri_text)
fname = uri_text.rsplit("/", 1)[-1]
check("the widget HTML file was actually written",
      os.path.exists(os.path.join(_TMP, fname)))

# 10b. CSV string via file_content → parsed into rows
csv_text = "loc,val\nIndia,1200\nUnited States,3400\nGermany,900\n"
res = run(server.render_geo_analysis(
    location_field="loc", value_field="val", file_content=csv_text))
check("CSV file_content parsed → widget returned", hasattr(res, "resource"))

# 10c. JSON-array string in `data`
res = run(server.render_geo_analysis(
    location_field="loc", value_field="val",
    data=json.dumps([{"loc": "APAC", "val": 5}, {"loc": "EMEA", "val": 9}])))
check("JSON-string data parsed → widget returned", hasattr(res, "resource"))

# 10d. bad field name → helpful text error (not a crash)
res = run(server.render_geo_analysis(
    location_field="nope", value_field="val",
    data=[{"loc": "India", "val": 1}]))
check("missing location field → clear text error", isinstance(res, str) and "not in the data" in res)

# 10e. ambiguous Georgia through the TOOL, no country col → asks instead of guessing
res = run(server.render_geo_analysis(
    location_field="loc", value_field="val",
    data=rows_multi([("Georgia", 100), ("Texas", 200)], per=2)))
check("ambiguous value → tool asks to disambiguate",
      isinstance(res, str) and "ambiguous" in res.lower(), str(res)[:80])

# 10f. empty data → clear text error
res = run(server.render_geo_analysis(location_field="loc", value_field="val", data=[]))
check("empty data → clear text error", isinstance(res, str) and "No tabular data" in res)

# ---------------------------------------------------------------------------
print("\n== 11. HARDENING REGRESSIONS (found via real-runtime fuzzing) ==")
# 11a. target_currency not in the FX table must NOT KeyError (was a crash).
crows2 = [{"loc": "India", "val": 100, "cur": "USD"}, {"loc": "Japan", "val": 200, "cur": "EUR"}]
try:
    r = build_geo_result(crows2, "loc", "val", currency_field="cur", target_currency="ZZZ")
    check("unknown target_currency does not crash", True)
    check("warns it fell back to USD", any("USD" in w for w in r["warnings"]), r["warnings"])
except Exception as e:
    check("unknown target_currency does not crash", False, f"{type(e).__name__}: {e}")

# 11b. NaN/Inf values are sanitized to None at the source (no propagation / no crash).
from geo_analysis import _to_number  # noqa: E402
check("_to_number(NaN) → None", _to_number(float("nan")) is None)
check("_to_number(inf) → None", _to_number(float("inf")) is None)
check("_to_number('-inf' float) → None", _to_number(float("-inf")) is None)
nanrows = [{"loc": "India", "val": float("nan")}, {"loc": "Japan", "val": float("inf")},
           {"loc": "Germany", "val": 500}]
r = build_geo_result(nanrows, "loc", "val")
allvals = [f["value"] for f in r.get("features", [])]
import math as _m  # noqa: E402
check("no NaN/Inf reaches feature values", all(_m.isfinite(v) for v in allvals), allvals)
check("NaN/Inf render without error", "__GEO__" not in make_geo_html(r, "nan"))

# 11c. renderer JS has an offline-CDN guard and a boot() try/catch backstop.
from geo_html import _GEO_HTML  # noqa: E402
check("renderer guards missing d3 (offline CDN)", 'typeof d3==="undefined"' in _GEO_HTML)
check("renderer wraps boot() in try/catch", "try{\n  boot();" in _GEO_HTML or "try{" in _GEO_HTML and "boot();" in _GEO_HTML)

# ---------------------------------------------------------------------------
print("\n== 12. ADVERSARIAL INPUT HARDENING (permanent guards for crash-hunt finds) ==")


def widget_html(res):
    txt = getattr(getattr(res, "resource", None), "text", "") or ""
    fn = txt.rsplit("/", 1)[-1]
    p = os.path.join(_TMP, fn)
    return open(p, encoding="utf-8").read() if fn and os.path.exists(p) else ""


def embedded_geo_parses(html):
    """The `var GEO = …;`/`var TITLE = …;` literals must parse as JSON (guards the
    __TITLE__/__GEO__ placeholder-clobber bug, which breaks the widget silently)."""
    import re as _re
    m = _re.search(r"var GEO = (.*?);\nvar TITLE = (.*?);\n", html, _re.DOTALL)
    if not m:
        return False
    try:
        for g in (m.group(1), m.group(2)):
            json.loads(g.replace("<\\/", "</"))
        return True
    except ValueError:
        return False


# 12a. a data value literally equal to a template placeholder must not break the JS
res = run(server.render_geo_analysis(location_field="loc", value_field="val",
    data=[{"loc": "__TITLE__", "val": 5}, {"loc": "__GEO__", "val": 6}, {"loc": "India", "val": 7}]))
check("__TITLE__/__GEO__ in data → widget returned", hasattr(res, "resource"))
check("__TITLE__/__GEO__ embedded JSON stays valid",
      hasattr(res, "resource") and embedded_geo_parses(widget_html(res)))

# 12b. malformed CSV / JSON strings must never raise
for label, kw in [
    ("CSV NUL byte", dict(location_field="a", value_field="b", file_content="a,b\nIndia,1\x00\nJapan,2")),
    ("oversized CSV field", dict(location_field="a", value_field="b",
                                 file_content='a,b\n"' + "x" * 200000 + '",2\nJapan,3')),
    ("deeply nested JSON string", dict(location_field="a", value_field="b", data="[" * 20000)),
    ("spelled-out target currency", dict(location_field="loc", value_field="val", target_currency="euros",
        currency_field="cur", data=[{"loc": "India", "val": 1, "cur": "USD"},
                                    {"loc": "France", "val": 2, "cur": "EUR"}])),
]:
    try:
        res = run(server.render_geo_analysis(**kw))
        check(f"{label} → no crash", isinstance(res, str) or hasattr(res, "resource"))
    except Exception as e:
        check(f"{label} → no crash", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
print("\n== 13. run_file_analytics chart-type guard (production incident) ==")
# Real incident: a geography-flavored file prompt made the local Qwen model emit
# chart config {"type": "choropleth", ...} — not a real Chart.js controller — which
# threw "choropleth is not a registered controller" in the browser and blanked that
# chart's iframe. _filter_charts must drop any non-Chart.js type before it's ever
# written into a rendered iframe.
bad_charts = [
    {"title": "World Choropleth", "config": {"type": "choropleth",
        "data": {"labels": ["India", "USA"], "datasets": [{"data": [100, 200]}]}}},
    {"title": "US State Choropleth", "config": {"type": "map",
        "data": {"labels": ["CA", "TX"], "datasets": [{"data": [1, 2]}]}}},
    {"title": "Heatmap attempt", "config": {"type": "heatmap",
        "data": {"labels": ["A"], "datasets": [{"data": [1]}]}}},
    {"title": "Real bar chart", "config": {"type": "bar",
        "data": {"labels": ["A", "B"], "datasets": [{"data": [10, 20]}]}}},
    {"title": "Odd-cased polarArea", "config": {"type": "polararea",
        "data": {"labels": ["A", "B", "C"], "datasets": [{"data": [10, 20, 5]}]}}},
]
kept = server._filter_charts(bad_charts)
kept_titles = [c["title"] for c in kept]
check("choropleth chart type is dropped (was the production crash)",
      "World Choropleth" not in kept_titles)
check("map chart type is dropped", "US State Choropleth" not in kept_titles)
check("heatmap chart type is dropped", "Heatmap attempt" not in kept_titles)
check("real bar chart survives", "Real bar chart" in kept_titles)
check("odd-cased polarArea survives and is casing-normalized",
      any(c["title"] == "Odd-cased polarArea" and c["config"]["type"] == "polarArea" for c in kept))
check("exactly the two valid charts remain", kept_titles == ["Real bar chart", "Odd-cased polarArea"], kept_titles)

# ---------------------------------------------------------------------------
print("\n== 14. CENTROID RESOLUTION (feeds the Excel export's live Bubble Chart) ==")
country_r = build_geo_result(rows_multi([("India", 1200), ("United States", 3400)], per=4), "loc", "val")
by_label = {f["label"]: f["centroid"] for f in country_r["features"]}
check("country centroid resolved (India)", by_label.get("India") == {"lat": 22.0, "lon": 79.0})
check("country centroid resolved (US)",
      by_label.get("United States") == {"lat": 39.8, "lon": -98.6})

state_r = build_geo_result(rows_multi([("California", 500), ("Texas", 420)], per=4), "loc", "val")
by_label = {f["label"]: f["centroid"] for f in state_r["features"]}
check("US state centroid resolved (California)",
      by_label.get("California") == {"lat": 37.2, "lon": -119.7})

zone_r = build_geo_result(rows_multi([("APAC", 900)], per=3), "loc", "val")
check("zone centroid reuses its hub city's lat/lon",
      zone_r["features"][0]["centroid"] == {"lat": 1.35, "lon": 103.82})

junk_r = build_geo_result(rows_multi([("Widget-A", 10), ("SKU-1099", 30)], per=3), "loc", "val")
check("unresolvable table rows get centroid=None (never invented)",
      all(f["centroid"] is None for f in junk_r["features"]))

# ---------------------------------------------------------------------------
print("\n== 15. EXCEL EXPORT — download link rendering + graceful degradation ==")
r = build_geo_result(rows_multi([("India", 1200), ("Germany", 900)], per=4), "loc", "val")
html_no_dl = make_geo_html(r, "No download yet")
check("no download link in the widget when download_url is omitted",
      "class=\"dl\"" not in html_no_dl and ">null<" not in html_no_dl)
html_with_dl = make_geo_html(r, "Ready to download",
                             download_url="http://10.10.30.160:7001/files/geo_report_abc123.xlsx")
check("download link appears once a URL is supplied",
      "geo_report_abc123.xlsx" in html_with_dl)
check("download link JSON stays valid (no injection via the URL)",
      embedded_geo_parses(html_with_dl))

# render_geo_analysis must still return the interactive widget successfully
# even though playwright/network aren't available in this test environment —
# the Excel export is best-effort and must never block the core feature.
res = run(server.render_geo_analysis(
    location_field="loc", value_field="val",
    data=[{"loc": "India", "val": 1200}, {"loc": "Germany", "val": 900}],
    title="Degradation check"))
check("tool still returns a widget when the screenshot/office pipeline is unavailable",
      hasattr(res, "resource"))
degraded_html = widget_html(res)
check("widget has no download link when the export pipeline couldn't run",
      "class=\"dl\"" not in degraded_html)

# ---------------------------------------------------------------------------
print(f"\n{'='*56}\nRESULT: {PASS} passed, {FAIL} failed\n{'='*56}")
sys.exit(1 if FAIL else 0)
