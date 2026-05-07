import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from .config import model
from .utils_web import fetch_page_text
from .utils_text import clean, parse_json_insight_to_table, parse_and_repair
import pandas as pd

# --------------------------
# Helpers
# --------------------------

def chunk_text(text, chunk_size=3000, overlap=200):
    """Split text into chunks with overlap to preserve context."""
    text = text.replace("\n", " ").replace("\r", " ")
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

def audit_landing_page_with_gemini(url, html_text, metrics=None):
    """
    Runs a CRO/Google Ads landing page audit using Gemini.
    Always returns valid JSON array.
    """
    # Prepare metrics snippet
    metric_note = ""
    if metrics is not None and isinstance(metrics, (dict, pd.Series)):
        metric_note = f"""
Keyword Performance:
- Clicks: {metrics.get('Clicks', 0)}
- Conversions: {metrics.get('Conversions', 0)}
- Cost: {metrics.get('Cost', 0):.2f}
- CPA: {metrics.get('CPA ($)', 0):.2f}
- CTR: {metrics.get('CTR', 0):.2%}
"""

    # Break HTML/text into chunks - limit to first 2 to save time/cost
    chunks = chunk_text(html_text.strip(), chunk_size=3000)[:2]

    all_rows = []
    for i, chunk in enumerate(chunks, 1):
        prompt = f"""
You are a CRO and Google Ads landing page consultant.
Audit the page at {url} using the performance data and HTML content.
Critically audit the landing page experience from a conversion perspective.
- Tailor feedback based on performance and content.
- Avoid generic advice.
- Highlight specific issues like weak CTAs, unclear messaging, UX blockers, mismatch with ad intent, or lack of trust signals.
Rules:
- Output must be a valid JSON array. No markdown, no extra text.
- Each object must contain "Characteristic", "Insight", and "Recommendation".
- Each field is a single string (no newlines inside values).
- Include at least one row with "Characteristic": "Competitor Benchmark" if missing.
- Only include high-impact, actionable insights.
- Do not hallucinate or make up data. Strictly use the data provided.
- Provide a summary of key actionables that are not at all even slightest generic at the end
- The insights provided shall be the result of deep level analysis of the data provided considering various aspects such as trends, patterns, anomalies, and correlations within the data and metric values.
Page Performance Data:
{metric_note}

Page Content (chunk {i}/{len(chunks)}):
{chunk}
"""
        try:
            raw = (model.generate_content(prompt).text or "").strip()
        except Exception as e:
            print(f"[ERROR] Gemini LP audit API error for {url} (chunk {i}): {e}")
            continue

        if not raw:
            print(f"[WARN] Gemini returned empty for {url} (chunk {i})")
            continue

        rows = parse_and_repair(raw)
        if rows:
            all_rows.extend(rows)

    # Remove duplicates by tuple of values
    seen = set()
    unique_rows = []
    for row in all_rows:
        key = (row.get("Characteristic"), row.get("Insight"), row.get("Recommendation"))
        if key not in seen:
            seen.add(key)
            row["URL"] = url  # Add URL to each row
            unique_rows.append(row)

    return json.dumps(unique_rows, ensure_ascii=False)

def run_landing_page_audits(df_lp, max_workers=5):
    """
    Runs landing page audits in parallel for all rows in df_lp.
    Returns a list of JSON strings with audit results.
    """
    insights = []
    if df_lp is None or df_lp.empty:
        return insights

    def process_row(idx_row):
        url = idx_row.get("Final URL", "")
        if not url:
            return None
        html_text = fetch_page_text(url)
        if not html_text:
            return None
        print(f"[INFO] Auditing LP (parallel): {url}")
        return audit_landing_page_with_gemini(url, html_text, idx_row)

    rows = [row for _, row in df_lp.iterrows()]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_row, row): row for row in rows}
        for future in as_completed(futures):
            try:
                result = future.result()
                if isinstance(result, str) and result.strip():
                    insights.append(result.strip())
            except Exception as e:
                print("[WARN] LP audit thread error:", e)

    return insights
