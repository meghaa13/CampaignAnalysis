from cx_Freeze import setup, Executable
import os
import sys
from pathlib import Path

# --- Project info ---
app_name = "CampaignOptimisation"
main_script = "run_server.py"   # your Flask entrypoint

# --- Dependencies ---
build_exe_options = {
    "packages": [
        "os",
        "sys",
        "json",
        "yaml",
        "flask",
        "playwright",
        "pychrome",
        "pandas",
        "logging",
        "requests",
        "google",                   # ✅ include Google root
        "google.ads",               # ✅ include ads namespace
        "google.ads.googleads",     # ✅ include the full google-ads client
    ],
    "excludes": [],
    "includes": [
        "seaborn.cm",                        # ✅ fix seaborn circular import
        "matplotlib.backends.backend_agg",   # ✅ safe matplotlib backend
        "matplotlib.pyplot",                 # ✅ bundle pyplot
    ],
    "include_files": [
        # ✅ Bundle Chromium (pointing to your local ms-playwright cache)
        ("bundled_chromium", "bundled_chromium"),
        (
            str(Path(os.environ["USERPROFILE"]) / "AppData" / "Local" / "ms-playwright"),
            "ms-playwright"
        ),
        # ✅ Bundle templates + configs
        ("report_images", "report_images"),
        ("audit", "audit"),
        ("ms-playwright", "ms-playwright"),
        ("templates", "templates"),
        ("base_google-ads.yaml", "base_google-ads.yaml"),
        ("client-secrets-web.json", "client-secrets-web.json"),
        ("geotargets-2025-07-15.csv", "geotargets-2025-07-15.csv"),
        ("token1.json", "token1.json"),  # token storage
    ],
    "optimize": 1,
}

# --- Windows GUI / Console config ---
base = None
if sys.platform == "win32":
    base = "Console"   # or "Win32GUI" if you don’t want terminal

executables = [
    Executable(
        main_script,
        base=base,
        target_name=f"{app_name}.exe",
    )
]

setup(
    name=app_name,
    version="1.0",
    description="Campaign Optimisation Flask App",
    options={"build_exe": build_exe_options},
    executables=executables
)
