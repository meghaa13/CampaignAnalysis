from .utils_text import clean, parse_and_repair
from .config import model

def gemini_wasted_spend_summary(df):
    if df is None or df.empty:
        return ""

    # ── Compute campaign-wide baselines ──────────────────────────────────────
    total_clicks       = df['Clicks'].sum()
    total_impressions  = df['Impressions'].sum()
    total_conversions  = df['Conversions'].sum()
    total_cost         = df['Cost ($)'].sum()

    campaign_avg_ctr = total_clicks / total_impressions if total_impressions > 0 else 0
    campaign_avg_cvr = total_conversions / total_clicks if total_clicks > 0 else 0
    campaign_avg_cpc = total_cost / total_clicks if total_clicks > 0 else 0
    campaign_avg_cpa = total_cost / total_conversions if total_conversions > 0 else 0

    print(f"[WS] Campaign Avg CTR={campaign_avg_ctr:.2%}  CVR={campaign_avg_cvr:.2%}  "
          f"CPC=${campaign_avg_cpc:.2f}  CPA=${campaign_avg_cpa:.2f}")

    # ── Dimension 1: Zero-conversion spend (clear waste) ─────────────────────
    d1 = df[(df['Conversions'] == 0) & (df['Cost ($)'] > 0)].copy()
    d1['Waste Dimension'] = 'Zero-Conversion Spend'

    # ── Dimension 2: Below-avg CTR (impression waste / poor relevance) ───────
    d2 = df[(df['CTR'] < campaign_avg_ctr * 0.7) & (df['Cost ($)'] > 0)].copy()
    d2['Waste Dimension'] = 'Low CTR – Poor Ad Relevance'

    # ── Dimension 3: Below-avg CVR but still spending (landing page waste) ───
    d3 = df[
        (df['Conversion Rate'] < campaign_avg_cvr * 100 * 0.7) &
        (df['Conversions'] > 0) &
        (df['Cost ($)'] > 0)
    ].copy()
    d3['Waste Dimension'] = 'Low CVR – Landing Page / Audience Mismatch'

    # ── Dimension 4: High CPA (CPA > 2× campaign average) ────────────────────
    d4 = df[
        (df['CPA ($)'] > campaign_avg_cpa * 2) &
        (df['Conversions'] > 0)
    ].copy()
    d4['Waste Dimension'] = 'High CPA – Inefficient Conversion Cost'

    # ── Dimension 5: High CPC but no Quality Score signal (budget bleed) ─────
    # CPA ($) proxy: campaigns paying well above avg CPC without commensurate returns
    d5 = df[
        (df['Avg CPC'] > campaign_avg_cpc * 1.5) &
        (df['Conversion Rate'] < campaign_avg_cvr * 100)
    ].copy()
    d5['Waste Dimension'] = 'High CPC + Low CVR – Bid Strategy Misalignment'

    import pandas as pd
    df_flagged = pd.concat([d1, d2, d3, d4, d5]).drop_duplicates(subset=['Campaign Name'])
    df_flagged = df_flagged.sort_values('Cost ($)', ascending=False).head(30)

    if df_flagged.empty:
        return ("Characteristic | Insight | Recommendation\n"
                "No Wasted Spend Detected | All campaigns perform at or above average on all 5 waste dimensions | "
                "Maintain current optimisation cadence and monitor weekly.")

    cols_for_prompt = [
        'Campaign Name', 'Waste Dimension', 'Cost ($)', 'CTR',
        'Conversion Rate', 'Avg CPC', 'CPA ($)', 'Impressions', 'Clicks', 'Conversions'
    ]
    available_cols = [c for c in cols_for_prompt if c in df_flagged.columns]

    prompt = f"""
You are a senior Google Ads performance specialist conducting a wasted-spend audit.

The table below lists campaigns flagged across FIVE waste dimensions:
1. Zero-Conversion Spend         – budget burned with zero return
2. Low CTR / Poor Ad Relevance   – impressions not converting to clicks (Quality Score drag)
3. Low CVR / LP Mismatch         – clicks not converting (landing page or audience issue)
4. High CPA                      – conversions happening but at 2× the account average cost
5. High CPC + Low CVR            – bid strategy paying over the odds for underperforming traffic

For EACH flagged campaign, identify which waste dimension applies, quantify the financial impact,
and give a concrete, specialist-level recommendation (bid adjustment, match-type change,
negative keyword addition, LP improvement, audience exclusion, ad-schedule change, etc.).

Output MUST be a valid JSON array. Each object:
- "Characteristic": waste dimension name
- "Insight": exact metric values from the table, campaign name, and quantified loss
- "Recommendation": specific, actionable fix (not generic advice)

Rules:
- No markdown. No commentary outside the JSON array.
- Name the campaign explicitly in every Insight.
- Do not hallucinate. Use only the data below.

Campaign Benchmarks (account averages):
- Avg CTR: {campaign_avg_ctr:.2%}
- Avg CVR: {campaign_avg_cvr:.2%}
- Avg CPC: ${campaign_avg_cpc:.2f}
- Avg CPA: ${campaign_avg_cpa:.2f}

Flagged Campaigns:
{df_flagged[available_cols].to_string(index=False)}
"""

    try:
        raw_output = model.generate_content(prompt).text or ""
        parsed = parse_and_repair(raw_output)
        if isinstance(parsed, list) and len(parsed) > 0:
            import json as _j
            return _j.dumps(parsed, ensure_ascii=False)
        print("[LLM] Wasted spend: LLM output unparseable, using rule-based.")
    except Exception as e:
        print(f"[LLM] Wasted spend error: {e}. Using rule-based output.")

    # ── Rule-based fallback ───────────────────────────────────────────────────
    import json as _j
    rule_rows = []
    for _, r in df_flagged.iterrows():
        dim = r.get("Waste Dimension", "Wasted Spend")
        campaign = r.get("Campaign Name", "Campaign")
        cost = r.get("Cost ($)", 0)
        ctr  = r.get("CTR", 0)
        cvr  = r.get("Conversion Rate", 0)
        cpa  = r.get("CPA ($)", 0)
        cpc  = r.get("Avg CPC", 0)

        rec_map = {
            "Zero-Conversion Spend":         f"Pause {campaign} or add conversion tracking. ${cost:.2f} fully unattributed.",
            "Low CTR – Poor Ad Relevance":   f"Rewrite {campaign} headlines to include exact keyword. Add sitelinks.",
            "Low CVR – Landing Page / Audience Mismatch": f"A/B test landing page for {campaign}. Check audience targeting.",
            "High CPA – Inefficient Conversion Cost":     f"Set Target CPA bid on {campaign} at account avg. Review LP.",
            "High CPC + Low CVR – Bid Strategy Misalignment": f"Reduce max CPC on {campaign} by 25%. Switch to Target CPA bidding.",
        }
        rule_rows.append({
            "Characteristic": dim,
            "Insight": f"{campaign} | Cost: ${cost:.2f} | CTR: {ctr:.2%} | CVR: {cvr:.2f}% | CPA: ${cpa:.2f} | CPC: ${cpc:.2f}",
            "Recommendation": rec_map.get(dim, f"Review {campaign} performance and optimise bids."),
        })
    return _j.dumps(rule_rows, ensure_ascii=False) if rule_rows else ""