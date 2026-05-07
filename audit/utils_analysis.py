from .utils_text import clean, parse_and_repair
from .config import model
import json
import re

def _rule_based_risks_opps(df, campaigns_df=None):
    """
    Pure rule-based fallback for Risks & Opportunities.
    No LLM. Deterministic. Always produces output.
    Used when LLM returns nothing or all providers are exhausted.
    """
    import pandas as _pd

    risks = []
    opps  = []

    # ── Combine keyword + campaign data ──────────────────────────────────────
    frames = [f for f in [df, campaigns_df] if f is not None and not f.empty]
    if not frames:
        return {"Risks": [], "Opportunities": []}

    all_data = _pd.concat(frames, ignore_index=True)

    def s(col): return all_data[col].sum() if col in all_data.columns else 0
    total_cost  = s("Cost ($)");  total_clicks = s("Clicks")
    total_convs = s("Conversions"); total_imps = s("Impressions")

    avg_cpa = total_cost / total_convs  if total_convs  > 0 else 0
    avg_cpc = total_cost / total_clicks if total_clicks > 0 else 0
    avg_ctr = total_clicks / total_imps if total_imps   > 0 else 0
    avg_cvr = total_convs / total_clicks if total_clicks > 0 else 0

    # ── RISKS ─────────────────────────────────────────────────────────────────

    # R1: Low QS keywords
    if "Quality Score" in df.columns:
        low_qs = df[df["Quality Score"].fillna(10) < 5]
        if not low_qs.empty:
            names = ", ".join(low_qs["Keyword"].head(3).tolist()) if "Keyword" in low_qs.columns else "multiple keywords"
            total_low_qs_cost = low_qs["Cost ($)"].sum() if "Cost ($)" in low_qs.columns else 0
            risks.append({
                "Characteristic": "Quality Score Drag",
                "Insight": f"{len(low_qs)} keywords with QS < 5 spending ${total_low_qs_cost:.2f}. Examples: {names}",
                "Recommendation": "Rewrite ads to include exact keyword in headline. Fix LP relevance. Low QS inflates your CPCs account-wide."
            })

    # R2: High CPA campaigns
    if campaigns_df is not None and not campaigns_df.empty and "CPA ($)" in campaigns_df.columns:
        high_cpa = campaigns_df[campaigns_df["CPA ($)"] > avg_cpa * 1.8].sort_values("CPA ($)", ascending=False)
        if not high_cpa.empty:
            top = high_cpa.iloc[0]
            risks.append({
                "Characteristic": "High CPA — Budget Inefficiency",
                "Insight": f"{top.get('Campaign Name','Campaign')}: CPA=${top.get('CPA ($)',0):.2f} vs account avg ${avg_cpa:.2f} ({((top.get('CPA ($)',0)/max(avg_cpa,0.01))-1)*100:.0f}% above avg)",
                "Recommendation": f"Set Target CPA = ${avg_cpa*1.2:.2f} on this campaign. Review audience targeting and LP match."
            })

    # R3: Zero-conversion campaigns with high spend
    if campaigns_df is not None and not campaigns_df.empty:
        zero_conv = campaigns_df[
            (campaigns_df.get("Conversions", _pd.Series(dtype=float)).fillna(0) == 0) &
            (campaigns_df.get("Cost ($)", _pd.Series(dtype=float)).fillna(0) > 50)
        ] if "Conversions" in campaigns_df.columns else _pd.DataFrame()
        if not zero_conv.empty:
            worst = zero_conv.sort_values("Cost ($)", ascending=False).iloc[0]
            risks.append({
                "Characteristic": "Zero-Conversion High Spend",
                "Insight": f"{worst.get('Campaign Name','Campaign')}: ${worst.get('Cost ($)',0):.2f} spent with 0 conversions",
                "Recommendation": "Pause campaign or add conversion tracking. Check if landing page loads correctly."
            })

    # R4: Broad match dominance
    if "Match Type" in df.columns:
        broad_cost = df[df["Match Type"] == "BROAD"]["Cost ($)"].sum() if "Cost ($)" in df.columns else 0
        broad_pct  = broad_cost / total_cost if total_cost > 0 else 0
        if broad_pct > 0.6:
            risks.append({
                "Characteristic": "Broad Match Over-Reliance",
                "Insight": f"{broad_pct:.0%} of keyword spend (${broad_cost:.2f}) on BROAD match. High risk of irrelevant query spend.",
                "Recommendation": "Download search terms report. Add exact-match negatives for irrelevant queries. Shift top performers to Phrase or Exact match."
            })

    # R5: Low CTR signals
    if avg_ctr > 0:
        low_ctr_kws = df[df["CTR"].fillna(0) < avg_ctr * 0.4] if "CTR" in df.columns else _pd.DataFrame()
        if not low_ctr_kws.empty:
            count = len(low_ctr_kws)
            spend = low_ctr_kws["Cost ($)"].sum() if "Cost ($)" in low_ctr_kws.columns else 0
            risks.append({
                "Characteristic": "Low CTR — Ad Relevance Risk",
                "Insight": f"{count} keywords with CTR below 40% of account avg ({avg_ctr:.2%}). Combined spend: ${spend:.2f}",
                "Recommendation": "Add ad extensions (sitelinks, callouts, structured snippets). Test RSA headlines with keyword insertion."
            })

    # ── OPPORTUNITIES ─────────────────────────────────────────────────────────

    # O1: High CVR keywords — under-invested
    if "CVR" in df.columns or "Conversion Rate" in df.columns:
        cvr_col = "CVR" if "CVR" in df.columns else "Conversion Rate"
        high_cvr = df[df[cvr_col].fillna(0) > avg_cvr * 1.5].sort_values("Cost ($)", ascending=True)
        if not high_cvr.empty:
            top = high_cvr.iloc[0]
            kw_name = top.get("Keyword", top.get("Campaign Name", "Keyword"))
            opps.append({
                "Characteristic": "High CVR — Scale Opportunity",
                "Insight": f"'{kw_name}': CVR={top.get(cvr_col,0):.2%} vs avg {avg_cvr:.2%} but only ${top.get('Cost ($)',0):.2f} spend",
                "Recommendation": f"Increase budget/bids on this keyword. Consider adding similar phrase-match variants. High CVR = proven buyer intent."
            })

    # O2: Exact match with low CPC — room to raise bids
    if "Match Type" in df.columns and "Avg CPC" in df.columns:
        exact_low = df[
            (df["Match Type"] == "EXACT") &
            (df["Avg CPC"].fillna(0) < avg_cpc * 0.7) &
            (df.get("Conversions", _pd.Series(dtype=float)).fillna(0) > 0)
        ]
        if not exact_low.empty:
            top = exact_low.sort_values("Conversions", ascending=False).iloc[0]
            opps.append({
                "Characteristic": "Exact Match — Bid Room Available",
                "Insight": f"'{top.get('Keyword','Keyword')}': EXACT match | CPC=${top.get('Avg CPC',0):.2f} (below avg ${avg_cpc:.2f}) | {top.get('Conversions',0):.0f} conversions",
                "Recommendation": f"Raise bid by 20-30%. You're likely losing IS to competitors on a proven converting keyword."
            })

    # O3: Paused keywords that converted historically
    if "Status" in df.columns:
        paused_conv = df[
            (df["Status"] == "PAUSED") &
            (df.get("Conversions", _pd.Series(dtype=float)).fillna(0) > 0)
        ]
        if not paused_conv.empty:
            opps.append({
                "Characteristic": "Paused Keywords With Conversions",
                "Insight": f"{len(paused_conv)} paused keywords had {paused_conv.get('Conversions',_pd.Series()).sum():.0f} conversions before pausing",
                "Recommendation": "Review these keywords. Re-enable top performers with tighter match types or updated bids."
            })

    # O4: Budget-constrained campaigns (high CTR, low impressions relative to potential)
    if campaigns_df is not None and not campaigns_df.empty:
        if "CTR" in campaigns_df.columns and "Impressions" in campaigns_df.columns:
            high_ctr_camps = campaigns_df[campaigns_df["CTR"].fillna(0) > avg_ctr * 1.3]
            if not high_ctr_camps.empty:
                top = high_ctr_camps.sort_values("CTR", ascending=False).iloc[0]
                opps.append({
                    "Characteristic": "High CTR Campaign — Budget Expansion",
                    "Insight": f"{top.get('Campaign Name','Campaign')}: CTR={top.get('CTR',0):.2%} (strong ad relevance). Budget may be limiting reach.",
                    "Recommendation": f"Increase daily budget by 20-30%. High CTR = strong ad-keyword match. More budget = more efficient conversions."
                })

    # O5: Low-CPA keywords — add to new campaigns
    if "CPA ($)" in df.columns and avg_cpa > 0:
        low_cpa = df[
            (df["CPA ($)"].fillna(0) < avg_cpa * 0.6) &
            (df.get("Conversions", _pd.Series(dtype=float)).fillna(0) > 1)
        ].sort_values("Conversions", ascending=False)
        if not low_cpa.empty:
            names = ", ".join(low_cpa.head(3).get("Keyword", low_cpa.head(3).index).tolist()[:3])
            opps.append({
                "Characteristic": "Low CPA Keywords — Expansion Targets",
                "Insight": f"Top low-CPA keywords: {names} | CPA < ${avg_cpa*0.6:.2f} vs account avg ${avg_cpa:.2f}",
                "Recommendation": "Create dedicated ad group or campaign for these keywords. Increase bids to capture more traffic while CPA is profitable."
            })

    return {
        "Risks":        risks[:5],
        "Opportunities": opps[:5],
    }


