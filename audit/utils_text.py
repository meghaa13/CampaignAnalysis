import re
import json
import pandas as pd

def clean(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"[*_`]+", "", text).strip()

def _normalize_records(records):
    """
    Ensure all records have the schema:
    Characteristic, Insight, Recommendation
    """
    normalized = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        normalized.append({
            "Characteristic": rec.get("Characteristic") or rec.get("characteristic") or rec.get("Topic") or "",
            "Insight": rec.get("Insight") or rec.get("insight") or rec.get("Observation") or "",
            "Recommendation": rec.get("Recommendation") or rec.get("recommendation") or rec.get("Action") or "",
        })
    return normalized

def compress_to_table_format(insight_text):
    lines = [l for l in insight_text.splitlines() if l.strip() and '|' in l]
    structured = []
    for line in lines:
        cols = [col.strip() for col in line.split('|')]
        if len(cols) == 4:
            structured.append((cols[1], cols[2], cols[3]))  # Skip first col
        elif len(cols) == 3:
            structured.append(tuple(cols))
    return structured

def safe_parse_gemini_json(raw_text):
    """
    Attempt to robustly parse Gemini's JSON output.
    Falls back to extracting the first JSON array found.
    """
    if not raw_text:
        return []
    raw_text = raw_text.strip()

    # First attempt: direct parse
    try:
        return json.loads(raw_text)
    except Exception:
        pass

    # Second attempt: extract JSON array via regex
    match = re.search(r"\[.*\]", raw_text, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # Could not parse
    return []

def parse_json_insight_to_table(text):
    """
    Tries multiple ways to parse Gemini output into a DataFrame.
    Works for list/dict objects, JSON strings, pipe tables, or fallback raw text.
    Never raises.
    """
    # ── Handle non-string inputs directly (list or dict already parsed) ──────
    if isinstance(text, list):
        if len(text) == 0:
            return pd.DataFrame(columns=["Characteristic", "Insight", "Recommendation"])
        if all(isinstance(d, dict) for d in text):
            return pd.DataFrame(text)
        # list of lists
        if all(isinstance(d, (list, tuple)) for d in text):
            return pd.DataFrame(text, columns=["Characteristic", "Insight", "Recommendation"])
        return pd.DataFrame(columns=["Characteristic", "Insight", "Recommendation"])

    if isinstance(text, dict):
        return pd.DataFrame([text])

    if not text or not isinstance(text, str):
        return pd.DataFrame(columns=["Characteristic", "Insight", "Recommendation"])

    cleaned = text.strip().replace("“", '"').replace("”", '"').replace("’", "'")

    # 1) Try JSON first
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            if all(isinstance(d, dict) for d in data):
                return pd.DataFrame(data)
            if all(isinstance(d, (list, tuple)) for d in data):
                return pd.DataFrame(data, columns=["Characteristic", "Insight", "Recommendation"])
    except Exception:
        pass

    # 2) Try pipe-separated
    pipe_rows = []
    for line in cleaned.splitlines():
        if "|" in line:
            parts = [p.strip("•*- \t") for p in line.split("|")]
            if len(parts) >= 3:
                pipe_rows.append(parts[:3])
    if pipe_rows:
        return pd.DataFrame(pipe_rows, columns=["Characteristic", "Insight", "Recommendation"])

    # 3) Try colon/dash/arrow separated
    colon_rows = []
    for line in cleaned.splitlines():
        parts = re.split(r"[:\-→]{1}", line)
        if len(parts) >= 3:
            colon_rows.append([p.strip() for p in parts[:3]])
    if colon_rows:
        return pd.DataFrame(colon_rows, columns=["Characteristic", "Insight", "Recommendation"])

    # 4) Fallback: tab or multi-space
    for sep in ["\t", "  "]:
        fallback_rows = []
        for line in cleaned.splitlines():
            parts = [p.strip() for p in line.split(sep) if p.strip()]
            if len(parts) >= 3:
                fallback_rows.append(parts[:3])
        if fallback_rows:
            return pd.DataFrame(fallback_rows, columns=["Characteristic", "Insight", "Recommendation"])

    return None

def parse_and_repair(raw):
    """
    Robust parser for Gemini output.
    Always returns list[dict] with schema: Characteristic, Insight, Recommendation
    """
    if not raw:
        return []

    # Strip markdown fences
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    # Fix newlines inside strings
    raw = re.sub(
        r'(?<=")([^"]*?)\n([^"]*?)(?=")',
        lambda m: m.group(0).replace("\n", " "),
        raw
    )

    # Try JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            return _normalize_records(parsed)
    except Exception:
        pass

    # Try extracting array
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return _normalize_records(parsed)
        except Exception:
            pass

    # Fallback to table parser
    df = parse_json_insight_to_table(raw)
    if not df.empty:
        return df.to_dict(orient="records")

    return []

from datetime import datetime

def format_date(date_str):
    """
    Ensures date is in YYYY-MM-DD format.
    Supports both YYYY-MM-DD and YYYYMMDD inputs.
    """
    if "-" not in date_str:  # means it's YYYYMMDD
        return datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
    return date_str