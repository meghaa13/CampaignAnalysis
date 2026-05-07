from .utils_text import clean, parse_and_repair
from .config import model

def gemini_summary(df, label="Campaigns"):
    """
    Returns a JSON **string** (list[dict]) with clean fields:
      - Characteristic
      - Insight
      - Recommendation
    Always valid JSON. Never markdown. Never multi-line field breaks.
    """
    # Defensive: if df empty, return empty JSON array
    try:
        data_str = df.head(30).to_string(index=False)
    except Exception:
        data_str = ""

    prompt = f"""
You're a senior Google Ads strategist.

Given the {label} data below, return a JSON array of insights.
Each object MUST include:
- "Characteristic"
- "Insight"           (use actual metric values from the table)
- "Recommendation"    (tactical, specific)

HARD RULES:
- Output JSON ONLY. No markdown, no extra narration.
- Return a JSON ARRAY (like: [{{...}}, {{...}}]).
- Keep each value on one line (no embedded newlines).
- Only include rows with meaningful, actionable insights.
- Do not hallucinate or make up data. Strictly use the data provided.
- Provide a summary of key actionables that are not at all even slightest generic at the end
- Avoid generic advice; reference the table data.
- The insights provided shall be the result of deep level analysis of the data provided considering various aspects such as trends, patterns, anomalies, and correlations within the data and metric values.

Data:
{data_str}
"""

    try:
        raw = (model.generate_content(prompt).text or "").strip()
        insights = parse_and_repair(raw)
        if not isinstance(insights, list):
            return []
        return insights
    except Exception as e:
        print(f"[ERROR] Gemini API error in gemini_summary: {e}")
        return []
