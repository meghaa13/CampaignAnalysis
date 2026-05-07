from .utils_text import clean
from .config import model
import json
import re

def gemini_hourly_summary(df):
    if df is None or df.empty:
        return "[]"

    prompt = f"""
You're a Google Ads dayparting analyst.
Based on the following hourly performance data, identify key pattern and optimization opportunities. 

Analyze this hourly performance table and return a **valid JSON array** only.

Rules:
- Each object must have "Characteristic", "Insight", "Recommendation".
- Strictly No markdown, no extra commentary.
- Keep each value as a single string.
- Only include meaningful, actionable rows.
- Do not hallucinate or make up data. Strictly use the data provided.
- The insights provided shall be the result of deep level analysis of the data provided considering various aspects such as trends, patterns, anomalies, and correlations within the data and metric values.

Data:
{df.to_string(index=False)}
"""

    raw = ""
    try:
        raw = model.generate_content(prompt).text.strip()
        # --- strip ```json ... ``` fences if present ---
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        cleaned = clean(cleaned)
        data = json.loads(cleaned)
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Gemini API error in hourly summary: {e}\nRaw: {raw[:200]}")
        return "[]"