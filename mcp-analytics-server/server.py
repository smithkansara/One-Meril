import os
import re
import io
import csv
import json
import time
import uuid
import base64
import asyncio
import logging
from pathlib import Path
import httpx
from fastmcp import FastMCP
from mcp.types import EmbeddedResource, TextResourceContents

from geo_analysis import build_geo_result, detect_granularity
from geo_html import make_geo_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mcp-analytics")

# Retry ONLY connection-level failures — the model wasn't reachable yet (a brief
# restart / cold container). A ReadTimeout means the model DID start but generation
# ran past the deadline; retrying just re-runs the same slow work 3× (and blows past
# LibreChat's own MCP timeout), so those are NOT retried — they fail fast with a
# clear message instead.
_TRANSIENT_HTTPX = (httpx.ConnectError, httpx.ConnectTimeout)

PORT = int(os.getenv("MCP_PORT", "8959"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3:8b")
# num_ctx drives KV-cache memory. This box has ~12 GiB free; the 5.2 GB q4_k_m
# model + 32K ctx needed 20 GiB and failed to load. 8192 keeps total ~9 GiB.
NUM_CTX = int(os.getenv("NUM_CTX", "8192"))
# Keep the prompt (system + data + response) within NUM_CTX. ~20k chars ≈ 5k
# tokens of data, leaving room for the system prompt and the JSON response.
MAX_DATA_CHARS = int(os.getenv("MAX_DATA_CHARS", "20000"))
# Hard ceiling on generated tokens. qwen3:8b on one A4000 does ~25 tok/s, so a
# runaway response (e.g. a "top 10 × 10 sections" request) blows past the read
# timeout. Capping keeps generation well under it; the system prompt also bounds
# output so a complete JSON is produced within this budget.
NUM_PREDICT = int(os.getenv("NUM_PREDICT", "4096"))
CHART_DIR = os.getenv("CHART_DIR", "/app/charts")
WEBSERVER_BASE_URL = os.getenv("WEBSERVER_BASE_URL", "http://10.10.30.160:7001").rstrip("/")
# mcp-office builds the combined "data + report" download file (it owns the
# document libraries and the shared /files output volume).
MCP_OFFICE_URL = os.getenv("MCP_OFFICE_URL", "http://mcp-office-docs:8958").rstrip("/")

mcp = FastMCP(
    name="Meril Analytics",
    instructions=(
        "## AUTOMATIC TOOL SELECTION RULES — follow these BEFORE choosing any analytics tool.\n\n"

        "### RULE 1 — FILE UPLOADED → use run_file_analytics\n"
        "Call run_file_analytics if ANY of the following is true:\n"
        "- The user has attached, uploaded, or shared a file (CSV, Excel, Word, PDF, PowerPoint, TXT, JSON, etc.).\n"
        "- The message references 'this file', 'attached file', 'uploaded file', 'the data', 'this spreadsheet', "
        "'this document', 'this report', 'this CSV', 'analyse this', 'analyze this'.\n"
        "- LibreChat has passed a file_url, file_content, or file_name to the tool call.\n"
        "- A file extension appears anywhere in the context: .csv, .xlsx, .xls, .docx, .doc, .pdf, .pptx, .txt, .json.\n\n"

        "### RULE 2 — NO FILE, WEB QUERY → use run_analytics (via the n8n action)\n"
        "Use run_analytics ONLY when there is NO file attached AND the user is asking about:\n"
        "stocks, crypto, finance, market data, Yahoo Finance, Wikipedia, company comparisons, "
        "live web data, rankings, or any question that requires internet sources.\n\n"

        "### RULE 3 — run_file_analytics returns a self-contained UI widget\n"
        "run_file_analytics returns a single interactive UI resource (a widget) that shows a live "
        "progress bar and then renders the FULL analysis by itself — KPIs, charts, insights, AND the "
        "download link are all inside the widget.\n"
        "After it returns, your ENTIRE reply MUST be ONLY the UI resource marker it gives you "
        "(e.g. \\ui{<id>}). Output NOTHING else: no preamble, no summary, no headings, no 'Download' "
        "links, no URLs, and do NOT repeat any 'UI Resource'/'marker' instruction text. NEVER invent "
        "or write a download URL — the real one is inside the widget. Do NOT call any other tool.\n\n"

        "NEVER call run_analytics when a file has been uploaded — it does not process files."
    ),
)

_ARTIFACT_CHART_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:transparent;padding:10px;font-family:system-ui,sans-serif}
canvas{width:100%!important;max-height:340px}
</style>
</head>
<body>
<canvas id="c"></canvas>
<script>
(function(){
var cfg=__CONFIG__;
cfg.options=cfg.options||{};
cfg.options.responsive=true;
var pl=cfg.options.plugins=cfg.options.plugins||{};
var tt=pl.tooltip=pl.tooltip||{};
tt.enabled=true;tt.mode="index";tt.intersect=false;
tt.backgroundColor="rgba(0,0,0,0.88)";tt.titleColor="#fff";
tt.bodyColor="#ccc";tt.padding=10;tt.cornerRadius=8;
tt.borderColor="rgba(255,255,255,0.1)";tt.borderWidth=1;
(tt.callbacks=tt.callbacks||{}).label=function(c){
  var v=c.parsed&&c.parsed.y!=null?c.parsed.y:typeof c.parsed==="number"?c.parsed:null;
  if(v==null)return " "+(c.dataset.label||"")+": "+c.formattedValue;
  var a=Math.abs(v);
  return " "+(c.dataset.label||"")+": "+(
    a>=1e7?"₹"+(v/1e7).toFixed(2)+"Cr":
    a>=1e5?"₹"+(v/1e5).toFixed(2)+"L":
    a>=1e3?"₹"+(v/1e3).toFixed(1)+"K":
    v.toLocaleString("en-IN",{maximumFractionDigits:2})
  );
};
if(cfg.type==="line")(cfg.data&&cfg.data.datasets||[]).forEach(function(d){
  d.pointRadius=Math.max(d.pointRadius||0,4);d.pointHoverRadius=7;
  if(d.tension==null)d.tension=0.3;
});
new Chart(document.getElementById("c"),cfg);
})();
</script>
</body>
</html>"""


def _make_artifact_html(config: dict) -> str:
    config_json = json.dumps(config, separators=(",", ":"))
    return _ARTIFACT_CHART_HTML.replace("__CONFIG__", config_json)


# KPI stat-tile row. A single horizontally-scrolling row of cards; theme-aware
# (light/dark) surfaces and delta colors follow the data-viz design system.
_STAT_TILES_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
  --border:rgba(11,11,11,0.10);
  --pos:#006300;--neg:#d03b3b;--neu:#898781;
}
@media (prefers-color-scheme:dark){
  :root{
    --surface:#1a1a19;--ink:#ffffff;--ink2:#c3c2b7;
    --border:rgba(255,255,255,0.10);
    --pos:#0ca30c;--neg:#d03b3b;--neu:#898781;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:transparent;padding:6px 2px;
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
.row{display:flex;gap:12px;overflow-x:auto;padding-bottom:6px}
.tile{flex:0 0 auto;min-width:172px;background:var(--surface);
  border:1px solid var(--border);border-radius:12px;padding:15px 18px}
.label{color:var(--ink2);font-size:13px;font-weight:500;margin-bottom:9px;
  white-space:nowrap}
.value{color:var(--ink);font-size:29px;font-weight:600;line-height:1.05;
  white-space:nowrap}
.delta{margin-top:9px;font-size:12.5px;font-weight:500;white-space:nowrap}
.delta.pos{color:var(--pos)}.delta.neg{color:var(--neg)}.delta.neu{color:var(--neu)}
.arrow{font-weight:700;margin-right:3px}
</style>
</head>
<body>
<div class="row" id="r"></div>
<script>
(function(){
var stats=__STATS__;
function esc(s){return String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
var arrow={positive:"\\u2197",negative:"\\u2198",neutral:""};
var r=document.getElementById("r");
stats.forEach(function(s){
  var sent=(s.sentiment||"neutral").toLowerCase();
  var cls=sent==="positive"?"pos":sent==="negative"?"neg":"neu";
  var t=document.createElement("div");t.className="tile";
  var html='<div class="label">'+esc(s.label)+'</div>'+
    '<div class="value">'+esc(s.value)+'</div>';
  if(s.delta){html+='<div class="delta '+cls+'"><span class="arrow">'+
    (arrow[sent]||"")+'</span>'+esc(s.delta)+'</div>';}
  t.innerHTML=html;r.appendChild(t);
});
})();
</script>
</body>
</html>"""


def _make_stats_html(stats: list) -> str:
    stats_json = json.dumps(stats, separators=(",", ":"))
    return _STAT_TILES_HTML.replace("__STATS__", stats_json)


_FILE_ANALYSIS_SYSTEM = """\
You are an expert data analyst. Analyze the provided data and produce a complete analysis with charts.

Respond ONLY with a single valid JSON object — no markdown fences, no text before or after.

OUTPUT BUDGET (applies even if the user asks for "everything" / "top 10" / "10 sections"):
Your ENTIRE response must be a COMPLETE, valid JSON object that fits in a few thousand tokens.
Prioritize the highest-value findings over exhaustiveness. Hard limits: at most 5 stat tiles,
at most 6 charts, at most 8 insights, and a "markdown_report" of at most ~500 words. A concise
COMPLETE response is required; a long truncated one is a failure.

Required JSON structure:
{
  "title": "A concise 5-10 word title naming what this analysis/research is about, reflecting the user's request, e.g. 'CRM Lead Source & Win-Rate Analysis'",
  "summary": "2-3 sentences: what the dataset is about, its domain, and scope",
  "preprocessing_notes": "brief note on any cleaning done (nulls, duplicates, type fixes)",
  "stats": [
    {"label": "Total revenue", "value": "₹2.99L", "delta": "12.4% vs last month", "sentiment": "positive"},
    {"label": "Active vendors", "value": "148", "delta": "No change", "sentiment": "neutral"},
    {"label": "Avg bid turnaround", "value": "3.2d", "delta": "0.4d slower", "sentiment": "negative"}
  ],
  "insights": [
    "Insight 1: specific finding with ACTUAL numbers from the data e.g. 'Region North had the highest sales of 1,240 units'",
    "Insight 2: ...",
    "Insight 3: ...",
    "Insight 4: ...",
    "Insight 5: ..."
  ],
  "charts": [
    {
      "title": "Descriptive chart title",
      "config": {
        "type": "bar",
        "data": {
          "labels": ["Label1", "Label2"],
          "datasets": [{"label": "Series name", "data": [1240, 980], "backgroundColor": ["#4e79a7","#f28e2b"]}]
        },
        "options": {
          "responsive": true,
          "plugins": {
            "legend": {"position": "top"},
            "title": {"display": true, "text": "Chart title"}
          }
        }
      }
    }
  ],
  "markdown_report": "## Overview\\n\\n...\\n\\n### Key Insights\\n\\n- ...\\n\\n### Patterns & Trends\\n\\n- ...\\n\\n### Recommendations\\n\\n- ..."
}

CRITICAL chart rules — read carefully before generating any chart:

1. DATA VALUES MUST BE REAL NUMBERS FROM THE DATASET.
   - NEVER use placeholder values like 0.1, 0.2, 0.3, 0.4, 0.5, 1.0.
   - NEVER normalize data to a 0–1 range unless the source column is already a percentage or ratio.
   - Values must come directly from the data cells (e.g. 1240, 98.5, 3000000).
   - BAD example: data: [0.1, 0.2, 0.3, 0.4, 0.5]  ← FORBIDDEN — these are fake placeholders
   - GOOD example: data: [1240, 980, 1560, 720, 1100] ← real values from the dataset

2. TEXT-ONLY COLUMNS CANNOT BE CHART VALUES.
   - If a column contains sentences or descriptions (not numbers), do NOT use it as chart data.
   - For text data, COUNT occurrences or frequency of categories instead.
   - Example: if dataset has a "Status" column with values "Active/Inactive", chart the COUNT of each status.
   - Example: if dataset has a "Region" column, chart the SUM or COUNT per region.

3. NO DATA = NO CHART.
   - If the dataset has no numerical columns and you cannot derive meaningful counts, return "charts": [].
   - Do NOT invent chart data. An empty chart array is better than a fake chart.

4. Chart types — ONLY these Chart.js types exist, use nothing else: "bar", "line", "pie",
   "doughnut", "radar", "polarArea", "bubble", "scatter". "bar" for comparisons, "line" for
   time-series/trends, "pie"/"doughnut" for proportions (≤7 slices), "radar" for multi-variable
   scores. There is NO "map"/"choropleth"/"geo"/"heatmap" chart type — Chart.js cannot render
   maps. For geographic/country/state/region breakdowns, use a "bar" chart of the metric per
   location instead (e.g. "Quote by Country" as a bar chart) — never invent a map chart type.
5. Horizontal bar: type "bar" with options.indexAxis = "y".
6. Maximum 15 data points per chart — aggregate if larger.
7. Colors: #4e79a7 #f28e2b #e15759 #76b7b2 #59a14f #edc948 #b07aa1 #ff9da7 #9c755f
8. Each dataset needs backgroundColor (array). Line charts also need borderColor.
9. Generate 2–4 charts. If user specifies a focus, prioritize those dimensions.

STAT TILE rules (the "stats" array — a row of KPI cards shown at the top):
- Produce 3–5 headline KPI tiles capturing the single most important numbers in the
  data: totals, counts, rates, averages, balances (e.g. total revenue, active vendor
  count, win rate, average deal value, remaining credit balance).
- "value": the headline number as a compact formatted string — 1,284 / 12.9K / ₹4.2M /
  28.6% / 3.2d. Keep it short (this is the big number on the card).
- "delta" (optional): a short change/context line vs a named period, e.g.
  "12.4% vs last month", "No change", "0.4d slower". Omit if there is nothing to compare.
- "sentiment": "positive" if the value/trend is good, "negative" if bad, "neutral"
  otherwise. This drives the tile's color (green / red / gray) and arrow — so a rising
  cost or slower turnaround is "negative", not "positive".
- Every value MUST be a real number derived from the data. If there are no meaningful
  headline metrics, return "stats": []."""

_THINK_TAG_RE = re.compile(r'<think>.*?</think>', re.DOTALL)

_FILE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "preprocessing_notes": {"type": "string"},
        "stats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                    "delta": {"type": "string"},
                    "sentiment": {"type": "string"},
                },
                "required": ["label", "value"],
            },
        },
        "insights": {"type": "array", "items": {"type": "string"}},
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "config": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "data": {
                                "type": "object",
                                "properties": {
                                    "labels": {"type": "array", "items": {"type": "string"}},
                                    "datasets": {"type": "array", "items": {"type": "object"}},
                                },
                                "required": ["labels", "datasets"],
                            },
                            "options": {"type": "object"},
                        },
                        "required": ["type", "data"],
                    },
                },
                "required": ["title", "config"],
            },
        },
        "markdown_report": {"type": "string"},
    },
    "required": ["title", "summary", "stats", "insights", "charts", "markdown_report"],
}


