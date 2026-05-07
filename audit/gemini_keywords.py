from .utils_text import clean, parse_and_repair
from .config import model
import json

def gemini_keyword_summary(df):
    if df is None or df.empty:
        return "[]"
    prompt = f"""
You're a Google Ads keyword analyst.
Given the keyword data below, return a JSON array of insights. Each object should include:
- "Characteristic"
- "Insight" (with actual metric values, keyword names, specific numbers)
- "Recommendation" (tactical, specific — not generic)
Avoid generic advice. Focus on actionable insights.
Do not hallucinate or make up data. Strictly use the data provided.
The insights provided shall be the result of deep level analysis considering trends,
patterns, anomalies, and correlations within the data and metric values.
Output ONLY a valid JSON array. No markdown. No text outside the array.
Data:
{df.head(50).to_string(index=False)}
"""
    raw = ""
    try:
        raw = model.generate_content(prompt).text or ""
        parsed = parse_and_repair(raw)
        if isinstance(parsed, list) and len(parsed) > 0:
            return json.dumps(parsed, ensure_ascii=False)
        return clean(raw)
    except Exception as e:
        print(f"[ERROR] gemini_keyword_summary: {e}")
        return "[]"