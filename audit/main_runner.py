from concurrent.futures import ThreadPoolExecutor, as_completed
from .fetch_campaigns import fetch_campaign_data
from .fetch_keywords import fetch_keyword_data
from .fetch_landing_pages import fetch_landing_page_data
from .fetch_hourly import fetch_hourly_performance_data
from .fetch_geo import fetch_geo_performance_data
from .gemini_campaigns import gemini_summary
from .gemini_keywords import gemini_keyword_summary
from .gemini_hourly import gemini_hourly_summary
from .gemini_geo import gemini_geo_summary
from .gemini_wasted import gemini_wasted_spend_summary
from .gemini_lp_audit import run_landing_page_audits
from .gemini_competitor import generate_competitor_insights
from .utils_analysis import wasted_spend_analyzer, gemini_summary_risks_opps
from .report_generator import generate_report
from .config import model
import pandas as pd


# ---------- Helpers ----------
def filter_all_nonzero_rows(df: pd.DataFrame, metrics: list) -> pd.DataFrame:
    if df is None or df.empty or not metrics:
        return df
    mask = (df[metrics].astype(float) != 0).all(axis=1)
    return df[mask]

def aggregate_if_empty(df: pd.DataFrame, group_col: str = None, label: str = "Aggregate") -> pd.DataFrame:
    """
    If DF has 0 values for critical metrics, replace with an aggregate row.
    - Keeps existing structure.
    - If group_col provided, aggregates across all rows into 1 row.
    """
    if df is None or df.empty:
        return df

    # If everything is 0 for cost/impressions/clicks/conversions → aggregate
    critical_metrics = [c for c in ["Cost ($)", "Impressions", "Clicks", "Conversions"] if c in df.columns]
    if not critical_metrics:
        return df

    if (df[critical_metrics].sum().sum() == 0):
        return df  # nothing to aggregate

    if group_col and group_col in df.columns:
        agg = df[critical_metrics].sum().to_dict()
        row = {col: 0 for col in df.columns}
        row.update(agg)
        row[group_col] = label
        return pd.DataFrame([row])[df.columns]

    # No group column, just return total row
    agg = df[critical_metrics].sum().to_dict()
    row = {col: 0 for col in df.columns}
    row.update(agg)
    row[list(df.columns)[0]] = label
    return pd.DataFrame([row])[df.columns]


def normalize_dataframe(df: pd.DataFrame, required_cols: list, label_col: str = None) -> pd.DataFrame:
    """
    Guarantee DataFrame has all required columns.
    - If empty → return one placeholder row.
    - If partially filled → keep real data, fill missing cols with 0/'Unknown'.
    """
    if df is None or df.empty:
        return pd.DataFrame([{
            col: "No data available" if col == label_col else 0
            for col in required_cols
        }])

    df = df.copy()

    # Fill missing columns without wiping real data
    for col in required_cols:
        if col not in df.columns:
            df[col] = "Unknown" if col == label_col else 0

    return df[required_cols]

def safe_topn(df: pd.DataFrame, col: str, n: int) -> pd.DataFrame:
    """Safely sort by column and return top n rows."""
    if df is None or df.empty or col not in df.columns:
        return df
    return df.sort_values(col, ascending=False).head(n)


def get_date_range_from_df(df: pd.DataFrame, date_col: str = "Date") -> str:
    """Extract min/max dates if available."""
    if df is None or df.empty or date_col not in df.columns:
        return ""
    try:
        min_date = pd.to_datetime(df[date_col]).min().date()
        max_date = pd.to_datetime(df[date_col]).max().date()
        return f"Data available from {min_date} to {max_date}"
    except Exception:
        return ""