def _truncate_data(content: str) -> str:
    if len(content) <= MAX_DATA_CHARS:
        return content
    truncated = content[:MAX_DATA_CHARS]
    last_nl = truncated.rfind('\n')
    if last_nl > MAX_DATA_CHARS * 0.8:
        truncated = truncated[:last_nl]
    return truncated + f"\n\n[... truncated at {len(truncated):,} chars — dataset is larger ...]"


def _strip_think_tags(raw: str) -> str:
    """Remove <think>…</think> blocks, falling back to their content if nothing else remains."""
    think_match = re.search(r'<think>(.*?)</think>', raw, re.DOTALL)
    think_content = think_match.group(1).strip() if think_match else ""
    raw = _THINK_TAG_RE.sub("", raw).strip()

    unclosed = re.search(r'<think>(.*)', raw, flags=re.DOTALL)
    if unclosed and not think_content:
        think_content = unclosed.group(1).strip()
    raw = re.sub(r'<think>.*', '', raw, flags=re.DOTALL).strip()

    return think_content if not raw else raw


def _extract_json_object(raw: str, original_raw: str) -> str:
    """Strip markdown fences, locate the outermost JSON object, and return it."""
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw.strip())
    raw = raw.strip()

    start, end = raw.find('{'), raw.rfind('}')
    if start != -1 and end != -1 and start <= end:
        raw = raw[start:end + 1]

    if not raw:
        raise json.JSONDecodeError(
            f"Qwen returned empty response after cleaning. "
            f"Original output (first 500 chars): {original_raw[:500]!r}",
            "", 0,
        )
    return raw