def gemini_summary_risks_opps(df, campaigns_df=None):
    """
    Risks & Opportunities analysis.
    Tries LLM first (Gemini → Groq → OpenRouter via fallback chain).
    Falls back to pure rule-based analysis if LLM returns nothing.
    Always returns a dict with 'Risks' and 'Opportunities'.
    """
    if df is None or df.empty:
        return {"Risks": [], "Opportunities": []}

    import pandas as pd

    # Build a rich context block
    kw_summary = df.head(40).to_string(index=False)

    camp_summary = ""
    if campaigns_df is not None and not campaigns_df.empty:
        camp_summary = f"\nCampaign-level data:\n{campaigns_df.head(20).to_string(index=False)}"

    # Pre-compute account-level stats to give Gemini concrete anchors
    stats_lines = []
    for col, label in [("Cost ($)", "Total Cost"), ("Conversions", "Total Conversions"),
                       ("Clicks", "Total Clicks"), ("Impressions", "Total Impressions")]:
        if col in df.columns:
            stats_lines.append(f"  {label}: {df[col].sum():,.2f}")

    if "Quality Score" in df.columns:
        low_qs = df[df["Quality Score"] < 5]
        stats_lines.append(f"  Keywords with QS < 5: {len(low_qs)} ({len(low_qs)/max(len(df),1)*100:.1f}%)")

    if "Match Type" in df.columns:
        mt_dist = df["Match Type"].value_counts().to_dict()
        stats_lines.append(f"  Match-type distribution: {mt_dist}")

    account_stats = "\n".join(stats_lines)

    prompt = f"""
You are a senior Google Ads strategist conducting a deep account audit.

Analyze the keyword and campaign performance data below across these SIX dimensions:

1. Bid Strategy Efficiency   – CPA / ROAS outliers vs account average
2. Quality Score Drag        – keywords with QS < 5 inflating CPCs account-wide
3. Match-Type Distribution   – over-reliance on BROAD causing irrelevant traffic
4. Budget Pacing             – campaigns likely limited by budget vs. potential impression share
5. Ad-Schedule Opportunities – hours/days with strong CVR being under-bid
6. Structural Anomalies      – duplicate keywords, single-keyword ad groups, missing negatives

Return ONLY a valid JSON object with this exact structure:

{{
  "Risks": [
    {{"Characteristic": "...", "Insight": "exact metric values + campaign/keyword name", "Recommendation": "specific specialist action"}}
  ],
  "Opportunities": [
    {{"Characteristic": "...", "Insight": "exact metric values + campaign/keyword name", "Recommendation": "specific specialist action"}}
  ]
}}

Rules:
- Return 5 Risks and 5 Opportunities (not 3 — go deeper).
- Every Insight MUST name the specific campaign or keyword and quote actual numbers.
- Recommendations must be specialist-level: e.g. "Add exact-match negative '-free trial' to Campaign X" not "add negatives".
- No markdown. No text outside the JSON object.
- Do not hallucinate. Use only the data provided.

Account Summary:
{account_stats}

Keyword-level data (top 40 by cost):
{kw_summary}
{camp_summary}
"""

    try:
        raw = model.generate_content(prompt).text or ""
        cleaned = clean(raw)

        parsed = parse_and_repair(cleaned)

        if not isinstance(parsed, dict):
            import re, json
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                except Exception:
                    parsed = {}

        if isinstance(parsed, dict):
            return {
                "Risks": parsed.get("Risks", []),
                "Opportunities": parsed.get("Opportunities", [])
            }

        print(f"[WARN] Unexpected Gemini output (not dict): {raw[:300]}")
        return {"Risks": [], "Opportunities": []}

    except Exception as e:
        print(f"[ERROR] Gemini summary risks/opps error: {e}")

    # ── Rule-based fallback — always produces output ──────────────────────────
    print("[LLM] Using rule-based risks/opps (LLM unavailable or empty).")
    return _rule_based_risks_opps(df, campaigns_df)

