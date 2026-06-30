import os
import re
import json
import uuid
import asyncio
from pathlib import Path
import httpx
from fastmcp import FastMCP

PORT = int(os.getenv("MCP_PORT", "8959"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen3:8b")
MAX_DATA_CHARS = int(os.getenv("MAX_DATA_CHARS", "40000"))
CHART_DIR = os.getenv("CHART_DIR", "/app/charts")
WEBSERVER_BASE_URL = os.getenv("WEBSERVER_BASE_URL", "http://10.10.30.160:8000").rstrip("/")

mcp = FastMCP(
    name="Meril Analytics",
    instructions=(
        "This server provides local file analysis via the run_file_analytics tool.\n\n"
        "Use run_file_analytics when the user has uploaded ANY file "
        "(CSV, Excel, PDF, Word, image, or any attachment). "
        "It runs the analysis locally using the Qwen AI model."
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


_FILE_ANALYSIS_SYSTEM = """\
You are an expert data analyst. Analyze the provided data and produce a complete analysis with charts.

Respond ONLY with a single valid JSON object — no markdown fences, no text before or after.

Required JSON structure:
{
  "summary": "2-3 sentences: what the dataset is about, its domain, and scope",
  "preprocessing_notes": "brief note on any cleaning done (nulls, duplicates, type fixes)",
  "insights": [
    "Insight 1: specific finding with actual numbers from the data",
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
          "datasets": [{"label": "Series name", "data": [10, 20], "backgroundColor": ["#4e79a7","#f28e2b"]}]
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

Chart rules (follow exactly):
- Generate 2 to 4 charts that give the most insight into THIS specific data
- Chart.js v3 format only
- Use ACTUAL values from the dataset, never placeholder numbers
- Chart types: "bar" for comparisons, "line" for time-series/trends, "pie" for proportions (≤7 slices), "doughnut" for proportions, "radar" for multi-variable
- Horizontal bar: use type "bar" with options.indexAxis = "y"
- Limit each chart to 15 data points max — aggregate/summarize if the dataset is larger
- Colors to use: #4e79a7 #f28e2b #e15759 #76b7b2 #59a14f #edc948 #b07aa1 #ff9da7 #9c755f
- Each dataset needs backgroundColor (array) and for line charts also borderColor
- If user specifies what to focus on, prioritize those dimensions in charts and insights"""

_THINK_TAG_RE = re.compile(r'<think>.*?</think>', re.DOTALL)

_FILE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "preprocessing_notes": {"type": "string"},
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
    "required": ["summary", "insights", "charts", "markdown_report"],
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


async def _call_qwen(content: str, user_prompt: str) -> dict:
    user_msg = f"User request: {user_prompt}\n\nData:\n{_truncate_data(content)}\n\n/no_think"
    async with httpx.AsyncClient(timeout=httpx.Timeout(270.0, connect=10.0)) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": QWEN_MODEL,
                "messages": [
                    {"role": "system", "content": _FILE_ANALYSIS_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "think": False,
                "format": _FILE_ANALYSIS_SCHEMA,
                "options": {"temperature": 0.1, "num_ctx": 16384},
            },
        )
        resp.raise_for_status()
        original_raw = resp.json()["message"]["content"]

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


async def _render_charts(charts: list) -> list[str]:
    """Save each chart to disk and return iframe markdown blocks."""
    blocks: list[str] = []
    os.makedirs(CHART_DIR, exist_ok=True)
    for chart in charts:
        config = chart.get("config")
        if not isinstance(config, dict):
            continue
        title = chart.get("title", "")
        chart_id = uuid.uuid4().hex[:12]
        chart_file = Path(CHART_DIR) / f"chart-{chart_id}.html"
        await asyncio.to_thread(chart_file.write_text, _make_artifact_html(config), "utf-8")
        chart_url = f"{WEBSERVER_BASE_URL}/charts/chart-{chart_id}.html"
        label = f"**{title}**\n" if title else ""
        blocks.append(
            f"{label}"
            f'```html\n<iframe src="{chart_url}" style="width:100%;height:380px;border:none;border-radius:8px;"></iframe>\n```'
        )
    return blocks


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


def _build_handoff(detected_format: str, report_plain: str) -> str:
    return (
        f"\n\n---\n"
        f"**NEXT STEP — call `create_report_with_source_data` now:**\n"
        f"- `data_content` = the original file content passed to this tool\n"
        f"- `report_markdown` = the analysis above (without chart iframes)\n"
        f"- `file_format` = `{detected_format}`\n"
        f"- `data_section_title` = `Source Data`\n"
        f"- `report_section_title` = `Analysis Report`\n"
        f"\n*Report content for file builder (plain, no iframes):*\n"
        f"<!-- REPORT_PLAIN_START -->\n{report_plain}\n<!-- REPORT_PLAIN_END -->"
    )


@mcp.tool()
async def run_file_analytics(
    prompt: str,
    file_url: str | None = None,
    file_name: str | None = None,
    file_content: str | None = None,
) -> str:
    """
    Analyze an uploaded file using the local Qwen AI model (qwen3:8b).

    ALWAYS use this tool when the user has uploaded ANY file:
    CSV, Excel, PDF, Word document, text file, or any other attachment.

    The pipeline: reads the full document → preprocesses/cleans → finds insights →
    picks the best chart types → renders interactive Chart.js visualizations.

    IMPORTANT — after this tool returns, you MUST call `create_report_with_source_data`
    from the mcp-office-docs server using:
      - data_content  = the original file content (file_content argument or fetched text)
      - report_markdown = the full analysis text returned by this tool (without chart iframes)
      - file_format   = the file's extension (e.g. "xlsx", "docx", "pdf", "pptx", "csv", "txt")
      - data_section_title  = "Source Data"
      - report_section_title = "Analysis Report"
    This creates a combined downloadable file and gives the user a download link.
    The same applies when the source data comes from the web instead of an uploaded file —
    use "xlsx" or "docx" as the format and pass the fetched content as data_content.

    Args:
        prompt: What the user wants to analyze or understand about the file.
        file_url: Public URL of the uploaded file (pass if available).
        file_name: Filename of the uploaded file (pass if available).
        file_content: Extracted text/table content from the file (pass if available).
    """
    _, parts = await _load_file_content(file_content, file_url, file_name)

    if not parts:
        return (
            "⚠️ No file content received. Make sure the file uploaded successfully "
            "and that LibreChat is passing the file content to the analysis tool."
        )

    try:
        result = await _call_qwen("\n\n".join(parts), prompt)
    except json.JSONDecodeError as e:
        return f"⚠️ **Analysis failed:** Qwen returned invalid JSON.\n\n```\n{e}\n```"
    except httpx.ConnectError:
        return f"⚠️ **Cannot reach Ollama** at `{OLLAMA_URL}`. Check that the ollama container is running."
    except httpx.HTTPStatusError as e:
        return f"⚠️ **Ollama error** — HTTP {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return f"⚠️ **Analysis error:** {e}"

    lines: list[str] = []

    if result.get("summary"):
        lines.append(f"## 📊 Data Overview\n\n{result['summary']}\n")
    if result.get("preprocessing_notes"):
        lines.append(f"*Preprocessing: {result['preprocessing_notes']}*\n")

    chart_blocks = await _render_charts(result.get("charts", []))
    if chart_blocks:
        lines.append("## 📈 Charts\n\n" + "\n\n".join(chart_blocks) + "\n")

    insights = result.get("insights", [])
    if insights:
        lines.append("## 💡 Key Insights\n\n" + "\n".join(f"- {i}" for i in insights) + "\n")
    if result.get("markdown_report"):
        lines.append(result["markdown_report"])

    analysis_text = "\n".join(lines).strip()
    report_plain = _build_plain_report(result, insights)
    handoff = _build_handoff(_detect_format(file_name), report_plain)

    return analysis_text + handoff


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        path="/mcp",
    )