async def _post_qwen(content: str, user_prompt: str, on_progress=None) -> str:
    """Single POST to Ollama; returns the raw message content. When `on_progress`
    is given, the response is streamed and `on_progress(accumulated_text)` is called
    as tokens arrive (so a live progress bar can reflect real generation)."""
    user_msg = f"User request: {user_prompt}\n\nData:\n{_truncate_data(content)}\n\n/no_think"
    body = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": _FILE_ANALYSIS_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "stream": bool(on_progress),
        "think": False,
        "format": _FILE_ANALYSIS_SCHEMA,
        "options": {"temperature": 0.1, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(270.0, connect=10.0)) as client:
        if not on_progress:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
            resp.raise_for_status()
            return resp.json()["message"]["content"]
        # Streaming: Ollama emits one JSON object per line; accumulate content.
        acc: list[str] = []
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (obj.get("message") or {}).get("content", "")
                if piece:
                    acc.append(piece)
                    await on_progress("".join(acc))
                if obj.get("done"):
                    break
        return "".join(acc)


async def _call_qwen(content: str, user_prompt: str, on_progress=None) -> dict:
    # Retry transient Ollama failures (dropped connection during a restart, a cold
    # model still loading). A brief blip should self-heal, not surface as an error.
    attempts = 3
    original_raw = None
    for i in range(attempts):
        try:
            original_raw = await _post_qwen(content, user_prompt, on_progress)
            break
        except _TRANSIENT_HTTPX as e:
            logger.warning("Ollama call attempt %d/%d failed: %s: %s",
                           i + 1, attempts, type(e).__name__, e)
            if i == attempts - 1:
                raise
            await asyncio.sleep(2 * (i + 1))
        except httpx.HTTPStatusError as e:
            # 5xx (e.g. model still loading) is worth one more try; 4xx is not.
            if e.response.status_code >= 500 and i < attempts - 1:
                logger.warning("Ollama HTTP %d, retrying (%d/%d)",
                               e.response.status_code, i + 1, attempts)
                await asyncio.sleep(2 * (i + 1))
                continue
            raise

    raw = _strip_think_tags(original_raw)
    raw = _extract_json_object(raw, original_raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Qwen output is not valid JSON. "
            f"Extracted (first 500 chars): {raw[:500]!r}",
            e.doc, e.pos,
        ) from e


def _detect_format(file_name: str | None) -> str:
    """Infer the output file format from the uploaded filename extension."""
    if not file_name:
        return "xlsx"
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    mapping = {
        "xlsx": "xlsx", "xls": "xlsx",
        "docx": "docx", "doc": "docx",
        "pdf": "pdf",
        "pptx": "pptx", "ppt": "pptx",
        "csv": "csv",
        "txt": "txt", "text": "txt", "md": "txt",
    }
    return mapping.get(ext, "xlsx")


async def _load_file_content(
    file_content: str | None,
    file_url: str | None,
    file_name: str | None,
) -> tuple[str, list[str]]:
    """Fetch/collect the file text. Returns (raw_content, parts_list)."""
    parts: list[str] = []
    raw_content = ""

    if file_content:
        raw_content = file_content
        parts.append(file_content)
    elif file_url:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                r = await client.get(file_url)
                r.raise_for_status()
                raw_content = r.text
                parts.append(raw_content)
        except Exception:
            parts.append(f"[File available at: {file_url} — could not download automatically]")

    if file_name:
        parts.insert(0, f"Filename: {file_name}")

    return raw_content, parts


# ---------------------------------------------------------------------------
# Session file cache — remember an uploaded file's extracted content so
# follow-up questions can be analyzed WITHOUT re-reading / re-sending the file.
# Keyed by file identity (file_url is unique per upload; file_name as fallback),
# optionally scoped by session_id. In-memory (per mcp-analytics process).
# ---------------------------------------------------------------------------
_FILE_CACHE: dict[str, dict] = {}
_FILE_CACHE_MAX_ENTRIES = int(os.getenv("FILE_CACHE_MAX_ENTRIES", "64"))
_FILE_CACHE_TTL = int(os.getenv("FILE_CACHE_TTL", str(6 * 3600)))       # 6 hours
_FILE_CACHE_MAX_BYTES = int(os.getenv("FILE_CACHE_MAX_BYTES", "2000000"))  # 2 MB / entry


def _cache_keys(session_id: str | None, file_url: str | None, file_name: str | None) -> list[str]:
    sid = (session_id or "").strip()
    keys: list[str] = []
    for ident in (file_url, file_name):
        ident = (ident or "").strip()
        if ident:
            keys.append(f"{sid}::{ident}" if sid else ident)
    return keys


def _cache_evict() -> None:
    now = time.time()
    for k in [k for k, v in _FILE_CACHE.items() if now - v["ts"] > _FILE_CACHE_TTL]:
        _FILE_CACHE.pop(k, None)
    if len(_FILE_CACHE) > _FILE_CACHE_MAX_ENTRIES:
        oldest = sorted(_FILE_CACHE, key=lambda k: _FILE_CACHE[k]["ts"])
        for k in oldest[: len(_FILE_CACHE) - _FILE_CACHE_MAX_ENTRIES]:
            _FILE_CACHE.pop(k, None)


