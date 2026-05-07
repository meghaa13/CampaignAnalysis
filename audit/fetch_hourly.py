import os
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from google.ads.googleads.client import GoogleAdsClient
# Removed versioned import because it broke on local Windows env
# from google.ads.googleads.v20.enums.types.day_of_week import DayOfWeekEnum
from .utils_text import format_date
from .storage_utils import upload_report_images_to_gcs, REPORT_IMAGES_DIR

def fetch_hourly_performance_data(
    client: GoogleAdsClient,
    customer_id: str,
    date_range: str = "LAST_30_DAYS",
    start_date: str = None,
    end_date: str = None,
):
    service = client.get_service("GoogleAdsService")

    if date_range == "CUSTOM" and start_date and end_date:
        start_date = format_date(start_date)
        end_date = format_date(end_date)
        query = f"""
            SELECT segments.day_of_week, segments.hour,
                   metrics.clicks, metrics.conversions, metrics.cost_micros
            FROM campaign
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
              AND campaign.advertising_channel_type = 'SEARCH'
              AND campaign.status IN ('ENABLED', 'PAUSED')
              AND metrics.clicks > 0
            LIMIT 10000
        """
    else:
        query = f"""
            SELECT segments.day_of_week, segments.hour,
                   metrics.clicks, metrics.conversions, metrics.cost_micros
            FROM campaign
            WHERE segments.date DURING {date_range}
              AND campaign.advertising_channel_type = 'SEARCH'
              AND campaign.status IN ('ENABLED', 'PAUSED')
              AND metrics.clicks > 0
            LIMIT 10000
        """

    try:
        response = service.search_stream(customer_id=customer_id, query=query)
    except Exception as e:
        print(f"[ERROR] Error fetching hourly data: {e}")
        return pd.DataFrame(), pd.DataFrame()

    day_of_week_enum = client.get_type("DayOfWeekEnum")
    data = []
    for batch in response:
        for row in batch.results:
            cost = row.metrics.cost_micros / 1e6 if row.metrics.cost_micros else 0
            conversions = row.metrics.conversions or 0
            clicks = row.metrics.clicks or 0
            cvr = conversions / clicks if clicks else 0
            data.append({
                "Day": day_of_week_enum.DayOfWeek(row.segments.day_of_week).name.title().replace("_", " "),
                "Hour": row.segments.hour,
                "Clicks": clicks,
                "Conversions": conversions,
                "Cost ($)": cost,
                "CVR": cvr
            })

    df = pd.DataFrame(data)
    if df.empty:
        return df, df

    pivot = df.pivot_table(
        index="Day",
        columns="Hour",
        values=["Clicks", "Conversions", "Cost ($)", "CVR"],
        aggfunc="sum",
        fill_value=0
    )
    pivot = pivot.replace(0, "")

    # [OK] Save heatmaps to REPORT_IMAGES_DIR
    os.makedirs(REPORT_IMAGES_DIR, exist_ok=True)
    for metric in ["Clicks", "Conversions", "CVR"]:
        heat_data = df.pivot_table(index="Day", columns="Hour", values=metric, aggfunc="sum", fill_value=0)
        plt.figure(figsize=(10, 6))
        sns.heatmap(heat_data, annot=True, fmt=".2f", cmap="coolwarm")
        plt.title(f"Heatmap: {metric} by Day and Hour")
        plt.tight_layout()
        output_path = os.path.join(REPORT_IMAGES_DIR, f"{metric}_heatmap.png")
        plt.savefig(output_path)
        plt.close()
        print(f"[OK] Saved heatmap: {output_path}")

    # [OK] Upload immediately to GCS under this customer_id
    try:
        prefix, uploaded = upload_report_images_to_gcs(customer_id)
        if prefix:
            from flask import session
            session["latest_report_images_prefix"] = prefix
            print(f"[OK] Uploaded heatmaps to GCS prefix: {prefix}")
    except Exception as e:
        print(f"(!) Failed to upload heatmaps to GCS: {e}")

    return df, pivot