def _aggregate_daily_campaigns(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily campaign data (with Date column) into per-campaign totals for report."""
    if df is None or df.empty or "Date" not in df.columns:
        return df

    group_cols = [c for c in ["Campaign ID", "Campaign Name", "Status",
                               "Bid Strategy", "Budget/day ($)"] if c in df.columns]
    if not group_cols:
        return df

    sum_cols = {c: "sum" for c in ["Impressions", "Clicks", "Cost ($)", "Conversions"]
                if c in df.columns}
    if not sum_cols:
        return df

    agg = df.groupby(group_cols, as_index=False).agg(sum_cols)

    # Recalculate derived metrics from totals
    if "Clicks" in agg.columns and "Impressions" in agg.columns:
        agg["CTR"] = agg["Clicks"] / agg["Impressions"].replace(0, 1)
    if "Cost ($)" in agg.columns and "Clicks" in agg.columns:
        agg["Avg CPC"] = agg["Cost ($)"] / agg["Clicks"].replace(0, 1)
    if "Cost ($)" in agg.columns and "Conversions" in agg.columns:
        agg["CPA ($)"] = agg["Cost ($)"] / agg["Conversions"].replace(0, 1)
    if "Conversions" in agg.columns and "Clicks" in agg.columns:
        agg["Conversion Rate"] = (agg["Conversions"] / agg["Clicks"].replace(0, 1)) * 100

    return agg


# ---------- Main ----------

def generate_google_ads_report(customer_id, google_ads_client, date_range=None, start_date=None, end_date=None):
    """
    Orchestrates fetching, Gemini summarization and report generation in parallel.
    """

    # 1) Fetch data in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_campaign_data, google_ads_client, customer_id, date_range): "campaigns",
            executor.submit(fetch_keyword_data, google_ads_client, customer_id, date_range, start_date, end_date): "keywords",
            executor.submit(fetch_landing_page_data, google_ads_client, customer_id, date_range, start_date, end_date): "landing_pages",
            executor.submit(fetch_hourly_performance_data, google_ads_client, customer_id, date_range, start_date, end_date): "hourly",
            executor.submit(fetch_geo_performance_data, google_ads_client, customer_id, date_range, start_date, end_date): "geo",
        }

        results = {}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                res = fut.result()
                results[name] = res
                print(f"[INFO] Fetched: {name}")
            except Exception as e:
                print(f"[ERROR] Error in fetch {name}: {e}")
                results[name] = pd.DataFrame() if name != "hourly" else (pd.DataFrame(), pd.DataFrame())

    # 2) Keep daily campaign data for chat, aggregate for report
    campaigns_raw = results.get("campaigns", pd.DataFrame())
    campaigns_daily = campaigns_raw.copy() if campaigns_raw is not None and not campaigns_raw.empty else pd.DataFrame()

    # Collect date range note BEFORE aggregation (while Date column exists)
    data_range_note = get_date_range_from_df(campaigns_daily)

    # Aggregate daily → per-campaign totals for report pipeline
    campaigns_agg = _aggregate_daily_campaigns(campaigns_raw)

    critical_metrics = ["Cost ($)", "Impressions", "Clicks", "Conversions"]

    campaigns_df = filter_all_nonzero_rows(campaigns_agg, critical_metrics)
    keywords_df = filter_all_nonzero_rows(results.get("keywords"), critical_metrics)
    geo_df = filter_all_nonzero_rows(results.get("geo"), critical_metrics)

    campaign_cols = ["Campaign Name","CTR", "Cost ($)", "Impressions", "Clicks", "Conversions", "Avg CPC", "CPA ($)", "Conversion Rate",]
    keyword_cols = ["Ad Group", "Keyword", "Match Type", "Quality Score", "Impressions", "Clicks", "CTR", "Avg CPC", "CPA ($)"]
    geo_cols = ["City", "Region", "Country", "Type", "Impressions", "Clicks", "Conversions", "Cost ($)", "CVR", "CPA ($)"]

    campaigns_df = normalize_dataframe(campaigns_df, campaign_cols, "Campaign")
    keywords_df = normalize_dataframe(keywords_df, keyword_cols, "Keyword")
    geo_df = normalize_dataframe(geo_df, geo_cols, "Geo")

    df_campaign = safe_topn(campaigns_df, "Cost ($)", 30)
    kw_df = safe_topn(keywords_df, "Cost ($)", 50)
    lp_df = results.get("landing_pages", pd.DataFrame())
    hour_pivot, hour_raw_df = results.get("hourly", (pd.DataFrame(), pd.DataFrame()))
    geo_df = safe_topn(geo_df, "Cost ($)", 50)

    # 3) Print date range
    if not data_range_note:
        data_range_note = get_date_range_from_df(keywords_df)
    if data_range_note:
        print(f"[DATE] {data_range_note}")

    # 4) Run Gemini / audits — limit to 3 concurrent workers so we don't
    #    blast the Groq/Gemini TPM (tokens-per-minute) limit all at once.
    with ThreadPoolExecutor(max_workers=3) as executor:
        # Pass BOTH kw_df and df_campaign so risks/opps covers all dimensions
        f_risk_opps = executor.submit(gemini_summary_risks_opps, kw_df, df_campaign)
        f_insight_30 = executor.submit(gemini_summary, df_campaign, "Campaigns")
        f_insight_kw = executor.submit(gemini_keyword_summary, kw_df)
        f_insight_hour = executor.submit(gemini_hourly_summary, hour_raw_df)
        f_insight_geo = executor.submit(gemini_geo_summary, geo_df)
        f_wasted = executor.submit(gemini_wasted_spend_summary, df_campaign)
        f_lp_audit = executor.submit(run_landing_page_audits, lp_df, 2)

        try:
            from .gemini_competitor import generate_competitor_insights
            competitor_df = generate_competitor_insights(
                google_ads_client,
                kw_df,
                lp_df,
                lp_df["Final URL"].iloc[0] if (lp_df is not None and not lp_df.empty and "Final URL" in lp_df.columns) else "",
                model,
                customer_id
            )
        except Exception as e:
            print("[WARN] Competitor insights failed:", e)
            competitor_df = None

        # Collect results
        risk_opp_data = f_risk_opps.result() if f_risk_opps else {"Risks": [], "Opportunities": []}
        insight_30 = f_insight_30.result() if f_insight_30 else ""
        insight_kw = f_insight_kw.result() if f_insight_kw else ""
        insight_hour = f_insight_hour.result() if f_insight_hour else ""
        insight_geo = f_insight_geo.result() if f_insight_geo else ""
        wasted_insight = f_wasted.result() if f_wasted else "No wasted spend analysis available."
        lp_audit_rows = f_lp_audit.result() if f_lp_audit else []

    # 5) Additional lightweight analysis
    wasted_flags = wasted_spend_analyzer(kw_df)

    # 6) Feed data into chat so /chat has real context
    #    Pass DAILY campaign data so chat can filter by date ranges
    try:
        from .chat_with_data import load_data_into_chat
        load_data_into_chat(
            campaigns_df=campaigns_daily,   # daily data with Date column
            keywords_df=kw_df,
            geo_df=geo_df,
            hourly_df=hour_raw_df,
            lp_df=lp_df,
        )
    except Exception as _e:
        print(f"[CHAT] Data load skipped: {_e}")

    # 7) Generate report
    filename = generate_report(
        df_campaign,
        kw_df,
        hour_pivot,
        hour_raw_df,
        insight_30,
        insight_kw,
        insight_hour,
        geo_df,
        insight_geo,
        wasted_flags=wasted_flags,
        wasted_insight=wasted_insight,
        lp_audit_rows=lp_audit_rows,
        risk_opp_insights=risk_opp_data,
        lp_flags=None,
        competitor_df=competitor_df,
    )

    return filename