def _cache_store(session_id, file_url, file_name, content: str) -> None:
    if not content or not content.strip():
        return
    entry = {"content": content[:_FILE_CACHE_MAX_BYTES], "ts": time.time()}
    for k in _cache_keys(session_id, file_url, file_name):
        _FILE_CACHE[k] = entry
    _cache_evict()


def _cache_lookup(session_id, file_url, file_name) -> str | None:
    now = time.time()
    for k in _cache_keys(session_id, file_url, file_name):
        e = _FILE_CACHE.get(k)
        if e and (now - e["ts"]) <= _FILE_CACHE_TTL:
            e["ts"] = now  # refresh recency
            return e["content"]
    return None


async def _resolve_file_content(
    session_id, file_url, file_name, file_content
) -> tuple[str, list[str], bool]:
    """Load the file content, caching fresh content and reusing cached content
    for follow-ups. Returns (raw_content, parts, reused_from_cache)."""
    raw_content, parts = await _load_file_content(file_content, file_url, file_name)
    if raw_content.strip():
        _cache_store(session_id, file_url, file_name, raw_content)
        return raw_content, parts, False

    cached = _cache_lookup(session_id, file_url, file_name)
    if cached:
        parts = ([f"Filename: {file_name}"] if file_name else []) + [cached]
        return cached, parts, True

    return raw_content, parts, False


def _is_placeholder_data(datasets: list) -> bool:
    """Return True if all data values look like fake 0–1 placeholders."""
    all_values = []
    for ds in datasets:
        vals = ds.get("data", [])
        for v in vals:
            try:
                all_values.append(float(v))
            except (TypeError, ValueError):
                pass
    if not all_values:
        return True
    real_values = [v for v in all_values if v > 1 or v < 0 or v != round(v, 1)]
    # If every value is a neat 0.x decimal ≤1.0, it's almost certainly fabricated
    all_tiny = all(0 <= v <= 1.0 for v in all_values)
    all_rounded_tenths = all(round(v * 10) == v * 10 for v in all_values)
    return all_tiny and all_rounded_tenths and len(real_values) == 0


# Chart.js v3 core controllers registered by the CDN's UMD "auto" bundle
# (chart.js@3.9.1/dist/chart.min.js). Keyed lowercase so any casing the model
# emits is recognized; value is the CORRECT casing Chart.js's registry expects
# (note "polarArea" — an incorrect case, e.g. "polararea", also throws).
_CHARTJS_TYPES = {
    "bar": "bar", "line": "line", "pie": "pie", "doughnut": "doughnut",
    "radar": "radar", "polararea": "polarArea", "bubble": "bubble", "scatter": "scatter",
}


def _filter_charts(charts: list) -> list:
    """Remove charts whose data is empty, contains placeholder values, or whose type
    isn't a real Chart.js controller. A hallucinated type (e.g. "choropleth"/"map"/
    "geo" for a geographic request) throws "X is not a registered controller" in the
    browser and blanks that chart's iframe — drop it here instead, before it reaches
    the browser."""
    valid = []
    for chart in charts:
        config = chart.get("config", {})
        ctype = str(config.get("type", "")).strip().lower()
        if ctype not in _CHARTJS_TYPES:
            logger.warning("Dropping chart %r — unsupported Chart.js type %r",
                           chart.get("title"), config.get("type"))
            continue
        config["type"] = _CHARTJS_TYPES[ctype]  # normalize casing (polararea -> polarArea)
        datasets = config.get("data", {}).get("datasets", [])
        labels = config.get("data", {}).get("labels", [])
        if not datasets or not labels:
            continue
        if _is_placeholder_data(datasets):
            continue
        valid.append(chart)
    return valid


async def _render_charts(charts: list) -> list[str]:
    """Validate, save each chart to disk and return raw <iframe> HTML blocks (with a
    title label) for embedding in the report widget."""
    blocks: list[str] = []
    os.makedirs(CHART_DIR, exist_ok=True)
    for chart in _filter_charts(charts):
        config = chart.get("config")
        if not isinstance(config, dict):
            continue
        title = chart.get("title", "")
        chart_id = uuid.uuid4().hex[:12]
        chart_file = Path(CHART_DIR) / f"chart-{chart_id}.html"
        await asyncio.to_thread(chart_file.write_text, _make_artifact_html(config), "utf-8")
        chart_url = f"{WEBSERVER_BASE_URL}/charts/chart-{chart_id}.html"
        label = f'<div class="chart-title">{_esc(title)}</div>' if title else ""
        blocks.append(f'{label}<iframe class="chartframe" src="{chart_url}"></iframe>')
    return blocks


def _filter_stats(stats: list) -> list:
    """Keep only stat tiles that have a non-empty label and value."""
    valid = []
    for s in stats or []:
        if not isinstance(s, dict):
            continue
        label = str(s.get("label", "")).strip()
        value = str(s.get("value", "")).strip()
        if label and value:
            valid.append(s)
    return valid


async def _render_stats(stats: list) -> str:
    """Save the KPI stat-tile row to disk and return a raw <iframe> HTML string.
    Returns an empty string if there are no valid stats."""
    valid = _filter_stats(stats)
    if not valid:
        return ""
    os.makedirs(CHART_DIR, exist_ok=True)
    stats_id = uuid.uuid4().hex[:12]
    stats_file = Path(CHART_DIR) / f"stats-{stats_id}.html"
    await asyncio.to_thread(stats_file.write_text, _make_stats_html(valid), "utf-8")
    stats_url = f"{WEBSERVER_BASE_URL}/charts/stats-{stats_id}.html"
    return f'<iframe class="statsframe" src="{stats_url}"></iframe>'


def _build_plain_report(result: dict, insights: list) -> str:
    """Build a chart-free version of the report for embedding in downloadable files."""
    parts: list[str] = []
    if result.get("summary"):
        parts.append(f"## Data Overview\n\n{result['summary']}\n")
    if insights:
        parts.append("## Key Insights\n\n" + "\n".join(f"- {i}" for i in insights) + "\n")
    if result.get("markdown_report"):
        parts.append(result["markdown_report"])
    return "\n".join(parts).strip()


