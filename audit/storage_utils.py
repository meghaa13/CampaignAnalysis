import os
from google.cloud import storage

# Directory for storing heatmaps locally
REPORT_IMAGES_DIR = os.environ.get("REPORT_IMAGES_DIR", "/tmp/report_images")
os.makedirs(REPORT_IMAGES_DIR, exist_ok=True)

BUCKET_NAME = os.environ.get("BUCKET_NAME")
storage_client = None
bucket = None
if BUCKET_NAME:
    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
    except Exception as e:
        print(f"[WARN] storage_utils: GCS Init failed: {e}")

def upload_file_to_gcs(local_path, dest_blob_name):
    if not bucket:
        print(f"[INFO] GCS disabled. Skipping upload of {local_path}")
        return None
    blob = bucket.blob(dest_blob_name)
    blob.upload_from_filename(local_path)
    return f"gs://{BUCKET_NAME}/{dest_blob_name}"


def upload_report_images_to_gcs(report_filepath):
    """Upload heatmap PNGs from REPORT_IMAGES_DIR to GCS under a folder named after report."""
    if not bucket:
        return None
    base = os.path.splitext(os.path.basename(report_filepath))[0]
    prefix = f"generated_reports/{base}_images/"
    for fname in os.listdir(REPORT_IMAGES_DIR):
        if fname.lower().endswith(".png"):
            local = os.path.join(REPORT_IMAGES_DIR, fname)
            dest = prefix + fname
            try:
                upload_file_to_gcs(local, dest)
            except Exception as e:
                print(f"[WARN] Failed to upload heatmap {local}: {e}")
    return prefix
