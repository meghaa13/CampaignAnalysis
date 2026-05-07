import pandas as pd
from .utils_web import resolve_geo_names_from_csv, extract_location_parts
from google.ads.googleads.client import GoogleAdsClient
from .utils_text import format_date

def fetch_geo_performance_data(
    client: GoogleAdsClient,
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    start_date: str = None,
    end_date: str = None,
):
    """
    Fetches geo performance data (country level).
    
    :param client: GoogleAdsClient instance
    :param customer_id: Google Ads customer ID
    :param date_range: Predefined range (e.g., "LAST_7_DAYS", "LAST_30_DAYS", "CUSTOM")
    :param start_date: Start date (YYYY-MM-DD) if CUSTOM
    :param end_date: End date (YYYY-MM-DD) if CUSTOM
    """
    service = client.get_service("GoogleAdsService")

    if date_range == "CUSTOM" and start_date and end_date:
        start_date = format_date(start_date)
        end_date = format_date(end_date)

        query = f"""
            SELECT 
                geographic_view.country_criterion_id,
                geographic_view.location_type,
                metrics.impressions,
                metrics.clicks,
                metrics.conversions,
                metrics.cost_micros
            FROM geographic_view
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
              AND metrics.impressions > 0
              AND geographic_view.location_type = 'LOCATION_OF_PRESENCE'
            LIMIT 10000
        """
    else:
        query = f"""
            SELECT 
                geographic_view.country_criterion_id,
                geographic_view.location_type,
                metrics.impressions,
                metrics.clicks,
                metrics.conversions,
                metrics.cost_micros
            FROM geographic_view
            WHERE segments.date DURING {date_range}
              AND metrics.impressions > 0
              AND geographic_view.location_type = 'LOCATION_OF_PRESENCE'
            LIMIT 10000
        """
    try:
        response = service.search_stream(customer_id=customer_id, query=query)
    except Exception as e:
        print(f"[ERROR] Error fetching geo data: {e}")
        return pd.DataFrame()

    data = []
    geo_ids = set()

    for batch in response:
        for row in batch.results:
            geo_id = row.geographic_view.country_criterion_id
            geo_ids.add(geo_id)
            cost = row.metrics.cost_micros / 1e6 if row.metrics.cost_micros else 0
            if cost == 0:
                continue
            conversions = row.metrics.conversions or 0
            clicks = row.metrics.clicks or 0
            cvr = conversions / clicks if clicks else 0
            cpa = cost / conversions if conversions else 0
            data.append({
                "Geo ID": geo_id,
                "Impressions": row.metrics.impressions,
                "Clicks": clicks,
                "Conversions": conversions,
                "Cost ($)": cost,
                "CVR": cvr,
                "CPA ($)": cpa
            })

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    geo_info = resolve_geo_names_from_csv(df["Geo ID"].unique())
    df["Canonical Name"] = df["Geo ID"].apply(lambda x: geo_info.get(x, {}).get("canonical_name", f"GeoID {x}"))
    df["Type"] = df["Geo ID"].apply(lambda x: geo_info.get(x, {}).get("type", "Unknown"))
    location_parts = df["Canonical Name"].apply(extract_location_parts)
    df = pd.concat([df, location_parts], axis=1)
    df = df[[
        "City", "Region", "Country", "Type", "Impressions", "Clicks", "Conversions", "Cost ($)", "CVR", "CPA ($)"
    ]]
    return df