async def _build_combined_file(
    data_content: str, report_plain: str, file_format: str, file_name: str | None,
    charts: list | None = None, charts_heading: str = "",
) -> tuple[str | None, str | None]:
    """Ask mcp-office to combine the original data + report into one downloadable
    file (same format as the source). Returns (download_url, error_message) — one
    is always None. The analysis itself is returned regardless of a download failure."""
    if not data_content.strip():
        data_content = "(original file content was not provided to the analyzer)"

    payload = {
        "data_content": data_content,
        "report_markdown": report_plain,
        "file_format": file_format,
        "data_section_title": "Source Data",
        "report_section_title": "Analysis Report",
        "charts": charts or [],
        "charts_heading": charts_heading or "",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            r = await client.post(f"{MCP_OFFICE_URL}/build-report", json=payload)
            r.raise_for_status()
            url = r.json().get("url")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    return (url or None), None


# ---------------------------------------------------------------------------
# Live-progress widget. The tool returns instantly with an MCP-UI resource; the
# analysis runs as a background task writing a status file to CHART_DIR (served by
# the webserver, same origin as the widget → no CORS). The widget polls it, shows a
# live progress bar, then renders the finished report in place. LibreChat renders
# ui:// resources in an AUTO-RESIZING iframe (no internal scrollbar) — the page
# reports its content height via `ui-size-change` (scrollHeight, not the clamped
# frame height, which was the earlier bug).
# ---------------------------------------------------------------------------

_JOB_TASKS: set = set()
JOB_FILE_TTL = int(os.getenv("JOB_FILE_TTL", str(3 * 3600)))


def _esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_to_html(md: str) -> str:
    """Minimal, safe markdown → HTML (headings, bullets, bold, paragraphs)."""
    def bold(s: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    out: list[str] = []
    in_ul = False
    for line in (md or "").split("\n"):
        s = line.strip()
        if not s:
            if in_ul:
                out.append("</ul>"); in_ul = False
            continue
        if s.startswith("### "):
            if in_ul: out.append("</ul>"); in_ul = False
            out.append(f"<h5>{bold(_esc(s[4:]))}</h5>")
        elif s.startswith("## "):
            if in_ul: out.append("</ul>"); in_ul = False
            out.append(f"<h4>{bold(_esc(s[3:]))}</h4>")
        elif re.match(r"^[-*]\s+", s):
            if not in_ul: out.append("<ul>"); in_ul = True
            item = re.sub(r"^[-*]\s+", "", s)
            out.append(f"<li>{bold(_esc(item))}</li>")
        else:
            if in_ul: out.append("</ul>"); in_ul = False
            out.append(f"<p>{bold(_esc(s))}</p>")
    if in_ul:
        out.append("</ul>")
    return "".join(out)


def _write_job_sync(job_id: str, data: dict) -> None:
    (Path(CHART_DIR) / f"job-{job_id}.json").write_text(json.dumps(data), "utf-8")


async def _emit_job(job_id: str, data: dict) -> None:
    await asyncio.to_thread(_write_job_sync, job_id, data)


def _cleanup_widget_files() -> None:
    try:
        now = time.time()
        for f in Path(CHART_DIR).glob("*.*"):
            if f.name.startswith(("job-", "progress-", "chart-", "stats-", "geo-")):
                try:
                    if now - f.stat().st_mtime > JOB_FILE_TTL:
                        f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


_PROGRESS_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --surface:#fcfcfb;--plane:#f4f1ea;--ink:#17140d;--ink2:#52514e;
  --line:#ddd6c5;--accent:#b9542b;--good:#3f7d3f;--bad:#a83a2f;
}
@media (prefers-color-scheme:dark){
  :root{--surface:#1a1a19;--plane:#0d0d0d;--ink:#ffffff;--ink2:#c3c2b7;
    --line:#2c2c2a;--accent:#e07a4f;--good:#4fae4f;--bad:#e0705f;}
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{overflow:hidden}
body{background:transparent;color:var(--ink);padding:4px;
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
#progress{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.phead{display:flex;align-items:center;gap:10px;margin-bottom:12px;font-size:14px}
.phead #stage{flex:1;font-weight:500}
.phead #pct{font-variant-numeric:tabular-nums;color:var(--ink2);font-size:13px}
.spinner{width:15px;height:15px;border:2px solid var(--line);border-top-color:var(--accent);
  border-radius:50%;animation:spin .8s linear infinite;flex:0 0 auto}
@keyframes spin{to{transform:rotate(360deg)}}
.bar{height:8px;background:var(--line);border-radius:5px;overflow:hidden}
.bar>i{display:block;height:100%;width:3%;background:var(--accent);transition:width .3s ease}
.note{margin-top:10px;font-size:12px;color:var(--ink2)}
#error{display:none;background:var(--surface);border:1px solid var(--bad);border-radius:10px;
  padding:16px 18px;color:var(--bad);font-size:14px}
#report{margin-top:2px}
.rtitle{font-size:22px;font-weight:700;margin:2px 0 2px}
.summary{color:var(--ink2);margin:6px 0 2px;font-size:14px}
.sec-label{font-size:11px;letter-spacing:1.4px;text-transform:uppercase;color:var(--accent);
  font-weight:600;margin:20px 0 8px}
.chart-title{font-weight:600;font-size:14px;margin:8px 0 2px}
.chartframe{display:block;width:100%;height:340px;border:none;border-radius:8px;margin:2px 0 14px;background:transparent}
.statsframe{display:block;width:100%;height:150px;border:none;margin:2px 0 10px;background:transparent}
.insights{margin:4px 0 4px 18px}
.insights li{margin:5px 0;font-size:14px}
.report{font-size:14px}
.report h4{font-size:16px;margin:16px 0 6px}
.report h5{font-size:14px;margin:12px 0 4px}
.report p{margin:6px 0}
.report ul{margin:4px 0 8px 18px}
.report li{margin:4px 0}
.dl{display:inline-block;margin-top:16px;background:var(--ink);color:var(--plane);
  padding:11px 18px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px}
.dl:hover{background:var(--accent);color:#fff}
.dl-warn{margin-top:14px;color:var(--bad);font-size:13px}
</style>
</head>
<body>
<div id="progress">
  <div class="phead"><span class="spinner"></span><span id="stage">Starting…</span><span id="pct">0%</span></div>
  <div class="bar"><i id="fill"></i></div>
  <div class="note">Analyzing on-premise with the local AI model — this usually takes 1–2 minutes.</div>
</div>
<div id="report"></div>
<div id="error"></div>
<script>
(function(){
var JOB="__JOB__";
var stageEl=document.getElementById("stage"),pctEl=document.getElementById("pct"),
    fillEl=document.getElementById("fill"),progEl=document.getElementById("progress"),
    repEl=document.getElementById("report"),errEl=document.getElementById("error");
var stop=false,misses=0;
// Report CONTENT height (scrollHeight), not the clamped frame height, so the
// mcp-ui host grows the iframe to fit → no internal scrollbar. Only postMessage
// when the height ACTUALLY changes, and stop the polling interval once stable —
// broadcasting a same-value resize forever caused the host to re-apply height on
// every tick, which fights the user's manual scrolling (worse with several
// widgets on one page each polling independently).
var _lastSentH=0, _stableTicks=0, _sizeIntervalId=null;
function sendSize(){
  try{
    // Measure CONTENT height via body.scrollHeight — NOT documentElement.scrollHeight,
    // which is floored to the iframe viewport height and prevents the frame from
    // shrinking once the host makes it tall (large empty gap below the content).
    var h=(document.body?document.body.scrollHeight:0)+4;
    if(h<=4){return;}
    if(Math.abs(h-_lastSentH)<2){
      _stableTicks++;
      if(_stableTicks>=4 && _sizeIntervalId){clearInterval(_sizeIntervalId);_sizeIntervalId=null;}
      return;
    }
    _stableTicks=0;
    _lastSentH=h;
    parent.postMessage({type:"ui-size-change",payload:{height:h}},"*");
  }catch(e){}
}
window.addEventListener("load",function(){
  try{parent.postMessage({type:"ui-lifecycle-iframe-ready"},"*");}catch(e){}
  sendSize();
});
if(window.ResizeObserver){new ResizeObserver(sendSize).observe(document.body);}
_sizeIntervalId=setInterval(sendSize,1000);   // safety net for late-loading nested chart iframes
async function poll(){
  if(stop)return;
  try{
    var r=await fetch("job-"+JOB+".json?t="+Date.now(),{cache:"no-store"});
    if(r.ok){
      misses=0;
      var d=await r.json();
      if(d.status==="done"){
        stop=true;fillEl.style.width="100%";pctEl.textContent="100%";
        progEl.style.display="none";repEl.innerHTML=d.html||"";sendSize();
        setTimeout(sendSize,300);setTimeout(sendSize,1200);setTimeout(sendSize,2500);return;
      }else if(d.status==="error"){
        stop=true;progEl.style.display="none";
        errEl.style.display="block";errEl.textContent="⚠️ "+(d.message||"Analysis failed. Please try again.");sendSize();return;
      }else{
        stageEl.textContent=d.stage||"Working…";
        var p=Math.max(0,Math.min(100,d.percent||0));
        pctEl.textContent=p+"%";fillEl.style.width=p+"%";
      }
    }else if(r.status===404){misses++;}
  }catch(e){misses++;}
  if(misses>90){stop=true;progEl.style.display="none";
    errEl.style.display="block";errEl.textContent="⚠️ Lost contact with the analysis job. Please try again.";sendSize();return;}
  setTimeout(poll,1000);
}
poll();
})();
</script>
</body>
</html>"""


def _make_progress_html(job_id: str) -> str:
    return _PROGRESS_HTML.replace("__JOB__", job_id)


def _download_link_html(url: str | None, file_format: str, error: str | None) -> str:
    if url:
        fname = url.rsplit("/", 1)[-1]
        return (f'<a class="dl" href="{_esc(url)}" target="_blank" rel="noopener">'
                f'⬇ Download {_esc(fname)}</a>')
    if error:
        return f'<div class="dl-warn">The downloadable {_esc(file_format.upper())} file could not be generated: {_esc(error)}</div>'
    return ""


async def _build_report_fragment(result: dict, valid_charts: list, download_html: str,
                                 reused_from_cache: bool, file_name: str | None) -> str:
    parts: list[str] = []
    if reused_from_cache:
        ref = f" ({_esc(file_name)})" if file_name else ""
        parts.append(f'<div class="summary">Reusing the previously loaded file{ref} — no re-upload needed.</div>')
    parts.append(f'<div class="rtitle">{_esc(result.get("title") or "Analysis")}</div>')

    stats_iframe = await _render_stats(result.get("stats", []))
    if stats_iframe:
        parts.append('<div class="sec-label">Key Metrics</div>')
        parts.append(stats_iframe)

    if result.get("summary"):
        parts.append(f'<p class="summary">{_esc(result["summary"])}</p>')

    chart_iframes = await _render_charts(valid_charts)
    if chart_iframes:
        parts.append('<div class="sec-label">Charts</div>')
        parts.extend(chart_iframes)

    insights = result.get("insights", [])
    if insights:
        parts.append('<div class="sec-label">Key Insights</div><ul class="insights">')
        parts.extend(f"<li>{_esc(i)}</li>" for i in insights)
        parts.append("</ul>")

    if result.get("markdown_report"):
        parts.append(f'<div class="report">{_md_to_html(result["markdown_report"])}</div>')

    if download_html:
        parts.append('<div class="sec-label">Download</div>')
        parts.append(download_html)
    return "".join(parts)


async def _run_analysis_job(job_id: str, parts_in: list, prompt: str, raw_content: str,
                            file_name: str | None, reused_from_cache: bool) -> None:
    def running(pct: int, stage: str) -> dict:
        return {"status": "running", "percent": pct, "stage": stage}
    last = [0.0]

    async def on_gen(acc: str) -> None:
        now = time.monotonic()
        if now - last[0] < 0.5:
            return
        last[0] = now
        frac = min(1.0, len(acc) / 4000.0)
        await _emit_job(job_id, running(int(14 + 56 * frac), "Analyzing with the AI model…"))

    try:
        await _emit_job(job_id, running(8, "Reading your data…"))
        await _emit_job(job_id, running(14, "Analyzing with the AI model…"))
        result = await _call_qwen("\n\n".join(parts_in), prompt, on_progress=on_gen)

        await _emit_job(job_id, running(76, "Rendering charts & metrics…"))
        valid_charts = _filter_charts(result.get("charts", []))

        await _emit_job(job_id, running(87, "Building the downloadable report…"))
        report_plain = _build_plain_report(result, result.get("insights", []))
        detected_format = _detect_format(file_name)
        if valid_charts:
            detected_format = "xlsx"
        charts_heading = (result.get("title") or prompt or "Analysis").strip()
        url, dl_err = await _build_combined_file(
            raw_content or "\n\n".join(parts_in), report_plain, detected_format, file_name,
            charts=valid_charts, charts_heading=charts_heading,
        )
        download_html = _download_link_html(url, detected_format, dl_err)

        await _emit_job(job_id, running(96, "Finalizing…"))
        fragment = await _build_report_fragment(result, valid_charts, download_html,
                                                reused_from_cache, file_name)
        await _emit_job(job_id, {"status": "done", "percent": 100, "stage": "Complete", "html": fragment})
    except json.JSONDecodeError:
        await _emit_job(job_id, {"status": "error",
                                 "message": "The AI returned malformed output. Please try again."})
    except (httpx.ConnectError, httpx.ConnectTimeout):
        await _emit_job(job_id, {"status": "error",
                                 "message": "Couldn't reach the AI model (it may be starting up). Please try again in a moment."})
    except httpx.TimeoutException:
        await _emit_job(job_id, {"status": "error",
                                 "message": "The analysis took too long and timed out. Try a smaller or more focused request."})
    except Exception as e:
        logger.exception("Analysis job %s failed", job_id)
        detail = f"{type(e).__name__}: {e}".strip().rstrip(":").strip()
        await _emit_job(job_id, {"status": "error", "message": detail})


@mcp.tool()
async def run_file_analytics(
    prompt: str,
    file_url: str | None = None,
    file_name: str | None = None,
    file_content: str | None = None,
    session_id: str | None = None,
) -> str | EmbeddedResource:
    """
    TRIGGER: Call this tool IMMEDIATELY whenever a file, document, spreadsheet, or attachment
    is present in the conversation — CSV, Excel (.xlsx/.xls), Word (.docx/.doc), PDF, PowerPoint
    (.pptx), plain text (.txt), JSON, or any other file format.
    DO NOT call run_analytics when a file is uploaded — this is the correct tool for all file analysis.

    Analyzes the uploaded file using the local Qwen AI model (offline, data stays on-premises).
    Pipeline: reads document → cleans/preprocesses → extracts insights → renders interactive
    Chart.js visualizations with REAL data values from the file → automatically combines the
    original data + full analysis into ONE downloadable file in the SAME format as the source
    and appends a download link.

    This tool is SELF-CONTAINED: it returns a single interactive UI widget that shows a live
    progress bar and then renders the FULL analysis (KPIs, charts, insights, download link) by
    itself. After it returns, reply with ONLY the UI resource marker (\\ui{id}) and NOTHING else —
    no text, no summary, no download URLs. Do NOT call any other tool afterwards.

    FOLLOW-UP QUESTIONS (IMPORTANT): the file's content is cached after the first analysis.
    For any further question about the SAME file, call this tool again with the SAME file_name
    (and file_url if you have it) and the new question as `prompt`. You do NOT need to re-read or
    re-send file_content — pass just the prompt + file_name and the cached content is reused.

    Args:
        prompt: What the user wants to analyze or understand about the file.
        file_url: Public URL of the uploaded file (pass if available from LibreChat).
        file_name: Filename including extension — used to auto-detect output format AND as the
            cache key for follow-up questions. Always pass it when known.
        file_content: Extracted text/table content. Pass on the FIRST analysis of a file; omit on
            follow-ups (the cached content is used automatically).
        session_id: Optional conversation/session identifier to scope the cache. Pass the same
            value across a conversation if available; otherwise the file_name/file_url is the key.
    """
    raw_content, parts, reused_from_cache = await _resolve_file_content(
        session_id, file_url, file_name, file_content
    )

    if not raw_content.strip():
        return (
            "⚠️ No file content received and nothing is cached for this file yet. "
            "Please attach the file (or re-attach it) and ask again."
        )

    # Launch the analysis as a background job and return a live-progress widget
    # (an MCP-UI resource) immediately — the chat shows a progress bar that grows
    # to fit and then renders the finished report in place.
    os.makedirs(CHART_DIR, exist_ok=True)
    await asyncio.to_thread(_cleanup_widget_files)
    job_id = uuid.uuid4().hex[:12]
    await _emit_job(job_id, {"status": "running", "percent": 3, "stage": "Queued…"})
    await asyncio.to_thread(
        (Path(CHART_DIR) / f"progress-{job_id}.html").write_text,
        _make_progress_html(job_id), "utf-8",
    )
    task = asyncio.create_task(
        _run_analysis_job(job_id, parts, prompt, raw_content, file_name, reused_from_cache)
    )
    _JOB_TASKS.add(task)
    task.add_done_callback(_JOB_TASKS.discard)

    progress_url = f"{WEBSERVER_BASE_URL}/charts/progress-{job_id}.html"
    return EmbeddedResource(
        type="resource",
        resource=TextResourceContents(
            uri=f"ui://analytics/{job_id}",
            mimeType="text/uri-list",
            text=progress_url,
        ),
    )


# ---------------------------------------------------------------------------
# Adaptive geographic visualization (render_geo_analysis).
#
# Deterministic — NOT the LLM. geo_analysis.build_geo_result runs the 7-step
# decision (detect granularity → check boundary availability → pick color
# encoding → hub-bubble / table fallbacks → drill-down data → currency
# normalization) and geo_html.make_geo_html renders it as one self-contained
# D3 + TopoJSON page. The tool returns instantly (no model call) with an MCP-UI
# resource, mirroring run_file_analytics' widget delivery.
# ---------------------------------------------------------------------------

def _rows_from_csv(text: str) -> list[dict]:
    """Parse CSV/TSV text into a list of row dicts. Sniffs the delimiter. Returns []
    on any malformed input (NUL bytes, over-limit fields, etc.) rather than raising."""
    # NUL bytes make the C csv reader raise mid-iteration; strip them up front.
    text = text.replace("\x00", "").strip()
    if not text:
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = [dict(r) for r in reader]
    except csv.Error:
        return []
    # Drop a phantom None key DictReader adds for ragged rows.
    for r in rows:
        r.pop(None, None)
    return rows


def _coerce_rows(data, file_content: str | None) -> tuple[list[dict], str | None]:
    """Turn the tool input into a list of row dicts. Accepts a real JSON array,
    a JSON-array string, or CSV/TSV text (via `data` or `file_content`).
    Returns (rows, error_message)."""
    # 1) Already a list of dicts.
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
        if rows:
            return rows, None
        if data:
            return [], "The `data` array does not contain row objects (expected [{col: val, ...}, ...])."

    # 2) A string in `data` — try JSON first, then CSV.
    for candidate in (data if isinstance(data, str) else None, file_content):
        if not candidate or not str(candidate).strip():
            continue
        s = str(candidate).strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
                parsed = parsed["rows"]
            if isinstance(parsed, list):
                rows = [r for r in parsed if isinstance(r, dict)]
                if rows:
                    return rows, None
        except (ValueError, RecursionError):  # bad/huge/deeply-nested JSON → try CSV
            pass
        rows = _rows_from_csv(s)
        if rows:
            return rows, None

    return [], ("No tabular data received. Pass `data` as an array of row objects "
                "(e.g. [{\"country\": \"India\", \"revenue\": 1200}, ...]) or provide CSV "
                "text via `file_content`.")


def _geo_contract_text(result: dict) -> str:
    """The OUTPUT CONTRACT as plain text, for logs / non-UI fallback."""
    lines = [f"Visual chosen: {result.get('visual')} — {result.get('why', '')}"]
    gran = result.get("granularity") or {}
    if gran:
        lines.append(f"Granularity: {gran.get('level')} "
                     f"(confidence {gran.get('confidence')}). {gran.get('notes', '') or ''}".strip())
    topo = result.get("topology") or {}
    lines.append(f"Boundary/topology source: {topo.get('url', 'none (table fallback)')}")
    warns = result.get("warnings") or []
    if warns:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in warns)
    else:
        lines.append("Warnings: none — every location matched.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Downloadable Excel export for render_geo_analysis: a server-side screenshot
# of the rendered map (headless Chromium) + a native editable Bubble Chart +
# the source data, combined into one xlsx by mcp-office. Best-effort only —
# any failure here is logged and skipped; the interactive widget always
# returns successfully regardless of whether the download link could be built.
# ---------------------------------------------------------------------------

def _screenshot_geo_widget(file_path) -> bytes | None:
    """Launch headless Chromium, load the widget in static mode (no drill
    panel, full-width map), wait for it to finish drawing, and return a PNG of
    the rendered content. SYNCHRONOUS — call via asyncio.to_thread. Returns
    None on any failure (Playwright/Chromium missing, CDN unreachable,
    timeout, ...) — the download link is a nice-to-have, never a reason to
    fail the whole tool call."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info("playwright not installed; skipping geo map screenshot")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 900, "height": 1200})
                # Set BEFORE the widget's own script runs — reliable regardless of
                # URL scheme, unlike a fragile file://…?query string.
                page.add_init_script("window.__FORCE_STATIC__ = true;")
                page.goto(f"file://{file_path}", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_function("window.__RENDER_READY__ === true", timeout=15000)
                page.wait_for_timeout(200)  # let the last paint settle
                root = page.query_selector("#root")
                return root.screenshot() if root else page.screenshot()
            finally:
                browser.close()
    except Exception:
        logger.exception("geo widget screenshot failed; download link will be skipped")
        return None


async def _build_geo_excel(result: dict, raw_rows: list, location_field: str,
                           value_field: str, group_field: str | None,
                           title: str, image_bytes: bytes | None) -> str | None:
    """POST to mcp-office's /build-geo-report to combine the map picture, a
    native editable Bubble Chart, and the source data into one xlsx. Returns
    the download URL, or None on any failure (logged, never raised)."""
    payload = {
        "features": result.get("features", []),
        "raw_rows": raw_rows,
        "location_field": location_field,
        "value_field": value_field,
        "group_field": group_field,
        "title": title,
        "why": result.get("why", ""),
        "currency_note": result.get("currency_note", ""),
        "warnings": result.get("warnings", []),
    }
    if image_bytes:
        payload["map_image_base64"] = base64.b64encode(image_bytes).decode("ascii")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            r = await client.post(f"{MCP_OFFICE_URL}/build-geo-report", json=payload)
            r.raise_for_status()
            return r.json().get("url")
    except Exception as e:
        logger.warning("geo excel export failed: %s: %s", type(e).__name__, e)
        return None


@mcp.tool()
async def render_geo_analysis(
    location_field: str,
    value_field: str,
    data: list | str | None = None,
    group_field: str | None = None,
    currency_field: str | None = None,
    country_field: str | None = None,
    color_mode: str = "auto",
    file_content: str | None = None,
    title: str | None = None,
    low_n: int = 3,
    target_currency: str = "USD",
) -> str | EmbeddedResource:
    """
    TRIGGER: Call this whenever the user wants to SEE data ON A MAP or asks a
    geographic "where" question — e.g. "map revenue by country", "which state has the
    highest sales", "plot our offices", "sales by region on a map", "heatmap of
    orders by city", "compare quotes across countries geographically".

    Renders geographic data at the correct zoom level AUTOMATICALLY, and falls back to
    a sortable table when a map would mislead. It is DETERMINISTIC (no AI model) and
    returns instantly. Steps it performs:
      1. Detects the location granularity — country / state-province / city-point /
         informal zone — and flags ambiguous values (e.g. "Georgia").
      2. Verifies boundary/topology data actually EXISTS for that granularity before
         promising a map (world countries always; US states available; other
         subdivisions → hub-bubble fallback).
      3. Chooses color encoding: sequential gradient for "best/worst value" questions,
         categorical (winner-per-location) when a group/category field is given.
      4. Falls back to representative hub-city BUBBLES for zone/region labels or
         missing subdivision boundaries (never forces informal zones onto a choropleth).
      5. Falls back to a sortable TABLE when a map would mislead (no usable location
         field, or coordinates can't be resolved).
      6. Every rendered location is CLICKABLE → shows the ranked entity list, sample
         size (n), and the fields used, so a single overweighted point can be spotted.
      7. Normalizes MIXED CURRENCIES to one currency before coloring (states the rate).

    Returns a single interactive UI widget (map/bubbles/table + a "why this visual"
    panel + a warning list for anything that couldn't be matched), and — best
    effort, may take a few extra seconds — a "Download" link in the widget for an
    Excel file containing: a picture of the rendered map, a native EDITABLE Excel
    Bubble Chart (positioned by real coordinates; edit a value/coordinate cell and
    it updates live in Excel), and the full source data on its own sheet. If the
    download link doesn't appear, the interactive widget above is still complete —
    the export is a bonus, never a reason the tool call would fail.

    After it returns, reply with ONLY the UI resource marker (\\ui{id}) and nothing else.

    Args:
        location_field: Column name holding the location (country/state/city/zone/lat,lon).
        value_field: Column name holding the numeric value to visualize (sum per location).
        data: The dataset — an array of row objects [{col: val, ...}, ...]. A JSON-array
            string or CSV text is also accepted; CSV may instead be passed as file_content.
        group_field: Optional category/vendor/entity column → categorical "winner per
            location" coloring + per-location ranking in the drill-down.
        currency_field: Optional column naming each row's currency code (for Step 7).
        country_field: Optional column naming the country — used to disambiguate values
            that are BOTH a US state and a country (e.g. "Georgia").
        color_mode: "auto" (default), "sequential", or "categorical".
        file_content: CSV/JSON text alternative to `data`.
        title: Optional heading for the widget.
        low_n: Sample-size threshold; locations with fewer rows are de-emphasized (default 3).
        target_currency: Currency to normalize to when mixing currencies (default USD).
    """
    # Whole body is wrapped: this tool must NEVER raise to the MCP layer — any
    # unforeseen input returns a clear message instead of an error the user sees.
    try:
        rows, err = _coerce_rows(data, file_content)
        if err:
            return f"⚠️ {err}"

        keys = {k for r in rows for k in r}
        if location_field not in keys:
            return (f"⚠️ Location field '{location_field}' is not in the data. "
                    f"Available columns: {sorted(str(k) for k in keys)}")
        if value_field not in keys:
            return (f"⚠️ Value field '{value_field}' is not in the data. "
                    f"Available columns: {sorted(str(k) for k in keys)}")

        # STEP 1 (ask-branch): if a value is genuinely ambiguous (US state OR country)
        # and no country column was supplied to disambiguate, flag it and ASK rather
        # than silently guessing which interpretation to map.
        try:
            pre = detect_granularity(rows, location_field, country_field=country_field)
        except Exception:
            pre = {}
        if pre.get("ambiguous") and not country_field and not (data is None and file_content is None):
            vals = ", ".join(str(v) for v in pre["ambiguous"])
            return (
                f"⚠️ The location value(s) **{vals}** are ambiguous — each could be a US "
                f"state OR a country, and there's no country column to disambiguate. "
                f"Please tell me which you mean (US states vs. countries), or add a "
                f"`country_field` so I map the right boundaries instead of guessing."
            )

        result = build_geo_result(
            rows, location_field, value_field,
            group_field=group_field, currency_field=currency_field,
            country_field=country_field, color_mode=color_mode,
            low_n=low_n, target_currency=target_currency,
        )
        logger.info("render_geo_analysis: %s", _geo_contract_text(result).replace("\n", " | "))

        heading = title or f"{value_field} by {location_field}"
        html = make_geo_html(result, heading)

        os.makedirs(CHART_DIR, exist_ok=True)
        await asyncio.to_thread(_cleanup_widget_files)
        geo_id = uuid.uuid4().hex[:12]
        file_path = Path(CHART_DIR) / f"geo-{geo_id}.html"
        await asyncio.to_thread(file_path.write_text, html, "utf-8")

        # Best-effort: screenshot the map + build a downloadable Excel (map
        # picture + a native, genuinely editable Bubble Chart + the source
        # data). Never blocks the core feature — any failure here just means
        # no download link is added, not a broken widget.
        try:
            png_bytes = await asyncio.to_thread(_screenshot_geo_widget, file_path)
            download_url = await _build_geo_excel(
                result, rows, location_field, value_field, group_field, heading, png_bytes,
            )
            if download_url:
                html = make_geo_html(result, heading, download_url=download_url)
                await asyncio.to_thread(file_path.write_text, html, "utf-8")
        except Exception:
            logger.exception("geo excel export pipeline failed; continuing without a download link")

        geo_url = f"{WEBSERVER_BASE_URL}/charts/geo-{geo_id}.html"
        return EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                uri=f"ui://analytics/geo-{geo_id}",
                mimeType="text/uri-list",
                text=geo_url,
            ),
        )
    except Exception as e:
        logger.exception("render_geo_analysis failed")
        return (f"⚠️ Sorry — the geographic view couldn't be generated "
                f"({type(e).__name__}). Please check the location/value fields and try again.")


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        path="/mcp",
    )
