import pandas as pd
from .utils_web import normalize_url
from google.ads.googleads.client import GoogleAdsClient
from .utils_text import format_date

def fetch_landing_page_data(
    client: GoogleAdsClient,
    customer_id: str,
    date_range: str = None,
    start_date: str = None,
    end_date: str = None
):
    service = client.get_service("GoogleAdsService")

    if date_range == "CUSTOM" and start_date and end_date:
        start_date = format_date(start_date)
        end_date = format_date(end_date)
        query = f"""
            SELECT 
              landing_page_view.unexpanded_final_url,
              metrics.impressions,
              metrics.clicks,
              metrics.conversions,
              metrics.cost_micros,
              metrics.ctr,
              metrics.average_cpc
            FROM landing_page_view
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
              AND metrics.impressions > 0
              AND metrics.cost_micros > 0
            LIMIT 100
        """
    else:
        query = f"""
            SELECT 
              landing_page_view.unexpanded_final_url,
              metrics.impressions,
              metrics.clicks,
              metrics.conversions,
              metrics.cost_micros,
              metrics.ctr,
              metrics.average_cpc
            FROM landing_page_view
            WHERE segments.date DURING {date_range}
              AND metrics.impressions > 0
              AND metrics.cost_micros > 0
            LIMIT 100
        """

    try:
        response = service.search_stream(customer_id=customer_id, query=query)
    except Exception as e:
        print(f"[ERROR] Error fetching landing page data: {e}")
        return pd.DataFrame()

    data = []
    for batch in response:
        for row in batch.results:
            url = row.landing_page_view.unexpanded_final_url
            cost = row.metrics.cost_micros / 1e6 if row.metrics.cost_micros else 0
            conversions = row.metrics.conversions or 0
            clicks = row.metrics.clicks or 0
            impressions = row.metrics.impressions or 0
            data.append({
                "Final URL": url,
                "Impressions": impressions,
                "Clicks": clicks,
                "Conversions": conversions,
                "Cost ($)": cost
            })

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df["Normalized URL"] = df["Final URL"].apply(normalize_url)
    agg_cols = ["Impressions", "Clicks", "Conversions", "Cost ($)"]
    df = df.groupby("Normalized URL", as_index=False)[agg_cols].sum()
    df["CTR"] = df["Clicks"] / df["Impressions"].replace(0, 1)
    df["CPA ($)"] = df["Cost ($)"] / df["Conversions"].replace(0, 1)
    df["Avg CPC"] = df["Cost ($)"] / df["Clicks"].replace(0, 1)
    df = df.rename(columns={"Normalized URL": "Final URL"})
    #return df

    df_filtered = df[(df["Cost ($)"] > 0) & (df["CTR"] > 0) ]

    return df_filtered