def extract_summary_highlights(df):
    """
    Uses Gemini to return top 3 Risks and top 3 Opportunities from performance data.
    """
    result = gemini_summary_risks_opps(df)
    risks = result.get("Risks", [])[:3]
    opportunities = result.get("Opportunities", [])[:3]
    return risks, opportunities


def wasted_spend_analyzer(df):
    """
    Fully rule-based, deterministic wasted-spend analysis.
    No LLM needed. Returns rich list of dicts for direct use in report.
    Each flag: {Keyword/Campaign, Dimension, Insight, Recommendation, Wasted_$}
    Covers 7 dimensions with account-relative thresholds.
    """
    flags = []
    if df is None or df.empty:
        return flags

    # ── Account-level benchmarks ──────────────────────────────────────────────
    def safe_sum(col): return df[col].sum() if col in df.columns else 0
    def safe_col(row, col, default=0): return row.get(col, default) or default

    total_cost   = safe_sum("Cost ($)")
    total_clicks = safe_sum("Clicks")
    total_imps   = safe_sum("Impressions")
    total_convs  = safe_sum("Conversions")

    avg_ctr = total_clicks / total_imps   if total_imps   > 0 else 0
    avg_cvr = total_convs  / total_clicks if total_clicks  > 0 else 0
    avg_cpc = total_cost   / total_clicks if total_clicks  > 0 else 0
    avg_cpa = total_cost   / total_convs  if total_convs   > 0 else 0

    for _, row in df.iterrows():
        name  = safe_col(row, "Keyword", safe_col(row, "Campaign Name", "N/A"))
        cost  = float(safe_col(row, "Cost ($)"))
        convs = float(safe_col(row, "Conversions"))
        clicks= float(safe_col(row, "Clicks"))
        imps  = float(safe_col(row, "Impressions"))
        cpc   = float(safe_col(row, "Avg CPC"))
        cpa   = float(safe_col(row, "CPA ($)"))
        qs    = float(safe_col(row, "Quality Score", 10))
        ctr   = float(safe_col(row, "CTR", clicks/imps if imps else 0))
        cvr   = float(safe_col(row, "CVR",
                      safe_col(row, "Conversion Rate", convs/clicks if clicks else 0)))
        # Normalise CVR: if stored as percent (e.g. 5.2) divide by 100
        if cvr > 1:
            cvr = cvr / 100
        match = str(safe_col(row, "Match Type", ""))

        # D1 — Zero-conversion spend (clearest waste)
        if convs == 0 and cost > 0:
            flags.append({
                "Name": name,
                "Dimension": "Zero-Conversion Spend",
                "Insight": f"${cost:.2f} spent | 0 conversions | {int(clicks)} clicks wasted",
                "Recommendation": (
                    f"Pause or add exact-match negative. If keeping, add conversion tracking "
                    f"or set a max-CPA bid cap. ${cost:.2f} is fully unattributed spend."
                ),
                "Wasted_$": cost,
            })

        # D2 — Quality Score drag (inflates CPC for whole account)
        if 0 < qs < 5 and cpc > 0:
            cpc_premium = (cpc - avg_cpc) if cpc > avg_cpc else 0
            wasted = cpc_premium * clicks
            flags.append({
                "Name": name,
                "Dimension": "Low Quality Score — CPC Inflation",
                "Insight": (
                    f"QS={int(qs)} | CPC=${cpc:.2f} vs account avg ${avg_cpc:.2f} "
                    f"| CPC premium ≈ ${cpc_premium:.2f}/click | Est. extra spend: ${wasted:.2f}"
                ),
                "Recommendation": (
                    f"Fix ad-to-keyword relevance: tighten ad group theme, rewrite ad headline "
                    f"to include '{name}', improve landing page load speed. "
                    f"QS 4→7 typically cuts CPC by ~30%."
                ),
                "Wasted_$": round(wasted, 2),
            })

        # D3 — Low CTR (impression waste — paying for visibility without clicks)
        if imps > 300 and avg_ctr > 0 and ctr < avg_ctr * 0.5:
            missed_clicks = (avg_ctr - ctr) * imps
            flags.append({
                "Name": name,
                "Dimension": "Low CTR — Impression Waste",
                "Insight": (
                    f"CTR={ctr:.2%} vs account avg {avg_ctr:.2%} | {int(imps):,} impressions | "
                    f"≈{int(missed_clicks):,} clicks lost to poor ad relevance"
                ),
                "Recommendation": (
                    f"A/B test headline with exact keyword match. Add ad extensions "
                    f"(sitelinks, callouts). Consider moving to phrase/exact match to "
                    f"improve relevance score."
                ),
                "Wasted_$": 0,
            })

        # D4 — High CPA (converting but at unsustainable cost)
        if convs > 0 and avg_cpa > 0 and cpa > avg_cpa * 1.8:
            excess = (cpa - avg_cpa) * convs
            flags.append({
                "Name": name,
                "Dimension": "High CPA — Inefficient Conversions",
                "Insight": (
                    f"CPA=${cpa:.2f} vs account avg ${avg_cpa:.2f} "
                    f"({((cpa/avg_cpa)-1)*100:.0f}% above avg) | "
                    f"{convs:.1f} convs | excess cost ≈ ${excess:.2f}"
                ),
                "Recommendation": (
                    f"Set Target CPA bid strategy at ${avg_cpa*1.2:.2f}. "
                    f"Review landing page for this keyword/campaign. "
                    f"Check if audience segment or device is driving the high CPA."
                ),
                "Wasted_$": round(excess, 2),
            })

        # D5 — Broad match bleed (paying for irrelevant traffic)
        if match == "BROAD" and cost > 0 and avg_cvr > 0 and cvr < avg_cvr * 0.5:
            flags.append({
                "Name": name,
                "Dimension": "Broad Match + Low CVR — Match Type Bleed",
                "Insight": (
                    f"BROAD match | CVR={cvr:.2%} vs account avg {avg_cvr:.2%} | "
                    f"Cost=${cost:.2f} | Likely serving irrelevant queries"
                ),
                "Recommendation": (
                    f"Download search terms report for this keyword. Add negatives for "
                    f"irrelevant queries. Consider switching to Phrase match as interim step. "
                    f"Broad match needs strong negative keyword list to be efficient."
                ),
                "Wasted_$": round(cost * 0.4, 2),  # estimate 40% wasted on irrelevant
            })

        # D6 — High CPC + Low CVR (bidding too high for what converts)
        if avg_cpc > 0 and cpc > avg_cpc * 1.5 and avg_cvr > 0 and cvr < avg_cvr * 0.6 and cost > 0:
            flags.append({
                "Name": name,
                "Dimension": "High CPC + Low CVR — Bid/Value Mismatch",
                "Insight": (
                    f"CPC=${cpc:.2f} ({cpc/avg_cpc:.1f}× avg) | "
                    f"CVR={cvr:.2%} ({cvr/avg_cvr:.1f}× avg) | Cost=${cost:.2f} | "
                    f"Paying premium for below-average conversion rate"
                ),
                "Recommendation": (
                    f"Reduce max CPC bid by 20-30%. Switch to Target CPA or Max Conversions "
                    f"bidding to let Google auto-adjust. Check if this keyword attracts "
                    f"window-shoppers vs. intent buyers."
                ),
                "Wasted_$": round(cost * 0.3, 2),
            })

    # Sort by wasted $ descending
    flags.sort(key=lambda x: x.get("Wasted_$", 0), reverse=True)
    return flags


def landing_page_flags(df):
    flags = []
    if df is None:
        return flags
    for _, row in df.iterrows():
        if row.get("Quality Score", 10) < 5:
            flags.append((row.get("Keyword", "N/A"), f"QS: {row['Quality Score']}", "Revamp Landing Page"))
    return flags